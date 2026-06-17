"""tests for shared/db.py"""

import threading

import pytest

from shared.db import Database
from shared.models import (
    AIUsage,
    Collection,
    Job,
    JobStatus,
    Step,
    StepStatus,
    Worker,
)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def sample_job():
    return Job(
        id="j_20260517_aaaaaa",
        content_type="video",
        pipeline="video",
        domain="deep-learning",
        url="https://example.com",
        style_tags=["case-study"],
        meta={"duration_sec": 485},
    )


class TestSchema:
    def test_init_idempotent(self, tmp_path):
        d = Database(tmp_path / "test.db")
        d.init_schema()
        d.init_schema()
        d.close()


class TestJobCRUD:
    def test_create_and_get(self, db, sample_job):
        db.create_job(sample_job)
        got = db.get_job(sample_job.id)
        assert got is not None
        assert got.id == sample_job.id
        assert got.content_type == "video"
        assert got.domain == "deep-learning"
        assert got.status == JobStatus.PENDING
        assert got.style_tags == ["case-study"]
        assert got.meta == {"duration_sec": 485}

    def test_get_nonexistent(self, db):
        assert db.get_job("nope") is None

    def test_list_all(self, db, sample_job):
        db.create_job(sample_job)
        j2 = Job(id="j_20260517_bbbbbb", content_type="paper", pipeline="paper")
        db.create_job(j2)
        total, jobs = db.list_jobs()
        assert total == 2

    def test_list_filter_status(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, status=JobStatus.PROCESSING)
        total, jobs = db.list_jobs(status="processing")
        assert total == 1
        assert jobs[0].status == JobStatus.PROCESSING

    def test_list_pagination(self, db):
        for i in range(5):
            db.create_job(Job(id=f"j_20260517_{i:06d}", content_type="video", pipeline="video"))
        total, page = db.list_jobs(limit=2, offset=0)
        assert total == 5
        assert len(page) == 2

    def test_update_job(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, status=JobStatus.DONE, progress_pct=100)
        got = db.get_job(sample_job.id)
        assert got.status == JobStatus.DONE
        assert got.progress_pct == 100

    def test_update_json_field(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, meta={"duration_sec": 500, "extra": True})
        got = db.get_job(sample_job.id)
        assert got.meta == {"duration_sec": 500, "extra": True}

    def test_delete_job(self, db, sample_job):
        db.create_job(sample_job)
        db.delete_job(sample_job.id)
        assert db.get_job(sample_job.id) is None

    def test_delete_cascades_steps(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="03_scene", pool="scene"))
        db.delete_job(sample_job.id)
        assert db.get_steps(sample_job.id) == []


class TestStepCRUD:
    def test_upsert_and_get(self, db, sample_job):
        db.create_job(sample_job)
        step = Step(
            job_id=sample_job.id,
            name="03_scene",
            status=StepStatus.RUNNING,
            pool="scene",
            meta={"scenes": 76},
        )
        db.upsert_step(step)
        steps = db.get_steps(sample_job.id)
        assert len(steps) == 1
        assert steps[0].name == "03_scene"
        assert steps[0].status == StepStatus.RUNNING
        assert steps[0].meta == {"scenes": 76}

    def test_upsert_replaces(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="03_scene", pool="scene"))
        db.upsert_step(Step(
            job_id=sample_job.id,
            name="03_scene",
            status=StepStatus.DONE,
            pool="scene",
            duration_sec=120.5,
        ))
        steps = db.get_steps(sample_job.id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.DONE
        assert steps[0].duration_sec == 120.5

    def test_update_step(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="03_scene", pool="scene"))
        db.update_step(sample_job.id, "03_scene", status="done", duration_sec=99.0)
        steps = db.get_steps(sample_job.id)
        assert steps[0].status == StepStatus.DONE
        assert steps[0].duration_sec == 99.0

    def test_get_steps_sorted(self, db, sample_job):
        db.create_job(sample_job)
        for name in ["05_dedup", "03_scene", "04_frames"]:
            db.upsert_step(Step(job_id=sample_job.id, name=name, pool="cpu"))
        steps = db.get_steps(sample_job.id)
        assert [s.name for s in steps] == ["03_scene", "04_frames", "05_dedup"]


