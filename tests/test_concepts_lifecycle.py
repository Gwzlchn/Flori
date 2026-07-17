"""概念生命周期、订阅、雷达关注区和每周自动周报测试。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.models import Job, JobStatus
from tests.conftest import make_fakeredis


class TestAutoPromote:
    def test_two_distinct_jobs_promote(self, db):
        db.add_glossary_suggestion("ml", "Transformer", "j1", "video")
        assert db.get_glossary_term("ml", "Transformer")["status"] == "suggested"
        db.add_glossary_suggestion(
            "ml", "Transformer", "j2", "document",
            document_kind="research_paper",
        )
        assert db.get_glossary_term("ml", "Transformer")["status"] == "accepted"

    def test_same_job_twice_stays_suggested(self, db):
        db.add_glossary_suggestion("ml", "Attention", "j1")
        db.add_glossary_suggestion("ml", "Attention", "j1")
        assert db.get_glossary_term("ml", "Attention")["status"] == "suggested"

    def test_variant_hit_counts_toward_promotion(self, db):
        # 变体经 resolve 归并后,第二个 job 同样触发晋升。
        db.add_glossary_suggestion("ml", "Kelly criterion", "j1", zh_name="凯利准则")
        db.add_glossary_suggestion("ml", "凯利准则", "j2")
        assert db.get_glossary_term("ml", "Kelly criterion")["status"] == "accepted"


class TestReject:
    def test_rejected_not_resuggested(self, db):
        db.add_glossary_suggestion("ml", "France", "j1")
        assert db.reject_glossary_term("ml", "France") is True
        # 同名/变体再采集:不新建、不挂 occurrence、状态不动。
        db.add_glossary_suggestion("ml", "france", "j2")
        t = db.get_glossary_term("ml", "France")
        assert t["status"] == "rejected"
        assert len(t["occurrences"]) == 1
        assert db.get_glossary_term("ml", "france") is None

    def test_reject_missing_returns_false(self, db):
        assert db.reject_glossary_term("ml", "nope") is False

    def test_list_glossary_excludes_rejected_by_default(self, db):
        db.add_glossary_suggestion("ml", "Good", "j1")
        db.add_glossary_suggestion("ml", "Junk", "j1")
        db.reject_glossary_term("ml", "Junk")
        assert {t["term"] for t in db.list_glossary("ml")} == {"Good"}
        assert {t["term"] for t in db.list_glossary("ml", status="rejected")} == {"Junk"}

    def test_rejected_excluded_from_consumers(self, db):
        db.create_job(Job(id="jr1", content_type="document", document_kind="article",
                          pipeline="document",
                          domain="ml", title="内容"))
        db.add_glossary_suggestion(
            "ml", "Junk", "jr1", "document", document_kind="article",
        )
        db.set_glossary_topic("ml", "Junk", True)
        db.reject_glossary_term("ml", "Junk")
        assert db.glossary_for_job("jr1", "ml") == []
        assert all(t["term"] != "Junk" for t in db.domain_top_terms("ml"))
        assert "Junk" not in db.concept_occurrence_dates("ml")
        assert all(c["term"] != "Junk" for c in db.concept_timeline("ml")["concepts"])
        assert all(r["term"] != "Junk" for r in db.glossary_term_rows("ml"))
        assert all(t["term"] != "Junk" for t in db.list_topic_concepts("ml"))


class TestWatch:
    def test_set_watched_roundtrip(self, db):
        db.add_glossary_suggestion("ml", "Momentum", "j1")
        assert db.set_glossary_watched("ml", "Momentum", True) is True
        assert db.get_glossary_term("ml", "Momentum")["watched"] is True
        assert db.set_glossary_watched("ml", "Momentum", False) is True
        assert db.get_glossary_term("ml", "Momentum")["watched"] is False

    def test_set_watched_missing_false(self, db):
        assert db.set_glossary_watched("ml", "nope", True) is False


class TestRadarWatchedSection:
    def test_watched_concepts_in_radar(self, db):
        from api.services.radar import radar
        db.create_job(Job(id="jw1", content_type="document", document_kind="article",
                          pipeline="document",
                          domain="ml", title="新内容"))
        db.add_glossary_suggestion(
            "ml", "Momentum", "jw1", "document", document_kind="article",
        )
        db.add_glossary_suggestion(
            "ml", "Silent", "jw1", "document", document_kind="article",
        )
        db.set_glossary_watched("ml", "Momentum", True)
        data = radar(db, "ml", 7)
        watched = data["watched_concepts"]
        assert [w["term"] for w in watched] == ["Momentum"]
        assert watched[0]["recent"] == 1 and watched[0]["total"] == 1


class TestGlossaryLifecycleAPI:
    @pytest.mark.asyncio
    async def test_reject_endpoint(self, client, db):
        db.add_glossary_suggestion("ml", "Junk", "j1")
        r = await client.post("/api/glossary/ml/Junk/reject")
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        assert (await client.post("/api/glossary/ml/nope/reject")).status_code == 404

    @pytest.mark.asyncio
    async def test_watch_endpoint(self, client, db):
        db.add_glossary_suggestion("ml", "Momentum", "j1")
        r = await client.post("/api/glossary/ml/Momentum/watch", json={"watched": True})
        assert r.status_code == 200 and r.json()["watched"] is True
        assert (await client.post(
            "/api/glossary/ml/nope/watch", json={"watched": True}
        )).status_code == 404

    @pytest.mark.asyncio
    async def test_batch_accept(self, client, db):
        db.add_glossary_suggestion("ml", "A-batch", "j1")
        db.add_glossary_suggestion("ml", "B-batch", "j1")
        r = await client.post("/api/glossary/batch", json={
            "action": "accept",
            "items": [{"domain": "ml", "term": "A-batch"},
                      {"domain": "ml", "term": "B-batch"},
                      {"domain": "ml", "term": "missing"}],
        })
        assert r.status_code == 200
        assert r.json() == {"updated": 2, "skipped": 1}
        assert db.get_glossary_term("ml", "A-batch")["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_batch_reject_and_bad_action(self, client, db):
        db.add_glossary_suggestion("ml", "C-batch", "j1")
        r = await client.post("/api/glossary/batch", json={
            "action": "reject", "items": [{"domain": "ml", "term": "C-batch"}],
        })
        assert r.json()["updated"] == 1
        assert db.get_glossary_term("ml", "C-batch")["status"] == "rejected"
        assert (await client.post(
            "/api/glossary/batch", json={"action": "nuke", "items": []}
        )).status_code == 400


class TestTermMapAliasExport:
    @pytest.mark.asyncio
    async def test_aliases_and_rejected_in_term_map(self, db, tmp_path):
        # 英文别名映射到同一译名;rejected 词条不导出。
        from scheduler.scheduler import Scheduler
        from shared.storage import LocalStorage

        db.add_glossary_suggestion("ml", "Kelly criterion", "jm1", zh_name="凯利准则")
        db.add_glossary_suggestion("ml", "kelly criterion", "jm2")   # 变体入 aliases
        db.add_glossary_suggestion("ml", "Bad term", "jm1", zh_name="坏词")
        db.reject_glossary_term("ml", "Bad term")

        storage = LocalStorage(tmp_path)
        config = SimpleNamespace(jobs_dir=Path(str(tmp_path)))
        eng = Scheduler(redis=None, db=db, config=config, storage=storage)
        job = Job(id="jm_tgt", content_type="document", document_kind="article",
                  pipeline="document", domain="ml")
        await eng._export_term_map(job)

        import json as _json
        raw = await storage.read_file("jm_tgt", "input/term_map.json")
        tmap = _json.loads(raw.decode("utf-8"))
        assert tmap["Kelly criterion"] == "凯利准则"
        assert tmap["kelly criterion"] == "凯利准则"   # 别名同译名
        assert "Bad term" not in tmap


class TestRadarDigestCron:
    def _engine(self, db, redis):
        from scheduler.scheduler import Scheduler
        config = SimpleNamespace(jobs_dir=Path("/tmp/na"), pools={})
        return Scheduler(redis=redis, db=db, config=config, storage=None)

    @pytest.mark.asyncio
    async def test_queues_on_configured_dow_and_dedups(self, db, monkeypatch):
        from tests.test_api_radar import _evidence

        monkeypatch.setenv("RADAR_DIGEST_CRON_DOW", "0")
        redis = make_fakeredis()
        try:
            db.create_job(Job(
                id="jd1", content_type="document", document_kind="article",
                pipeline="document",
                domain="ml", title="本周内容", status=JobStatus.DONE,
            ))
            _evidence(db, "jd1", "Momentum 是本周被反复讨论的概念。", domain="ml")
            db.add_glossary_suggestion(
                "ml", "Momentum", "jd1", "document", document_kind="article",
            )
            eng = self._engine(db, redis)
            monday = date(2026, 7, 6)   # 周一
            n = await eng.check_radar_digest(today=monday)
            assert n == 1
            info = await redis.get_latest_auto_digest("ml")
            assert info and info["task_id"].startswith("at_")
            queued = await redis.r.zrange("queue:ai", 0, 0)
            assert queued
            assert json.loads(queued[0])["request"]["temperature"] == 0
            # 同日再跑:当日锁防重复。
            assert await eng.check_radar_digest(today=monday) == 0
            # 收割任务别在测试残留。
            for t in list(eng._digest_harvest_tasks):
                t.cancel()
        finally:
            await redis.close()

    @pytest.mark.asyncio
    async def test_skips_wrong_dow_and_idle_domain(self, db, monkeypatch):
        monkeypatch.setenv("RADAR_DIGEST_CRON_DOW", "0")
        redis = make_fakeredis()
        try:
            eng = self._engine(db, redis)
            assert await eng.check_radar_digest(today=date(2026, 7, 7)) == 0  # 周二
            # 周一但库里无近窗动静(空库)→ 不投空周报。
            assert await eng.check_radar_digest(today=date(2026, 7, 6)) == 0
        finally:
            await redis.close()

    @pytest.mark.asyncio
    async def test_harvest_moves_result_to_latest(self, db):
        from api.services.radar import build_digest_source_manifest, radar
        from tests.test_api_radar import _evidence

        redis = make_fakeredis()
        try:
            eng = self._engine(db, redis)
            db.create_job(Job(
                id="digest-harvest", content_type="document", document_kind="article",
                pipeline="document",
                domain="ml", title="本周内容", status=JobStatus.DONE,
            ))
            _evidence(db, "digest-harvest", "Momentum 是本周热点。", domain="ml")
            manifest = build_digest_source_manifest(
                db, task_id="at_x1", radar_data=radar(db, "ml", 7),
            )
            source = manifest["sources"][0]
            assert source["content_type"] == "document"
            assert source["document_kind"] == "article"
            content = f"# 周报\n{source['excerpt']} [来源:{source['source_id']}]"
            await redis.r.set(
                "ai:anchor:at_x1",
                json.dumps({
                    "kind": "ai", "task_id": "at_x1", "step": "digest",
                    "audit_context": {"digest_source_manifest": manifest},
                }, ensure_ascii=False, sort_keys=True),
            )
            await redis.set_ai_result("at_x1", {"content": content})
            await eng._harvest_digest_result("ml", "at_x1", "2026-07-06T00:00:00+00:00",
                                             timeout_sec=5, poll_sec=0.01)
            info = await redis.get_latest_auto_digest("ml")
            assert info["markdown"] == content
            assert info["citation_validation"]["reliable"] is True
            assert info["task_id"] == "at_x1"
        finally:
            await redis.close()

    @pytest.mark.asyncio
    async def test_harvest_legacy_result_is_not_published_as_reliable(self, db):
        redis = make_fakeredis()
        try:
            eng = self._engine(db, redis)
            await redis.r.set(
                "ai:anchor:at_legacy",
                json.dumps({
                    "kind": "ai", "task_id": "at_legacy", "step": "digest",
                }, sort_keys=True),
            )
            await redis.set_ai_result("at_legacy", {"content": "旧周报"})
            await eng._harvest_digest_result(
                "ml", "at_legacy", "2026-07-06T00:00:00+00:00",
                timeout_sec=5, poll_sec=0.01,
            )
            info = await redis.get_latest_auto_digest("ml")
            assert "markdown" not in info
            assert info["citation_validation"]["status"] == "unverified"
            assert info["error"] == "digest citation validation failed"
        finally:
            await redis.close()

    @pytest.mark.asyncio
    async def test_latest_digest_endpoint(self, client, app):
        # 未生成过 → task_id null;只有可靠校验通过的持久摘要才返回正文。
        app.state.redis.get_latest_auto_digest.return_value = None
        r = await client.get("/api/domains/ml/digest/latest")
        assert r.status_code == 200 and r.json() == {"task_id": None}
        app.state.redis.get_latest_auto_digest.return_value = {
            "task_id": "at_1", "queued_at": "2026-07-06T00:00:00+00:00", "markdown": "# 周报",
        }
        r2 = await client.get("/api/domains/ml/digest/latest")
        legacy = r2.json()
        assert "markdown" not in legacy
        assert legacy["citation_validation"]["status"] == "unverified"
        assert legacy["citation_validation"]["reliable"] is False
        assert legacy["error"] == "digest citation validation unavailable"

        app.state.redis.get_latest_auto_digest.return_value = {
            "task_id": "at_2", "markdown": "# 可靠周报",
            "citation_validation": {
                "kind": "digest_citations", "status": "valid", "reliable": True,
                "issues": [], "manifest_sha256": "a" * 64,
            },
        }
        reliable = (await client.get("/api/domains/ml/digest/latest")).json()
        assert reliable["markdown"] == "# 可靠周报"

        app.state.redis.get_latest_auto_digest.return_value = {
            "task_id": "at_3", "markdown": "# 不可靠周报",
            "citation_validation": {
                "kind": "digest_citations", "status": "invalid", "reliable": False,
                "issues": ["unknown_source_id"], "manifest_sha256": "b" * 64,
            },
        }
        invalid = (await client.get("/api/domains/ml/digest/latest")).json()
        assert "markdown" not in invalid
        assert invalid["citation_validation"]["status"] == "unverified"
        assert invalid["citation_validation"]["issues"] == [
            "latest_digest_not_reliable", "unknown_source_id",
        ]
