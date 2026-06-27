"""tests for api/routes/queue.py"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.models import Job, Step, StepStatus, Worker
from api.main import create_app


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def redis_mock():
    """默认空队列;各用例按池设置 list_queue / get_queue_info 的 side_effect。"""
    from tests.conftest import make_redis_mock
    rc = make_redis_mock()
    rc.get_queue_info.return_value = {"length": 0}
    rc.list_queue.return_value = []
    return rc


@pytest.fixture
def app(db, test_config, redis_mock):
    return create_app(db=db, redis=redis_mock, config=test_config)


class TestQueue:
    @pytest.mark.asyncio
    async def test_empty(self, client):
        r = await client.get("/api/queue")
        assert r.status_code == 200
        body = r.json()
        assert "pools" in body and body["limit"] == 200
        for p in body["pools"]:
            assert p["running"] == [] and p["queued"] == []
            assert p["queued_count"] == 0

    @pytest.mark.asyncio
    async def test_queued_enriched(self, client, db, redis_mock):
        db.create_job(Job(id="j_y", content_type="paper", pipeline="paper",
                          title="RLHF 综述", domain="ai"))

        async def _info(p):
            return {"length": 1 if p == "ai" else 0}

        async def _list(p, limit=200):
            if p == "ai":
                return [{"job_id": "j_y", "step": "10_smart", "priority": 100,
                         "enqueued_at": 1747483200.0, "tags": [], "require_tags": []}]
            return []

        redis_mock.get_queue_info.side_effect = _info
        redis_mock.list_queue.side_effect = _list

        r = await client.get("/api/queue")
        assert r.status_code == 200
        ai = next(p for p in r.json()["pools"] if p["name"] == "ai")
        assert ai["queued_count"] == 1 and ai["queued_shown"] == 1
        t = ai["queued"][0]
        assert t["state"] == "queued"
        assert t["title"] == "RLHF 综述"          # enrich:作业标题
        assert t["content_type"] == "paper"
        assert t["priority"] == 100
        assert t["enqueued_at"] == 1747483200.0

    @pytest.mark.asyncio
    async def test_running_from_steps(self, client, db):
        db.create_job(Job(id="j_r", content_type="video", pipeline="video", title="Transformer"))
        db.upsert_worker(Worker(id="ai-1", type="ai", pools=["ai"], hostname="office-pc",
                                first_seen=_utcnow(), last_heartbeat=_utcnow()))
        db.upsert_step(Step(job_id="j_r", name="10_smart", status=StepStatus.RUNNING,
                            pool="ai", worker_id="ai-1", started_at=_utcnow()))

        r = await client.get("/api/queue")
        ai = next(p for p in r.json()["pools"] if p["name"] == "ai")
        assert len(ai["running"]) == 1
        run = ai["running"][0]
        assert run["state"] == "running"
        assert run["title"] == "Transformer"
        assert run["pool"] == "ai"
        assert run["worker_hostname"] == "office-pc"
        assert run["started_at"] is not None

    @pytest.mark.asyncio
    async def test_pool_filter(self, client, db):
        r = await client.get("/api/queue?pool=ai")
        names = [p["name"] for p in r.json()["pools"]]
        assert names == ["ai"]

    @pytest.mark.asyncio
    async def test_orphan_running_pool(self, client, db):
        # step 的 pool 不在所选池范围 → 归入 (未归类) 兜底组,不静默丢失。
        db.create_job(Job(id="j_o", content_type="video", pipeline="video", title="X"))
        db.upsert_step(Step(job_id="j_o", name="99_weird", status=StepStatus.RUNNING,
                            pool="legacy_pool", worker_id=None, started_at=_utcnow()))
        r = await client.get("/api/queue?pool=ai")
        orphan = next((p for p in r.json()["pools"] if p["name"] == "(未归类)"), None)
        assert orphan is not None
        assert orphan["running"][0]["job_id"] == "j_o"