class TestWorkerCRUD:
    def test_upsert_and_get(self, db):
        w = Worker(
            id="cpu-12345678",
            type="cpu",
            pools=["scene", "cpu"],
            tags={"vision"},
            reject_tags={"private"},
            hostname="my-pc",
        )
        db.upsert_worker(w)
        got = db.get_worker("cpu-12345678")
        assert got is not None
        assert got.pools == ["scene", "cpu"]
        assert got.tags == {"vision"}
        assert got.reject_tags == {"private"}

    def test_upsert_updates(self, db):
        w = Worker(id="ai-aabbccdd", type="ai")
        db.upsert_worker(w)
        w.status = "busy"
        w.tasks_completed = 5
        db.upsert_worker(w)
        # get_worker 衍生公共状态；要验证存量 status 列直接读底层。
        row = db._conn.execute(
            "SELECT status FROM workers WHERE id=?", ("ai-aabbccdd",)
        ).fetchone()
        assert row["status"] == "busy"
        assert db.get_worker("ai-aabbccdd").tasks_completed == 5

    def test_increment_stats(self, db):
        db.upsert_worker(Worker(id="ai-aabbccdd", type="ai"))
        db.increment_worker_stats("ai-aabbccdd", completed=3, failed=1, duration=100.0)
        db.increment_worker_stats("ai-aabbccdd", completed=2, duration=50.0)
        got = db.get_worker("ai-aabbccdd")
        assert got.tasks_completed == 5
        assert got.tasks_failed == 1
        assert got.total_duration_sec == 150.0

    def test_list_workers(self, db):
        db.upsert_worker(Worker(id="cpu-1", type="cpu"))
        db.upsert_worker(Worker(id="ai-1", type="ai"))
        assert len(db.list_workers()) == 2

    def test_list_workers_derives_public_status(self, db):
        from datetime import datetime, timedelta, timezone

        fresh = datetime.now(timezone.utc)
        offline_age = datetime.now(timezone.utc) - timedelta(minutes=2)   # >30s,<15min
        stale_age = datetime.now(timezone.utc) - timedelta(minutes=30)    # >15min
        db.upsert_worker(
            Worker(id="cpu-idle", type="cpu", status="idle",
                   first_seen=fresh, last_heartbeat=fresh)
        )
        db.upsert_worker(
            Worker(id="cpu-busy", type="cpu", status="busy", current_job="j1",
                   first_seen=fresh, last_heartbeat=fresh)
        )
        db.upsert_worker(
            Worker(id="cpu-off", type="cpu", status="idle",
                   first_seen=offline_age, last_heartbeat=offline_age)
        )
        db.upsert_worker(
            Worker(id="cpu-stale", type="cpu", status="busy",
                   first_seen=stale_age, last_heartbeat=stale_age)
        )
        workers = {w.id: w for w in db.list_workers()}
        assert workers["cpu-idle"].status == "online-idle"
        assert workers["cpu-busy"].status == "online-busy"
        assert workers["cpu-off"].status == "offline"
        assert workers["cpu-stale"].status == "stale"

    def test_list_workers_persists_stale(self, db):
        """越过 stale 窗口的 worker，公共状态 stale 要落库供 GC 识别。"""
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.upsert_worker(
            Worker(id="cpu-zombie", type="cpu", status="busy",
                   first_seen=old, last_heartbeat=old)
        )
        db.list_workers()
        # 直接读底层列，绕过 list_workers 的衍生，确认已持久化为 stale。
        row = db._conn.execute(
            "SELECT status FROM workers WHERE id=?", ("cpu-zombie",)
        ).fetchone()
        assert row["status"] == "stale"

    def test_list_workers_draining_overlay(self, db):
        """draining 是管理员叠加位：仍在线显示 draining，离线后回落到失联归类。"""
        from datetime import datetime, timedelta, timezone

        fresh = datetime.now(timezone.utc)
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.upsert_worker(
            Worker(id="cpu-drain-on", type="cpu", status="draining",
                   first_seen=fresh, last_heartbeat=fresh)
        )
        db.upsert_worker(
            Worker(id="cpu-drain-dead", type="cpu", status="draining",
                   first_seen=old, last_heartbeat=old)
        )
        workers = {w.id: w for w in db.list_workers()}
        assert workers["cpu-drain-on"].status == "draining"
        assert workers["cpu-drain-dead"].status == "stale"

    def test_set_worker_status_does_not_touch_heartbeat(self, db):
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.upsert_worker(
            Worker(id="cpu-1", type="cpu", status="idle",
                   first_seen=old, last_heartbeat=old)
        )
        db.set_worker_status("cpu-1", "offline")
        got = db.get_worker("cpu-1")
        assert got.status == "offline"
        # last_heartbeat 未被刷新（仍停在 10 分钟前）
        assert (datetime.now(timezone.utc) - got.last_heartbeat).total_seconds() > 300

    def test_delete_worker(self, db):
        db.upsert_worker(Worker(id="cpu-1", type="cpu"))
        db.delete_worker("cpu-1")
        assert db.get_worker("cpu-1") is None

    def test_update_worker_heartbeat_refreshes_timestamp(self, db):
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.upsert_worker(
            Worker(id="cpu-1", type="cpu", status="idle",
                   first_seen=old, last_heartbeat=old)
        )
        db.update_worker_heartbeat("cpu-1")
        got = db.get_worker("cpu-1")
        # 心跳被刷新到接近现在（而非停在 10 分钟前）
        assert (datetime.now(timezone.utc) - got.last_heartbeat).total_seconds() < 5
        # 公共状态由心跳新鲜度衍生：刚心跳 + 无任务 -> online-idle
        assert got.status == "online-idle"

    def test_update_worker_heartbeat_updates_status_and_task(self, db):
        db.upsert_worker(Worker(id="ai-1", type="ai", status="idle"))
        db.update_worker_heartbeat(
            "ai-1", status="busy", current_job="j1", current_step="A"
        )
        got = db.get_worker("ai-1")
        # 刚心跳 + 有在跑任务 -> online-busy
        assert got.status == "online-busy"
        assert got.current_job == "j1"
        assert got.current_step == "A"


