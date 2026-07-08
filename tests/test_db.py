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


def test_fts_match_query_strips_null_byte():
    # 含空字节的查询:剔除 \x00,不进入 sqlite3 绑定(否则 "unterminated string" → 裸 500)。
    from shared.db import _fts_match_query
    assert "\x00" not in _fts_match_query("ab\x00c")
    assert _fts_match_query("\x00") == ""


class TestSchema:
    def test_init_idempotent(self, tmp_path):
        d = Database(tmp_path / "test.db")
        d.init_schema()
        d.init_schema()   # 二次建表不抛(IF NOT EXISTS)
        # 不止"不抛":表仍在且可查——防 init_schema 被改成 no-op 也假绿。
        assert d.get_job("nope") is None
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
        db.delete_job_cascade(sample_job.id)
        assert db.get_job(sample_job.id) is None

    def test_delete_cascades_steps(self, db, sample_job):
        db.create_job(sample_job)
        db.upsert_step(Step(job_id=sample_job.id, name="03_scene", pool="scene"))
        db.delete_job_cascade(sample_job.id)
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

    def test_upsert_persists_concurrency_and_remote_addr(self, db):
        w = Worker(id="io-deadbeef", type="io", concurrency=4, remote_addr="203.0.113.7")
        db.upsert_worker(w)
        got = db.get_worker("io-deadbeef")
        assert got.concurrency == 4
        assert got.remote_addr == "203.0.113.7"

    def test_default_concurrency_and_remote_addr(self, db):
        db.upsert_worker(Worker(id="cpu-defaults", type="cpu"))
        got = db.get_worker("cpu-defaults")
        assert got.concurrency == 1
        assert got.remote_addr is None

    def test_upsert_updates(self, db):
        w = Worker(id="ai-aabbccdd", type="ai")
        db.upsert_worker(w)
        w.status = "busy"
        w.tasks_completed = 5
        db.upsert_worker(w)
        # get_worker 衍生公共状态;要验证存量 status 列直接读底层。
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
        """越过 stale 窗口的 worker,公共状态 stale 要落库供 GC 识别。"""
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.upsert_worker(
            Worker(id="cpu-zombie", type="cpu", status="busy",
                   first_seen=old, last_heartbeat=old)
        )
        db.list_workers()
        # 直接读底层列,绕过 list_workers 的衍生,确认已持久化为 stale。
        row = db._conn.execute(
            "SELECT status FROM workers WHERE id=?", ("cpu-zombie",)
        ).fetchone()
        assert row["status"] == "stale"

    def test_list_workers_paused_overlay(self, db):
        """paused 是管理员叠加位(独立 admin_status 列):仍在线显示 paused,离线后回落到失联归类。"""
        from datetime import datetime, timedelta, timezone

        fresh = datetime.now(timezone.utc)
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.upsert_worker(
            Worker(id="cpu-paused-on", type="cpu", admin_status="paused",
                   first_seen=fresh, last_heartbeat=fresh)
        )
        db.upsert_worker(
            Worker(id="cpu-paused-dead", type="cpu", admin_status="paused",
                   first_seen=old, last_heartbeat=old)
        )
        workers = {w.id: w for w in db.list_workers()}
        assert workers["cpu-paused-on"].status == "paused"
        assert workers["cpu-paused-dead"].status == "stale"

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
        # last_heartbeat 未被刷新(仍停在 10 分钟前)
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
        # 心跳被刷新到接近现在(而非停在 10 分钟前)
        assert (datetime.now(timezone.utc) - got.last_heartbeat).total_seconds() < 5
        # 公共状态由心跳新鲜度衍生:刚心跳 + 无任务 -> online-idle
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
    """读出的时间戳必须是 aware-UTC,且与 aware now 相减不崩。"""

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
        """旧库里存的 naive 时间串(无 tzinfo)被补成 UTC,兼容历史数据。"""
        from datetime import datetime, timezone

        # 模拟旧数据:直接写一个 naive ISO 串进 DB(绕过模型默认值)
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
        # 不崩 + 时刻不被时区平移(naive 当作 UTC,绝对值不变)
        assert got.first_seen == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_list_workers_stale_with_legacy_naive_heartbeat(self, db):
        """list_workers 对 naive 旧行做 stale 判定不崩,且正确判失联。"""
        from datetime import datetime, timezone

        # 新鲜(aware)
        db.upsert_worker(
            Worker(id="cpu-fresh", type="cpu", status="idle",
                   last_heartbeat=datetime.now(timezone.utc))
        )
        # 旧 naive 行:很久以前
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
    """per-worker token:仅存 sha256 hash,round-trip + 吊销使心跳/认领失效。"""

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

    def test_upsert_token_can_revoke_existing_for_same_worker(self, db):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db.upsert_worker_token(
            token_hash="old", worker_id="cpu-x", pools=["cpu"], tags=[], created_at=now,
        )
        db.upsert_worker_token(
            token_hash="new", worker_id="cpu-x", pools=["cpu"], tags=[],
            created_at=now, revoke_existing=True,
        )

        assert db.get_worker_token_by_hash("old")["revoked"] is True
        assert db.get_worker_token_by_hash("new")["revoked"] is False

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
    """集合 update / delete=解绑 / domain 过滤 / job_count 维护。"""

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
        # 集合没了,但 job 保留、collection_id 置空(解绑)。
        assert db.get_collection("c1") is None
        got = db.get_job("j_m2_1")
        assert got is not None
        assert got.collection_id is None

    def test_delete_collection_purge_strips_occurrences(self, db):
        # purge=True 删名下 job 时,必须同步摘除其 glossary 出现记录,不留悬空 job_id。
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.create_job(Job(id="j_purge_1", content_type="video", pipeline="video", collection_id="c1"))
        db.add_glossary_suggestion("ml", "注意力机制", "j_purge_1", "video")
        db.add_glossary_suggestion("ml", "注意力机制", "j_other", "video")  # 不属该集合,应保留
        db.delete_collection("c1", purge=True)
        assert db.get_job("j_purge_1") is None
        got = db.get_glossary_term("ml", "注意力机制")
        assert [o["job_id"] for o in got["occurrences"]] == ["j_other"]

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
        # collection_id 为空串 -> no-op,不抛。
        db.increment_collection_count("", 1)