class TestWorkerAwareUTC:
    """UTC 全量迁移：读出的时间戳必须是 aware-UTC，且与 aware now 相减不崩。"""

    def test_default_first_seen_is_aware_utc(self, db):
        from datetime import timezone

        db.upsert_worker(Worker(id="cpu-1", type="cpu"))
        got = db.get_worker("cpu-1")
        assert got.first_seen.tzinfo is not None
        assert got.first_seen.utcoffset().total_seconds() == 0

    def test_heartbeat_roundtrip_is_aware(self, db):
        from datetime import datetime, timezone

        db.upsert_worker(Worker(id="cpu-1", type="cpu"))
        db.update_worker_heartbeat("cpu-1")
        got = db.get_worker("cpu-1")
        assert got.last_heartbeat.tzinfo is not None
        # 与 aware now 相减不抛 "can't subtract naive and aware"
        delta = datetime.now(timezone.utc) - got.last_heartbeat
        assert delta.total_seconds() < 5

    def test_legacy_naive_row_parsed_as_utc(self, db):
        """旧库里存的 naive 时间串(无 tzinfo)被补成 UTC，兼容历史数据。"""
        from datetime import datetime, timezone

        # 模拟旧数据：直接写一个 naive ISO 串进 DB（绕过模型默认值）
        naive = datetime(2026, 1, 1, 0, 0, 0)
        db._conn.execute(
            "INSERT INTO workers (id, type, status, first_seen, last_heartbeat) "
            "VALUES (?,?,?,?,?)",
            ("cpu-legacy", "cpu", "idle", naive.isoformat(), naive.isoformat()),
        )
        db._conn.commit()
        got = db.get_worker("cpu-legacy")
        assert got.first_seen.tzinfo is not None
        assert got.last_heartbeat.tzinfo is not None
        # 不崩 + 时刻不被时区平移（naive 当作 UTC，绝对值不变）
        assert got.first_seen == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_list_workers_stale_with_legacy_naive_heartbeat(self, db):
        """list_workers 对 naive 旧行做 stale 判定不崩，且正确判失联。"""
        from datetime import datetime, timezone

        # 新鲜（aware）
        db.upsert_worker(
            Worker(id="cpu-fresh", type="cpu", status="idle",
                   last_heartbeat=datetime.now(timezone.utc))
        )
        # 旧 naive 行：很久以前
        db._conn.execute(
            "INSERT INTO workers (id, type, status, first_seen, last_heartbeat) "
            "VALUES (?,?,?,?,?)",
            ("cpu-old", "cpu", "busy", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        db._conn.commit()
        workers = {w.id: w for w in db.list_workers()}
        assert workers["cpu-fresh"].status == "online-idle"
        assert workers["cpu-old"].status == "stale"


class TestWorkerToken:
    """per-worker token：仅存 sha256 hash，round-trip + 吊销使心跳/认领失效。"""

    def test_upsert_and_get_by_hash(self, db):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db.upsert_worker_token(
            token_hash="h1",
            worker_id="cpu-aaaa",
            pools=["cpu", "io"],
            tags=["vision"],
            created_at=now,
        )
        row = db.get_worker_token_by_hash("h1")
        assert row is not None
        assert row["worker_id"] == "cpu-aaaa"
        assert row["pools"] == ["cpu", "io"]
        assert row["tags"] == ["vision"]
        assert row["revoked"] is False
        assert row["created_at"].tzinfo is not None

    def test_get_missing_returns_none(self, db):
        assert db.get_worker_token_by_hash("nope") is None

    def test_revoke_flips_flag(self, db):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db.upsert_worker_token(
            token_hash="h2", worker_id="cpu-bbbb",
            pools=["cpu"], tags=[], created_at=now,
        )
        db.revoke_worker_token("cpu-bbbb")
        row = db.get_worker_token_by_hash("h2")
        assert row["revoked"] is True

    def test_revoke_only_targets_owner(self, db):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db.upsert_worker_token(
            token_hash="ha", worker_id="cpu-x", pools=[], tags=[], created_at=now,
        )
        db.upsert_worker_token(
            token_hash="hb", worker_id="cpu-y", pools=[], tags=[], created_at=now,
        )
        db.revoke_worker_token("cpu-x")
        assert db.get_worker_token_by_hash("ha")["revoked"] is True
        assert db.get_worker_token_by_hash("hb")["revoked"] is False

    def test_list_worker_tokens(self, db):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db.upsert_worker_token(
            token_hash="h3", worker_id="cpu-cccc",
            pools=["cpu"], tags=[], created_at=now,
        )
        rows = db.list_worker_tokens()
        assert len(rows) == 1
        assert rows[0]["worker_id"] == "cpu-cccc"


class TestAIUsage:
    def test_record_and_summary(self, db):
        u = AIUsage(
            exec_id="ai-abc:1716000:0",
            provider="anthropic",
            model="claude-sonnet-4-6",
            job_id="j_xxx",
            step="10_smart",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.0105,
        )
        assert db.record_ai_usage(u) is True
        summary = db.get_usage_summary(job_id="j_xxx")
        assert summary["calls"] == 1
        assert summary["total_input_tokens"] == 1000
        assert summary["total_cost_usd"] == pytest.approx(0.0105)

    def test_exec_id_dedup(self, db):
        u = AIUsage(exec_id="dup-id", provider="test", model="test")
        assert db.record_ai_usage(u) is True
        assert db.record_ai_usage(u) is False
        summary = db.get_usage_summary()
        assert summary["calls"] == 1

    def test_summary_empty(self, db):
        summary = db.get_usage_summary()
        assert summary["calls"] == 0
        assert summary["total_cost_usd"] == 0


class TestCollection:
    def test_create_and_get(self, db):
        c = Collection(id="my-dl", name="深度学习", domain="deep-learning", tags=["论文"])
        db.create_collection(c)
        got = db.get_collection("my-dl")
        assert got.name == "深度学习"
        assert got.tags == ["论文"]

    def test_get_collection_not_found(self, db):
        assert db.get_collection("nonexistent") is None

    def test_list(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.create_collection(Collection(id="c2", name="c2", domain="deep-learning"))
        assert len(db.list_collections()) == 2


class TestCollectionM2:
    """M2：集合 update / delete=解绑 / domain 过滤 / job_count 维护。"""

    def test_update_collection(self, db):
        db.create_collection(Collection(id="c1", name="旧名", domain="ml", tags=["a"]))
        db.update_collection("c1", name="新名", description="desc", tags=["x", "y"])
        got = db.get_collection("c1")
        assert got.name == "新名"
        assert got.description == "desc"
        assert got.tags == ["x", "y"]

    def test_update_collection_partial(self, db):
        db.create_collection(Collection(id="c1", name="名", domain="ml", description="d0"))
        db.update_collection("c1", name="名2")
        got = db.get_collection("c1")
        assert got.name == "名2"
        # description 未传 -> 不动
        assert got.description == "d0"

    def test_list_collections_domain_filter(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.create_collection(Collection(id="c2", name="c2", domain="deep-learning"))
        out = db.list_collections(domain="ml")
        assert [c.id for c in out] == ["c1"]

    def test_delete_collection_unbinds_jobs(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        job = Job(id="j_m2_1", content_type="video", pipeline="video", collection_id="c1")
        db.create_job(job)
        db.delete_collection("c1")
        # 集合没了，但 job 保留、collection_id 置空（解绑）。
        assert db.get_collection("c1") is None
        got = db.get_job("j_m2_1")
        assert got is not None
        assert got.collection_id is None

    def test_increment_collection_count(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.increment_collection_count("c1", 1)
        db.increment_collection_count("c1", 1)
        assert db.get_collection("c1").job_count == 2
        db.increment_collection_count("c1", -1)
        assert db.get_collection("c1").job_count == 1

    def test_increment_collection_count_floor_zero(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.increment_collection_count("c1", -5)
        assert db.get_collection("c1").job_count == 0

    def test_increment_collection_count_empty_id_noop(self, db):
        # collection_id 为空串 -> no-op，不抛。
        db.increment_collection_count("", 1)


class TestGlossary:
    """M2：术语表 upsert / suggestion / accept / list / delete。"""

    def test_upsert_and_get(self, db):
        db.upsert_glossary_term("ml", "梯度下降", definition="一种优化算法", related=["反向传播"])
        got = db.get_glossary_term("ml", "梯度下降")
        assert got is not None
        assert got["definition"] == "一种优化算法"
        assert got["related"] == ["反向传播"]
        assert got["status"] == "accepted"
        assert got["occurrences"] == [] and got["is_topic"] is False

    def test_get_missing_returns_none(self, db):
        assert db.get_glossary_term("ml", "不存在") is None

    def test_upsert_overwrites_definition_keeps_occurrences(self, db):
        db.add_glossary_suggestion("ml", "Transformer", "j1", "video")
        db.upsert_glossary_term("ml", "Transformer", definition="自注意力模型")
        got = db.get_glossary_term("ml", "Transformer")
        assert got["definition"] == "自注意力模型"
        # upsert 保留已有 occurrences，不清空出现索引。
        assert any(o["job_id"] == "j1" for o in got["occurrences"])

    def test_add_suggestion_creates_suggested(self, db):
        db.add_glossary_suggestion("ml", "注意力机制", "j1", "video")
        got = db.get_glossary_term("ml", "注意力机制")
        assert got["status"] == "suggested"
        assert [o["job_id"] for o in got["occurrences"]] == ["j1"]
        assert got["occurrences"][0]["content_type"] == "video"

    def test_add_suggestion_merges_occurrences(self, db):
        db.add_glossary_suggestion("ml", "注意力机制", "j1")
        db.add_glossary_suggestion("ml", "注意力机制", "j2")
        db.add_glossary_suggestion("ml", "注意力机制", "j1")  # 同 job 不重复加
        got = db.get_glossary_term("ml", "注意力机制")
        assert [o["job_id"] for o in got["occurrences"]] == ["j1", "j2"]

    def test_add_suggestion_does_not_downgrade_accepted(self, db):
        db.upsert_glossary_term("ml", "梯度下降", definition="d")  # accepted
        db.add_glossary_suggestion("ml", "梯度下降", "j9")
        got = db.get_glossary_term("ml", "梯度下降")
        # 仍 accepted，只并入出现。
        assert got["status"] == "accepted"
        assert any(o["job_id"] == "j9" for o in got["occurrences"])

    def test_accept_term(self, db):
        db.add_glossary_suggestion("ml", "候选词", "j1")
        db.accept_glossary_term("ml", "候选词")
        assert db.get_glossary_term("ml", "候选词")["status"] == "accepted"

    def test_list_filters(self, db):
        db.upsert_glossary_term("ml", "A")
        db.add_glossary_suggestion("ml", "B", "j1")
        db.upsert_glossary_term("dl", "C")
        assert {t["term"] for t in db.list_glossary(domain="ml")} == {"A", "B"}
        assert {t["term"] for t in db.list_glossary(status="suggested")} == {"B"}
        assert {t["term"] for t in db.list_glossary(domain="ml", status="accepted")} == {"A"}

    def test_list_sorted_by_term(self, db):
        db.upsert_glossary_term("ml", "z")
        db.upsert_glossary_term("ml", "a")
        assert [t["term"] for t in db.list_glossary(domain="ml")] == ["a", "z"]

    def test_delete_term(self, db):
        db.upsert_glossary_term("ml", "X")
        db.delete_glossary_term("ml", "X")
        assert db.get_glossary_term("ml", "X") is None


class TestNotesFTS:
    """M2：笔记全文索引 + 中文子串检索（trigram）。"""

    def test_index_and_search_chinese_substring(self, db):
        db.index_job_notes(
            "j1", "smart", "深度学习入门",
            "本文介绍神经网络与反向传播算法的基本原理。",
            content_type="video", domain="ml", collection_id="c1",
        )
        total, items = db.search_notes("反向传播")
        assert total == 1
        assert items[0]["job_id"] == "j1"
        assert items[0]["note_type"] == "smart"
        assert items[0]["title"] == "深度学习入门"
        assert items[0]["collection_id"] == "c1"
        assert "反向传播" in items[0]["snippet"]

    def test_index_is_idempotent_per_job_note_type(self, db):
        # trigram tokenizer 至少需 3 字才命中，故关键词用 3+ 字。
        db.index_job_notes("j1", "smart", "t1", "第一版讲解卷积神经网络。")
        db.index_job_notes("j1", "smart", "t2", "第二版讲解循环神经网络。")
        total, items = db.search_notes("神经网络")
        # 同 job + note_type 只保留最新一行。
        assert total == 1
        assert items[0]["title"] == "t2"

    def test_index_separate_note_types_coexist(self, db):
        db.index_job_notes("j1", "smart", "智能笔记", "智能版讲解模型。")
        db.index_job_notes("j1", "mechanical", "机械笔记", "机械版讲解模型。")
        total, _ = db.search_notes("讲解模型")
        assert total == 2

    def test_search_filter_collection(self, db):
        db.index_job_notes("j1", "smart", "a", "讲注意力机制。", collection_id="c1")
        db.index_job_notes("j2", "smart", "b", "讲注意力机制。", collection_id="c2")
        total, items = db.search_notes("注意力", collection_id="c1")
        assert total == 1
        assert items[0]["job_id"] == "j1"

    def test_search_filter_domain_and_content_type(self, db):
        db.index_job_notes("j1", "smart", "a", "讲优化器。", domain="ml", content_type="video")
        db.index_job_notes("j2", "smart", "b", "讲优化器。", domain="dl", content_type="paper")
        total, items = db.search_notes("优化器", domain="dl")
        assert total == 1 and items[0]["job_id"] == "j2"
        total2, items2 = db.search_notes("优化器", content_type="paper")
        assert total2 == 1 and items2[0]["job_id"] == "j2"

    def test_delete_job_index(self, db):
        db.index_job_notes("j1", "smart", "a", "讲卷积神经网络。")
        db.delete_job_index("j1")
        total, _ = db.search_notes("卷积")
        assert total == 0

    def test_search_no_match(self, db):
        db.index_job_notes("j1", "smart", "a", "讲卷积。")
        total, items = db.search_notes("量子计算")
        assert total == 0
        assert items == []

    def test_search_empty_query_returns_empty(self, db):
        db.index_job_notes("j1", "smart", "a", "讲卷积。")
        assert db.search_notes("") == (0, [])
        assert db.search_notes("   ") == (0, [])

    def test_search_quote_injection_safe(self, db):
        # 含双引号的查询不应破坏 MATCH 语法（转义后当普通短语处理）。
        db.index_job_notes("j1", "smart", "a", '他说 "你好世界" 然后离开。')
        total, _ = db.search_notes('"你好世界"')
        assert total == 1

    def test_search_pagination(self, db):
        for i in range(5):
            db.index_job_notes(f"j{i}", "smart", f"t{i}", "共同关键词出现在每篇。")
        total, page = db.search_notes("共同关键词", limit=2, offset=0)
        assert total == 5
        assert len(page) == 2


class TestAppCredentials:
    def test_set_and_get(self, db):
        db.set_credential("bili_cookies", '{"sessdata": "abc"}')
        assert db.get_credential("bili_cookies") == '{"sessdata": "abc"}'

    def test_get_missing_returns_none(self, db):
        assert db.get_credential("nope") is None

    def test_set_overwrites(self, db):
        db.set_credential("k", "v1")
        db.set_credential("k", "v2")
        assert db.get_credential("k") == "v2"

    def test_delete(self, db):
        db.set_credential("k", "v")
        db.delete_credential("k")
        assert db.get_credential("k") is None

    def test_delete_missing_is_noop(self, db):
        db.delete_credential("never-existed")
        assert db.get_credential("never-existed") is None


class TestUpdateValidation:
    def test_update_job_invalid_column(self, db, sample_job):
        db.create_job(sample_job)
        with pytest.raises(ValueError, match="Invalid job columns"):
            db.update_job(sample_job.id, hacked_field="bad")

    def test_update_step_invalid_column(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="03_scene", pool="scene"))
        with pytest.raises(ValueError, match="Invalid step columns"):
            db.update_step(sample_job.id, "03_scene", hacked_field="bad")

    def test_update_job_style_tags_roundtrip(self, db, sample_job):
        db.create_job(sample_job)
        db.update_job(sample_job.id, style_tags=["lecture", "code-tutorial"])
        got = db.get_job(sample_job.id)
        assert got.style_tags == ["lecture", "code-tutorial"]


class TestDBEdgeCases:
    def test_create_duplicate_job_id(self, db):
        job1 = Job(id="j_dup", content_type="video", pipeline="test")
        db.create_job(job1)
        job2 = Job(id="j_dup", content_type="paper", pipeline="test2")
        # Should raise IntegrityError or similar
        with pytest.raises(Exception):
            db.create_job(job2)

    def test_get_usage_summary_with_since(self, db):
        """get_usage_summary filters by since parameter."""
        from datetime import datetime, timedelta

        now = datetime.now()
        old = AIUsage(
            exec_id="old-usage-1",
            provider="anthropic",
            model="claude-opus-4-6",
            job_id="j1",
            step="A",
            input_tokens=100,
            output_tokens=50,
            created_at=now - timedelta(days=10),
        )
        recent = AIUsage(
            exec_id="recent-usage-1",
            provider="anthropic",
            model="claude-opus-4-6",
            job_id="j2",
            step="B",
            input_tokens=200,
            output_tokens=100,
            created_at=now - timedelta(hours=1),
        )
        db.record_ai_usage(old)
        db.record_ai_usage(recent)
        # since parameter is a string (ISO format) compared against created_at
        since_str = (now - timedelta(days=1)).isoformat()
        summary = db.get_usage_summary(since=since_str)
        # Only recent usage should be included
        assert summary["total_input_tokens"] == 200
        assert summary["total_output_tokens"] == 100


class TestConcurrency:
    def test_parallel_writes(self, db, sample_job):
        db.create_job(sample_job)
        errors = []

        def update(n):
            try:
                db.update_job(sample_job.id, progress_pct=n)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # After the parallel writes, verify the job still exists and is readable
        got = db.get_job(sample_job.id)
        assert got is not None
        assert got.progress_pct in range(10)


class TestAuditFixes:
    """审计修复的回归保护:FTS 同步 / 原子级联删 / 终态守卫 / 列迁移 / 状态计数。"""

    def _job(self, jid="j_fix_1", collection_id=None, domain="ml"):
        return Job(id=jid, content_type="video", pipeline="video",
                   domain=domain, collection_id=collection_id, title="T")

    def test_update_step_only_if_active_guard(self, db, sample_job):
        # M1:终态(done)步不被迟到的 failed 覆盖
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="s1", pool="cpu"))
        db.update_step(sample_job.id, "s1", status="done")
        db.update_step(sample_job.id, "s1", only_if_active=True, status="failed", error="late")
        steps = {s.name: s for s in db.get_steps(sample_job.id)}
        assert steps["s1"].status == StepStatus.DONE  # 仍为 done,未被改 failed

    def test_update_step_active_still_writes_non_terminal(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="s1", pool="cpu", status=StepStatus.RUNNING))
        db.update_step(sample_job.id, "s1", only_if_active=True, status="failed", error="boom")
        steps = {s.name: s for s in db.get_steps(sample_job.id)}
        assert steps["s1"].status == StepStatus.FAILED  # running→failed 正常写入

    def test_delete_collection_syncs_fts(self, db):
        # L29:删集合后 FTS 行的 collection_id 同步清空
        db.create_collection(Collection(id="c1", name="C", domain="ml"))
        db.create_job(self._job("j1", collection_id="c1"))
        db.index_job_notes("j1", "smart", "标题", "深度学习正文内容", "video", "ml", "c1")
        db.delete_collection("c1")
        _, items = db.search_notes("深度学习", collection_id="c1")
        assert items == []  # 不再按已删集合命中
        _, all_items = db.search_notes("深度学习")
        assert all_items and all_items[0]["collection_id"] is None

    def test_update_job_syncs_fts_metadata(self, db):
        # L30:改 job 的 title/domain/collection_id 同步进 FTS 行
        db.create_job(self._job("j2", domain="ml"))
        db.index_job_notes("j2", "smart", "旧标题", "强化学习正文内容", "video", "ml", "")
        db.update_job("j2", title="新标题", domain="rl")
        _, items = db.search_notes("强化学习", domain="rl")
        assert items and items[0]["title"] == "新标题"
        _, none = db.search_notes("强化学习", domain="ml")
        assert none == []  # 旧 domain 不再命中

    def test_delete_job_cascade_atomic(self, db):
        # L31:级联删 job 同时清 FTS + 集合计数
        db.create_collection(Collection(id="c2", name="C2", domain="ml"))
        db.create_job(self._job("j3", collection_id="c2"))
        db.increment_collection_count("c2", 1)
        db.index_job_notes("j3", "smart", "T", "卷积神经网络正文", "video", "ml", "c2")
        db.delete_job_cascade("j3", "c2")
        assert db.get_job("j3") is None
        assert db.search_notes("卷积神经网络")[0] == 0  # FTS 行已删
        assert db.get_collection("c2").job_count == 0   # 计数 -1

    def test_delete_job_cascade_cleans_glossary_occurrences(self, db):
        # N5:删 job 摘掉 glossary.occurrences 里的悬空 job_id(保留概念与其它出现)
        db.create_job(self._job("j4"))
        db.add_glossary_suggestion("ml", "Transformer", "j4")
        db.add_glossary_suggestion("ml", "Transformer", "j_other")
        db.delete_job_cascade("j4", None)
        term = db.get_glossary_term("ml", "Transformer")
        ids = [o["job_id"] for o in term["occurrences"]]
        assert "j4" not in ids and "j_other" in ids

    def test_count_jobs_by_status(self, db):
        # L17:一次 GROUP BY 取各状态计数
        db.create_job(Job(id="ja", content_type="video", pipeline="video", status=JobStatus.DONE))
        db.create_job(Job(id="jb", content_type="video", pipeline="video", status=JobStatus.DONE))
        db.create_job(Job(id="jc", content_type="video", pipeline="video", status=JobStatus.FAILED))
        counts = db.count_jobs_by_status()
        assert counts.get("done") == 2 and counts.get("failed") == 1

    def test_ensure_columns_adds_missing(self, tmp_path):
        # K:旧库缺列时 init_schema 通过 _ensure_columns 自动补齐,不崩
        import sqlite3
        p = tmp_path / "legacy.db"
        con = sqlite3.connect(str(p))
        # 造一个缺 collection_id/source 列的老 jobs 表
        con.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, content_type TEXT, pipeline TEXT, "
            "domain TEXT, status TEXT, progress_pct INTEGER, meta TEXT, "
            "created_at TEXT, updated_at TEXT, error TEXT)"
        )
        con.commit()
        con.close()
        d = Database(p)
        d.init_schema()  # 应补齐 collection_id/source,不抛
        cols = {r["name"] for r in d._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "collection_id" in cols and "source" in cols
        d.close()