class TestCollectionSyncStatus:
    """集合同步状态字段(last_sync_status / last_sync_error)读写。"""

    def test_defaults_none(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        got = db.get_collection("c1")
        assert got.last_sync_status is None
        assert got.last_sync_error is None

    def test_set_sync_status_syncing(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.set_sync_status("c1", "syncing")
        got = db.get_collection("c1")
        assert got.last_sync_status == "syncing"
        assert got.last_sync_error is None

    def test_set_sync_status_error_stores_msg(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.set_sync_status("c1", "error", "boom: network down")
        got = db.get_collection("c1")
        assert got.last_sync_status == "error"
        assert got.last_sync_error == "boom: network down"

    def test_set_sync_status_error_truncated(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.set_sync_status("c1", "error", "x" * 1000)
        assert len(db.get_collection("c1").last_sync_error) == 500

    def test_non_error_status_clears_error(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.set_sync_status("c1", "error", "old failure")
        db.set_sync_status("c1", "syncing")
        assert db.get_collection("c1").last_sync_error is None

    def test_mark_synced_sets_ok_and_clears_error(self, db):
        from datetime import datetime, timezone
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.set_sync_status("c1", "error", "previous failure")
        db.mark_collection_synced("c1", datetime.now(timezone.utc))
        got = db.get_collection("c1")
        assert got.last_sync_status == "ok"
        assert got.last_sync_error is None
        assert got.last_synced_at is not None


class TestCountJobsByStatusScoped:
    """count_jobs_by_status(collection_id) 按集合的状态计数。"""

    def test_scoped_counts(self, db):
        db.create_collection(Collection(id="c1", name="c1", domain="ml"))
        db.create_collection(Collection(id="c2", name="c2", domain="ml"))
        specs = [
            ("a1", "c1", JobStatus.DONE), ("a2", "c1", JobStatus.DONE),
            ("a3", "c1", JobStatus.FAILED), ("a4", "c1", JobStatus.PROCESSING),
            ("b1", "c2", JobStatus.FAILED),
        ]
        for jid, cid, st in specs:
            db.create_job(Job(id=jid, content_type="video", pipeline="video",
                              collection_id=cid, status=st))
        counts = db.count_jobs_by_status("c1")
        assert counts.get("done") == 2
        assert counts.get("failed") == 1
        assert counts.get("processing") == 1
        # 全局(不传)仍统计所有集合
        assert db.count_jobs_by_status().get("failed") == 2


class TestGlossary:
    """术语表 upsert / suggestion / accept / list / delete。"""

    def test_upsert_and_get(self, db):
        db.upsert_glossary_term("ml", "梯度下降", definition="一种优化算法", related=["反向传播"])
        got = db.get_glossary_term("ml", "梯度下降")
        assert got is not None
        assert got["definition"] == "一种优化算法"
        # related 归一为类型化边:字符串入参 → rel='related'。
        assert got["related"] == [{"term": "反向传播", "rel": "related"}]
        assert got["status"] == "accepted"
        assert got["occurrences"] == [] and got["is_topic"] is False

    def test_get_missing_returns_none(self, db):
        assert db.get_glossary_term("ml", "不存在") is None

    def test_upsert_overwrites_definition_keeps_occurrences(self, db):
        db.add_glossary_suggestion("ml", "Transformer", "j1", "video")
        db.upsert_glossary_term("ml", "Transformer", definition="自注意力模型")
        got = db.get_glossary_term("ml", "Transformer")
        assert got["definition"] == "自注意力模型"
        # upsert 保留已有 occurrences,不清空出现索引。
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
        # 仍 accepted,只并入出现。
        assert got["status"] == "accepted"
        assert any(o["job_id"] == "j9" for o in got["occurrences"])

    def test_add_suggestion_with_definition(self, db):
        # (a) 带 definition 插入 -> 存定义且 status=suggested。
        db.add_glossary_suggestion("ml", "反向传播", "j1", "video", definition="链式法则求梯度")
        got = db.get_glossary_term("ml", "反向传播")
        assert got["definition"] == "链式法则求梯度"
        assert got["status"] == "suggested"

    def test_add_suggestion_fills_empty_definition(self, db):
        # (b) 已存在且原 definition 为空 -> 第二次带 definition 补填。
        db.add_glossary_suggestion("ml", "梯度消失", "j1")  # 无定义
        assert db.get_glossary_term("ml", "梯度消失")["definition"] == ""
        db.add_glossary_suggestion("ml", "梯度消失", "j2", definition="深层网络梯度趋零")
        got = db.get_glossary_term("ml", "梯度消失")
        assert got["definition"] == "深层网络梯度趋零"
        # 仍合并 occurrence,不降级。
        assert {o["job_id"] for o in got["occurrences"]} == {"j1", "j2"}

    def test_add_suggestion_does_not_overwrite_nonempty_definition(self, db):
        # (c) 已存在且原 definition 非空 -> 第二次不覆盖。
        db.add_glossary_suggestion("ml", "正则化", "j1", definition="原定义")
        db.add_glossary_suggestion("ml", "正则化", "j2", definition="新定义")
        got = db.get_glossary_term("ml", "正则化")
        assert got["definition"] == "原定义"
        assert {o["job_id"] for o in got["occurrences"]} == {"j1", "j2"}

    def test_add_suggestion_respects_definition_locked(self, db):
        # (d) definition_locked=1(即便原定义为空) -> 第二次不补填。
        db.add_glossary_suggestion("ml", "钉住词", "j1")  # 无定义
        db._conn.execute(
            "UPDATE glossary SET definition_locked=1 WHERE domain=? AND term=?",
            ("ml", "钉住词"),
        )
        db._conn.commit()
        db.add_glossary_suggestion("ml", "钉住词", "j2", definition="不该写入")
        got = db.get_glossary_term("ml", "钉住词")
        assert got["definition"] == ""
        # 钉住只锁定义,occurrence 仍照常并入。
        assert {o["job_id"] for o in got["occurrences"]} == {"j1", "j2"}

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
    """笔记全文索引 + 中文子串检索(trigram)。"""

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
        # trigram tokenizer 至少需 3 字才命中,故关键词用 3+ 字。
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
        # 含双引号的查询不应破坏 MATCH 语法(转义后当普通短语处理)。
        db.index_job_notes("j1", "smart", "a", '他说 "你好世界" 然后离开。')
        total, _ = db.search_notes('"你好世界"')
        assert total == 1

    def test_search_pagination(self, db):
        for i in range(5):
            db.index_job_notes(f"j{i}", "smart", f"t{i}", "共同关键词出现在每篇。")
        total, page = db.search_notes("共同关键词", limit=2, offset=0)
        assert total == 5
        assert len(page) == 2

    def test_index_also_generates_searchable_chunks(self, db):
        db.index_job_notes(
            "j1", "smart", "深度学习入门",
            "# 背景\n本文介绍反向传播算法的链式法则。\n\n# 细节\n梯度下降依赖这些梯度。",
            content_type="video", domain="ml", collection_id="c1",
        )
        total, items = db.search_note_chunks("链式法则", domain="ml")
        assert total == 1
        assert items[0]["job_id"] == "j1"
        assert items[0]["chunk_id"] == "j1:smart:0"
        assert items[0]["evidence"]["chunk_id"] == "j1:smart:0"
        assert items[0]["evidence"]["note_type"] == "smart"
        assert "链式法则" in items[0]["body"]

    def test_chunk_index_is_replaced_with_note(self, db):
        db.index_job_notes("j1", "smart", "t1", "第一版讲解卷积神经网络。")
        db.index_job_notes("j1", "smart", "t2", "第二版讲解循环神经网络。")
        total_old, _ = db.search_note_chunks("卷积神经网络")
        total_new, items = db.search_note_chunks("循环神经网络")
        assert total_old == 0
        assert total_new == 1
        assert items[0]["title"] == "t2"

    def test_delete_job_cascade_removes_chunks(self, db):
        db.create_job(Job(id="j_chunk_del", content_type="video", pipeline="video"))
        db.index_job_notes("j_chunk_del", "smart", "t", "讲解反向传播算法。")
        assert db.search_note_chunks("反向传播")[0] == 1
        db.delete_job_cascade("j_chunk_del")
        assert db.search_note_chunks("反向传播")[0] == 0


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

    def test_plaintext_when_no_key(self, db, monkeypatch):
        # 无 FLORI_SECRET_KEY 时保持明文行为:底层存的就是原文。
        monkeypatch.delenv("FLORI_SECRET_KEY", raising=False)
        import shared.db as dbmod
        dbmod._fernet.cache_clear()
        db.set_credential("k_plain", "sessdata-plain")
        raw = db._conn.execute(
            "SELECT value FROM app_credentials WHERE key=?", ("k_plain",)
        ).fetchone()["value"]
        assert raw == "sessdata-plain"
        assert db.get_credential("k_plain") == "sessdata-plain"


class TestAppCredentialsEncryption:
    """at-rest 加密:设了 FLORI_SECRET_KEY → Fernet 加密落库 + round-trip;
    历史明文行透传;无 key 退回明文。容器当前未装 cryptography,故整类 importorskip。"""

    @pytest.fixture
    def fernet_key(self, monkeypatch):
        crypto = pytest.importorskip("cryptography")  # 缺库则跳过整个用例
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("FLORI_SECRET_KEY", key)
        import shared.db as dbmod
        dbmod._fernet.cache_clear()           # 清掉按旧 env 缓存的实例
        yield key
        dbmod._fernet.cache_clear()           # 复位,避免污染其它测试

    def test_set_encrypts_and_get_roundtrips(self, db, fernet_key):
        secret = '{"sessdata": "TOP-SECRET-COOKIE"}'
        db.set_credential("bili_cookies", secret)
        raw = db._conn.execute(
            "SELECT value FROM app_credentials WHERE key=?", ("bili_cookies",)
        ).fetchone()["value"]
        # 落库的不是明文,且不含敏感子串。
        assert raw != secret
        assert "TOP-SECRET-COOKIE" not in raw
        assert "sessdata" not in raw
        # 读出来还原成原文(round-trip)。
        assert db.get_credential("bili_cookies") == secret

    def test_legacy_plaintext_row_passthrough(self, db, fernet_key):
        # 旧库里的明文行(非 Fernet token):有 key 时解密失败 → 原样透传,不崩。
        db._conn.execute(
            "INSERT INTO app_credentials (key, value, updated_at) VALUES (?,?,?)",
            ("legacy", "raw-plaintext-value", "2026-01-01T00:00:00+00:00"),
        )
        db._conn.commit()
        assert db.get_credential("legacy") == "raw-plaintext-value"

    def test_reencrypt_on_next_write(self, db, fernet_key):
        # 明文遗留行被重新写入后即变密文。
        db._conn.execute(
            "INSERT INTO app_credentials (key, value, updated_at) VALUES (?,?,?)",
            ("k", "plain", "2026-01-01T00:00:00+00:00"),
        )
        db._conn.commit()
        db.set_credential("k", "plain")  # 重写(模拟 reencrypt / 下次写)
        raw = db._conn.execute(
            "SELECT value FROM app_credentials WHERE key=?", ("k",)
        ).fetchone()["value"]
        assert raw != "plain"
        assert db.get_credential("k") == "plain"

    def test_get_with_no_key_returns_raw(self, db, fernet_key, monkeypatch):
        # 先用 key 加密存,再清掉 key:无 fernet 时返回原始(密文)串而非崩。
        db.set_credential("k", "value-x")
        monkeypatch.delenv("FLORI_SECRET_KEY", raising=False)
        import shared.db as dbmod
        dbmod._fernet.cache_clear()
        raw = db._conn.execute(
            "SELECT value FROM app_credentials WHERE key=?", ("k",)
        ).fetchone()["value"]
        assert db.get_credential("k") == raw  # 原样返回,不抛


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
        from datetime import datetime, timedelta

        now = datetime.now()
        old = AIUsage(
            exec_id="old-usage-1",
            provider="anthropic",
            model="claude-opus-4-8",
            job_id="j1",
            step="A",
            input_tokens=100,
            output_tokens=50,
            created_at=now - timedelta(days=10),
        )
        recent = AIUsage(
            exec_id="recent-usage-1",
            provider="anthropic",
            model="claude-opus-4-8",
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
    """回归保护:FTS 同步 / 原子级联删 / 终态守卫 / 列迁移 / 状态计数。"""

    def _job(self, jid="j_fix_1", collection_id=None, domain="ml"):
        return Job(id=jid, content_type="video", pipeline="video",
                   domain=domain, collection_id=collection_id, title="T")

    def test_update_step_only_if_active_guard(self, db, sample_job):
        # 终态(done)步不被迟到的 failed 覆盖
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
        # 删集合后 FTS 行的 collection_id 同步清空
        db.create_collection(Collection(id="c1", name="C", domain="ml"))
        db.create_job(self._job("j1", collection_id="c1"))
        db.index_job_notes("j1", "smart", "标题", "深度学习正文内容", "video", "ml", "c1")
        db.delete_collection("c1")
        _, items = db.search_notes("深度学习", collection_id="c1")
        assert items == []  # 不再按已删集合命中
        _, all_items = db.search_notes("深度学习")
        assert all_items and all_items[0]["collection_id"] is None

    def test_update_job_syncs_fts_metadata(self, db):
        # 改 job 的 title/domain/collection_id 同步进 FTS 行
        db.create_job(self._job("j2", domain="ml"))
        db.index_job_notes("j2", "smart", "旧标题", "强化学习正文内容", "video", "ml", "")
        db.update_job("j2", title="新标题", domain="rl")
        _, items = db.search_notes("强化学习", domain="rl")
        assert items and items[0]["title"] == "新标题"
        _, none = db.search_notes("强化学习", domain="ml")
        assert none == []  # 旧 domain 不再命中

    def test_delete_job_cascade_atomic(self, db):
        # 级联删 job 同时清 FTS + 集合计数
        db.create_collection(Collection(id="c2", name="C2", domain="ml"))
        db.create_job(self._job("j3", collection_id="c2"))
        db.increment_collection_count("c2", 1)
        db.index_job_notes("j3", "smart", "T", "卷积神经网络正文", "video", "ml", "c2")
        db.delete_job_cascade("j3", "c2")
        assert db.get_job("j3") is None
        assert db.search_notes("卷积神经网络")[0] == 0  # FTS 行已删
        assert db.get_collection("c2").job_count == 0   # 计数 -1

    def test_delete_job_cascade_cleans_glossary_occurrences(self, db):
        # 删 job 摘掉 glossary.occurrences 里的悬空 job_id(保留概念与其它出现)
        db.create_job(self._job("j4"))
        db.add_glossary_suggestion("ml", "Transformer", "j4")
        db.add_glossary_suggestion("ml", "Transformer", "j_other")
        db.delete_job_cascade("j4", None)
        term = db.get_glossary_term("ml", "Transformer")
        ids = [o["job_id"] for o in term["occurrences"]]
        assert "j4" not in ids and "j_other" in ids

    def test_count_jobs_by_status(self, db):
        # 一次 GROUP BY 取各状态计数
        db.create_job(Job(id="ja", content_type="video", pipeline="video", status=JobStatus.DONE))
        db.create_job(Job(id="jb", content_type="video", pipeline="video", status=JobStatus.DONE))
        db.create_job(Job(id="jc", content_type="video", pipeline="video", status=JobStatus.FAILED))
        counts = db.count_jobs_by_status()
        assert counts.get("done") == 2 and counts.get("failed") == 1

    def test_schema_version_stamped_after_init(self, db):
        # init_schema 后 user_version 被打戳为 1(可查询的迁移钩子)。
        assert db.schema_version() == 1

    def test_ensure_columns_adds_missing(self, tmp_path):
        # 旧库缺列时 init_schema 通过 _ensure_columns 自动补齐,不崩
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


class TestConceptTimeline:
    """concept_timeline 的分桶/容错的直接 DB 级测试。

    seed 约定:job 用显式 created_at,glossary 的 occurrence 经 job_id 映射到该 created_at;
    concept_timeline 仅取 domain 内 jobs 的 created_at(域外 job_id 不贡献计数)。"""

    def _seed_job(self, db, jid, created_iso, domain="ml", published_iso=None):
        from datetime import datetime

        db.create_job(
            Job(
                id=jid,
                content_type="video",
                pipeline="video",
                domain=domain,
                published_at=datetime.fromisoformat(published_iso) if published_iso else None,
                created_at=datetime.fromisoformat(created_iso),
            )
        )

    def test_month_bucketing(self, db):
        # 两个 job 跨两个月,一个概念在两月各出现一次。
        self._seed_job(db, "jA", "2026-01-10T08:00:00+00:00")
        self._seed_job(db, "jB", "2026-02-15T08:00:00+00:00")
        db.add_glossary_suggestion("ml", "梯度下降", "jA")
        db.add_glossary_suggestion("ml", "梯度下降", "jB")
        tl = db.concept_timeline("ml", granularity="month")
        assert tl["granularity"] == "month"
        assert tl["buckets"] == ["2026-01", "2026-02"]
        assert tl["totals"] == {"2026-01": 1, "2026-02": 1}
        assert len(tl["concepts"]) == 1
        c = tl["concepts"][0]
        assert c["term"] == "梯度下降"
        assert c["buckets"] == {"2026-01": 1, "2026-02": 1}
        assert c["total"] == 2

    def test_day_bucketing(self, db):
        # 同一天两个 job → 该日计 2;另一天 1。
        self._seed_job(db, "jA", "2026-03-01T01:00:00+00:00")
        self._seed_job(db, "jB", "2026-03-01T23:00:00+00:00")
        self._seed_job(db, "jC", "2026-03-02T12:00:00+00:00")
        for jid in ("jA", "jB", "jC"):
            db.add_glossary_suggestion("ml", "注意力", jid)
        tl = db.concept_timeline("ml", granularity="day")
        assert tl["granularity"] == "day"
        assert tl["totals"] == {"2026-03-01": 2, "2026-03-02": 1}
        assert tl["concepts"][0]["buckets"] == {"2026-03-01": 2, "2026-03-02": 1}

    def test_week_bucketing_year_boundary(self, db):
        # ISO 周边界:2025-12-29 与 2026-01-01 同属 2026-W01;2026-01-05 属 2026-W02。
        self._seed_job(db, "jA", "2025-12-29T10:00:00+00:00")
        self._seed_job(db, "jB", "2026-01-01T10:00:00+00:00")
        self._seed_job(db, "jC", "2026-01-05T10:00:00+00:00")
        for jid in ("jA", "jB", "jC"):
            db.add_glossary_suggestion("ml", "Transformer", jid)
        tl = db.concept_timeline("ml", granularity="week")
        assert tl["granularity"] == "week"
        assert tl["totals"] == {"2026-W01": 2, "2026-W02": 1}
        assert tl["buckets"] == ["2026-W01", "2026-W02"]
        assert tl["concepts"][0]["buckets"] == {"2026-W01": 2, "2026-W02": 1}

    def test_corrupt_occurrences_swallowed(self, db):
        # 损坏的 occurrences JSON 被吞掉:该词不贡献计数,且整体不崩。
        self._seed_job(db, "jA", "2026-01-10T08:00:00+00:00")
        db.add_glossary_suggestion("ml", "好词", "jA")
        # 直接写一条非法 occurrences JSON 的术语行。
        db._conn.execute(
            "INSERT INTO glossary (domain, term, occurrences, status) "
            "VALUES (?,?,?,?)",
            ("ml", "坏词", "{not valid json", "accepted"),
        )
        db._conn.commit()
        tl = db.concept_timeline("ml", granularity="month")
        terms = {c["term"] for c in tl["concepts"]}
        assert "好词" in terms
        assert "坏词" not in terms  # 损坏的不贡献,被吞
        assert tl["totals"] == {"2026-01": 1}

    def test_empty_domain_returns_empty_structure(self, db):
        # 未知/空 domain:无 glossary/job → buckets/totals/concepts 皆空,不报错。
        tl = db.concept_timeline("does-not-exist", granularity="month")
        assert tl["granularity"] == "month"
        assert tl["buckets"] == []
        assert tl["totals"] == {}
        assert tl["concepts"] == []

    def test_occurrence_outside_domain_jobs_not_counted(self, db):
        # occurrence 指向不在该 domain 的 job_id → 映射不到 created_at,不计数。
        self._seed_job(db, "jIn", "2026-01-10T08:00:00+00:00", domain="ml")
        self._seed_job(db, "jOut", "2026-01-10T08:00:00+00:00", domain="dl")
        db.add_glossary_suggestion("ml", "跨域词", "jIn")
        db.add_glossary_suggestion("ml", "跨域词", "jOut")  # jOut 属 dl,不在 ml jobs 里
        tl = db.concept_timeline("ml", granularity="month")
        # 只有 ml 内的 jIn 被计入。
        assert tl["totals"] == {"2026-01": 1}
        assert tl["concepts"][0]["total"] == 1

    def test_buckets_by_published_at_not_created_at(self, db):
        # 有 published_at 时按"源内容发布时间"分桶,而非入库时间(created_at)。
        # 两个 job 同月入库(2026-06),但源发布跨两月(2026-01 / 2026-02)。
        self._seed_job(db, "jA", "2026-06-20T08:00:00+00:00", published_iso="2026-01-10T08:00:00+00:00")
        self._seed_job(db, "jB", "2026-06-20T09:00:00+00:00", published_iso="2026-02-15T08:00:00+00:00")
        db.add_glossary_suggestion("ml", "梯度下降", "jA")
        db.add_glossary_suggestion("ml", "梯度下降", "jB")
        tl = db.concept_timeline("ml", granularity="month")
        # 按发布时间 → 一月一个、二月一个;若错按入库时间会是 {"2026-06": 2}。
        assert tl["buckets"] == ["2026-01", "2026-02"]
        assert tl["totals"] == {"2026-01": 1, "2026-02": 1}
        assert tl["concepts"][0]["buckets"] == {"2026-01": 1, "2026-02": 1}

    def test_falls_back_to_created_at_when_published_at_null(self, db):
        # published_at 为空的 job 回退到 created_at 分桶,不被丢弃。
        # jPub 有发布时间(2026-01),jNull 无 → 用入库时间(2026-03)。
        self._seed_job(db, "jPub", "2026-06-20T08:00:00+00:00", published_iso="2026-01-10T08:00:00+00:00")
        self._seed_job(db, "jNull", "2026-03-05T08:00:00+00:00")  # published_at 留空
        db.add_glossary_suggestion("ml", "注意力", "jPub")
        db.add_glossary_suggestion("ml", "注意力", "jNull")
        tl = db.concept_timeline("ml", granularity="month")
        assert tl["totals"] == {"2026-01": 1, "2026-03": 1}
        assert tl["concepts"][0]["buckets"] == {"2026-01": 1, "2026-03": 1}
        assert tl["concepts"][0]["total"] == 2


class TestJobFacets:
    """job_facets 的直接 DB 级测试(source/domain/status 后端聚合计数)。"""

    def test_facet_structure_for_seeded_dataset(self, db):
        from datetime import datetime

        db.create_job(Job(id="j1", content_type="video", pipeline="video",
                          domain="ml", source="bilibili", status=JobStatus.DONE))
        db.create_job(Job(id="j2", content_type="video", pipeline="video",
                          domain="ml", source="bilibili", status=JobStatus.PENDING))
        db.create_job(Job(id="j3", content_type="paper", pipeline="paper",
                          domain="dl", source="arxiv", status=JobStatus.DONE))
        facets = db.job_facets()
        assert set(facets.keys()) == {"source", "domain", "status"}
        assert facets["source"] == {"bilibili": 2, "arxiv": 1}
        assert facets["domain"] == {"ml": 2, "dl": 1}
        assert facets["status"] == {"done": 2, "pending": 1}

    def test_facets_skip_null_source(self, db):
        # source 为 None 的 job 不出现在 source facet(GROUP BY 跳过 None)。
        db.create_job(Job(id="j1", content_type="video", pipeline="video",
                          domain="ml", source="bilibili"))
        db.create_job(Job(id="j2", content_type="video", pipeline="video",
                          domain="ml"))  # source=None
        facets = db.job_facets()
        assert facets["source"] == {"bilibili": 1}  # None 被排除
        assert facets["domain"] == {"ml": 2}

    def test_facets_empty_db(self, db):
        facets = db.job_facets()
        assert facets == {"source": {}, "domain": {}, "status": {}}


class TestJobsBrief:
    def test_batch_enrich(self, db):
        db.create_job(Job(id="j_a", content_type="video", pipeline="video",
                          title="深入理解 Transformer", domain="ai"))
        db.create_job(Job(id="j_b", content_type="paper", pipeline="paper",
                          title=None, domain="ml"))
        out = db.jobs_brief(["j_a", "j_b", "j_missing"])
        assert out["j_a"] == {"title": "深入理解 Transformer", "content_type": "video",
                              "domain": "ai", "status": "pending", "pipeline": "video"}
        assert out["j_b"]["title"] is None
        assert out["j_b"]["content_type"] == "paper"
        assert "j_missing" not in out   # 查不到的 id 不出现(前端按缺失回退)

    def test_empty_and_dedup(self, db):
        assert db.jobs_brief([]) == {}
        assert db.jobs_brief([None, ""]) == {}
        db.create_job(Job(id="j_a", content_type="video", pipeline="video", title="x"))
        out = db.jobs_brief(["j_a", "j_a", "j_a"])   # 去重不报错
        assert list(out.keys()) == ["j_a"]


class TestDeleteCascadeCompleteness:
    """删 job/集合时彻底清:ai_usage + 订阅 ingested_items。"""

    def test_delete_job_cascade_clears_ai_usage_and_ingested(self, db):
        db.create_job(Job(id="jc_1", content_type="article", pipeline="article",
                          collection_id="col_x"))
        db.record_ai_usage(AIUsage(exec_id="e1", provider="claude-cli", model="sonnet",
                                   job_id="jc_1", step="04_smart", cost_usd=0.1))
        db.mark_ingested("col_x", "item_42")
        assert db.list_usage_by_job("jc_1")          # 有用量行
        assert "item_42" in db.ingested_item_ids("col_x")

        db.delete_job_cascade("jc_1", collection_id="col_x", item_id="item_42")

        assert db.get_job("jc_1") is None
        assert db.list_usage_by_job("jc_1") == []     # ai_usage 已删
        assert "item_42" not in db.ingested_item_ids("col_x")  # ingested_items 已删

    def test_delete_job_cascade_keeps_ingested_when_no_item_id(self, db):
        # 不传 item_id(手动 job)→ 不动 ingested_items。
        db.create_job(Job(id="jc_2", content_type="article", pipeline="article",
                          collection_id="col_y"))
        db.mark_ingested("col_y", "keepme")
        db.delete_job_cascade("jc_2", collection_id="col_y")
        assert "keepme" in db.ingested_item_ids("col_y")

    def test_delete_collection_purge_clears_ai_usage(self, db):
        db.create_collection(Collection(id="col_p", name="p", domain="deep-learning"))
        db.create_job(Job(id="jp_1", content_type="article", pipeline="article",
                          collection_id="col_p"))
        db.record_ai_usage(AIUsage(exec_id="e2", provider="claude-cli", model="sonnet",
                                   job_id="jp_1", cost_usd=0.2))
        db.delete_collection("col_p", purge=True)
        assert db.get_job("jp_1") is None
        assert db.list_usage_by_job("jp_1") == []     # purge 也清 ai_usage


class TestLineageP2b:
    """lineage_key / is_current 归组、版本列表、删 current 后回退。"""

    def _mk(self, db, jid, lineage, created, ct="article", cid=None):
        from datetime import datetime, timezone
        from shared.models import Job
        db.create_job(Job(
            id=jid, content_type=ct, pipeline=ct, lineage_key=lineage, collection_id=cid,
            created_at=datetime(2026, 6, 27, 10, 0, created, tzinfo=timezone.utc),
        ))

    def test_same_lineage_groups_to_current(self, db):
        # 同一 lineage 三个快照:仅最后建的为 current(后建 demote 先建)。
        self._mk(db, "jobs_a_x_001", "jobs_a_x", 1)
        self._mk(db, "jobs_a_x_002", "jobs_a_x", 2)
        self._mk(db, "jobs_a_x_003", "jobs_a_x", 3)
        total, jobs = db.list_jobs(current_only=True, limit=50)
        ids = {j.id for j in jobs}
        assert ids == {"jobs_a_x_003"}              # 只剩 current
        total_all, all_jobs = db.list_jobs(current_only=False, limit=50)
        assert len({j.id for j in all_jobs}) == 3   # 全部仍在库

    def test_lineage_versions_and_counts(self, db):
        self._mk(db, "jobs_a_y_001", "jobs_a_y", 1)
        self._mk(db, "jobs_a_y_002", "jobs_a_y", 2)
        vers = db.lineage_versions("jobs_a_y_001")
        assert [v.id for v in vers] == ["jobs_a_y_002", "jobs_a_y_001"]  # 时间倒序
        assert db.lineage_counts(["jobs_a_y"]) == {"jobs_a_y": 2}

    def test_promote_after_delete_current(self, db):
        self._mk(db, "jobs_a_z_001", "jobs_a_z", 1)
        self._mk(db, "jobs_a_z_002", "jobs_a_z", 2)   # current
        # 模拟删 current:删库行后回退
        db.delete_job_cascade("jobs_a_z_002")
        db.promote_lineage_current("jobs_a_z")
        _, jobs = db.list_jobs(current_only=True, limit=50)
        assert {j.id for j in jobs} == {"jobs_a_z_001"}   # 回退到剩余最新


class TestAITaskLogs:
    """独立 AI task 白盒审计(ai_task_logs)落表/读取。"""

    def test_record_and_get(self, db):
        import json
        ok = db.record_ai_task_log({
            "task_id": "at_x", "exec_id": "w:1", "step_name": "synthesis", "domain": "dl",
            "provider": "claude-cli", "model": "subscription", "ok": True,
            "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.12,
            "duration_sec": 2.0, "num_turns": 1,
            "record": {"output": "hi", "prompt": {"system": "S"},
                       "routing": {"attempts": [{"tier": "primary"}]}},
            "created_at": "2026-06-27T00:00:00+00:00",
        })
        assert ok is True
        logs = db.get_ai_task_logs("at_x")
        assert len(logs) == 1
        row = logs[0]
        assert row["provider"] == "claude-cli" and row["step_name"] == "synthesis"
        assert row["ok"] == 1 and row["cost_usd"] == 0.12
        assert json.loads(row["record_json"])["output"] == "hi"
        assert db.get_ai_task_logs("missing") == []

    def test_record_error_row(self, db):
        db.record_ai_task_log({
            "task_id": "at_e", "step_name": "digest", "ok": False, "error": "provider down",
            "record": {"ok": False, "error": "provider down"},
            "created_at": "2026-06-27T00:00:00+00:00",
        })
        row = db.get_ai_task_logs("at_e")[0]
        assert row["ok"] == 0 and "provider down" in row["error"]
