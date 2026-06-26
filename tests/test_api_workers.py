"""tests for api/routes/workers.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from shared.models import Worker
from api.main import create_app


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def redis_mock():
    """默认无远程 worker；list_worker_ids 返回空表，worker_exists 不活。
    get_traffic 须返回真 dict(裸 AsyncMock 的 await→AsyncMock,.get() 又得 coroutine)。"""
    from tests.conftest import make_redis_mock
    rc = make_redis_mock()
    rc.list_worker_ids.return_value = []
    rc.worker_exists.return_value = False
    rc.get_worker_info.return_value = None
    return rc


@pytest.fixture
def app(db, test_config, redis_mock):
    return create_app(db=db, redis=redis_mock, config=test_config)


def _make_worker(db, status="idle", heartbeat=None, **kw):
    w = Worker(
        id=kw.pop("id", "cpu-test001"),
        type="cpu",
        pools=["cpu", "io"],
        hostname="test-host",
        status=status,
        first_seen=_utcnow(),
        last_heartbeat=heartbeat if heartbeat is not None else _utcnow(),
        **kw,
    )
    db.upsert_worker(w)
    return w


class TestWorkers:
    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        resp = await client.get("/api/workers")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_with_worker(self, client, db):
        _make_worker(db)
        resp = await client.get("/api/workers")
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == "cpu-test001"

    @pytest.mark.asyncio
    async def test_get_worker(self, client, db):
        _make_worker(db)
        resp = await client.get("/api/workers/cpu-test001")
        assert resp.status_code == 200
        assert resp.json()["hostname"] == "test-host"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/workers/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_worker(self, client, db):
        _make_worker(db)
        resp = await client.put("/api/workers/cpu-test001", json={"status": "paused"})
        assert resp.status_code == 200
        # admin_status 列写入 paused；公共状态(在线+paused)也是 paused
        row = db._conn.execute(
            "SELECT admin_status FROM workers WHERE id=?", ("cpu-test001",)
        ).fetchone()
        assert row["admin_status"] == "paused"
        assert db.get_worker("cpu-test001").status == "paused"

    @pytest.mark.asyncio
    async def test_delete_offline_worker(self, client, db):
        old = _utcnow() - timedelta(minutes=2)
        _make_worker(db, heartbeat=old)
        resp = await client.delete("/api/workers/cpu-test001")
        assert resp.status_code == 204
        assert db.get_worker("cpu-test001") is None

    @pytest.mark.asyncio
    async def test_delete_online_worker_requires_force(self, client, db):
        _make_worker(db)  # 刚心跳 -> online，不带 force 不许删
        resp = await client.delete("/api/workers/cpu-test001")
        assert resp.status_code == 409
        assert db.get_worker("cpu-test001") is not None
        resp = await client.delete("/api/workers/cpu-test001?force=true")
        assert resp.status_code == 204
        assert db.get_worker("cpu-test001") is None


class TestTimestampSerialization:
    """UTC 全量迁移：API 序列化的时间戳必须带 UTC 标记(Z)，让浏览器无歧义解析，
    根治'容器跑 UTC、浏览器 UTC+8 → 刚心跳的 worker 被看成 8 小时前 → 误判离线'。"""

    @pytest.mark.asyncio
    async def test_list_timestamps_carry_utc_z(self, client, db):
        from datetime import datetime, timezone

        w = Worker(
            id="cpu-ts", type="cpu", status="idle",
            first_seen=datetime.now(timezone.utc),
            last_heartbeat=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        db.upsert_worker(w)
        resp = await client.get("/api/workers")
        assert resp.status_code == 200
        item = next(x for x in resp.json() if x["id"] == "cpu-ts")
        for field in ("first_seen", "last_heartbeat", "started_at"):
            assert item[field] is not None
            assert item[field].endswith("Z"), f"{field}={item[field]} 缺 UTC 标记"

    @pytest.mark.asyncio
    async def test_get_timestamp_carries_utc_z_for_legacy_naive(self, client, db):
        """旧库里 naive 时间串经详情接口序列化时也补成带 Z 的 UTC。"""
        db._conn.execute(
            "INSERT INTO workers (id, type, status, first_seen, last_heartbeat) "
            "VALUES (?,?,?,?,?)",
            ("cpu-legacy", "cpu", "offline",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        db._conn.commit()
        resp = await client.get("/api/workers/cpu-legacy")
        assert resp.status_code == 200
        body = resp.json()
        assert body["first_seen"] == "2026-01-01T00:00:00Z"
        assert body["last_heartbeat"] == "2026-01-01T00:00:00Z"


class TestStatusSemantics:
    """状态语义后端权威：API 返回的 status 由心跳新鲜度衍生，不信存量 status 列。"""

    @pytest.mark.asyncio
    async def test_fresh_idle_is_online_idle(self, client, db):
        _make_worker(db, id="w-idle", status="idle")
        body = (await client.get("/api/workers/w-idle")).json()
        assert body["status"] == "online-idle"

    @pytest.mark.asyncio
    async def test_fresh_busy_is_online_busy(self, client, db):
        _make_worker(db, id="w-busy", status="busy", current_job="j1")
        body = (await client.get("/api/workers/w-busy")).json()
        assert body["status"] == "online-busy"

    @pytest.mark.asyncio
    async def test_recent_gap_is_offline(self, client, db):
        _make_worker(db, id="w-off", heartbeat=_utcnow() - timedelta(minutes=2))
        body = (await client.get("/api/workers/w-off")).json()
        assert body["status"] == "offline"

    @pytest.mark.asyncio
    async def test_long_gap_is_stale(self, client, db):
        _make_worker(db, id="w-stale", heartbeat=_utcnow() - timedelta(minutes=30))
        body = (await client.get("/api/workers/w-stale")).json()
        assert body["status"] == "stale"

    @pytest.mark.asyncio
    async def test_online_paused_overlay(self, client, db):
        _make_worker(db, id="w-paused", admin_status="paused")
        body = (await client.get("/api/workers/w-paused")).json()
        assert body["status"] == "paused"

    @pytest.mark.asyncio
    async def test_redis_fresh_overrides_stale_db_heartbeat_list(self, client, db, redis_mock):
        # 回归:worker 空闲时 db.last_heartbeat 可能不刷新而过期(单看 db 判 offline),但 Redis 心跳
        # 新鲜。列表必须用 Redis 的新鲜心跳覆盖 db 已有 worker → 在线;否则在线 worker 全被误标离线。
        _make_worker(db, id="cpu-rescue", heartbeat=_utcnow() - timedelta(minutes=5))
        redis_mock.list_worker_ids.return_value = ["cpu-rescue"]
        redis_mock.get_worker_info.return_value = {
            "type": "cpu", "status": "idle", "last_heartbeat": _utcnow().isoformat(),
        }
        w = next(x for x in (await client.get("/api/workers")).json() if x["id"] == "cpu-rescue")
        assert w["status"] == "online-idle"

    @pytest.mark.asyncio
    async def test_redis_fresh_overrides_stale_db_heartbeat_detail(self, client, db, redis_mock):
        _make_worker(db, id="cpu-rescue", heartbeat=_utcnow() - timedelta(minutes=5))
        redis_mock.list_worker_ids.return_value = ["cpu-rescue"]
        redis_mock.get_worker_info.return_value = {
            "type": "cpu", "status": "idle", "last_heartbeat": _utcnow().isoformat(),
        }
        body = (await client.get("/api/workers/cpu-rescue")).json()
        assert body["status"] == "online-idle"

    @pytest.mark.asyncio
    async def test_list_online_count(self, client, db):
        _make_worker(db, id="w1", status="idle")
        _make_worker(db, id="w2", status="busy", current_job="j")
        _make_worker(db, id="w3", heartbeat=_utcnow() - timedelta(minutes=30))
        items = (await client.get("/api/workers")).json()
        by_id = {w["id"]: w for w in items}
        online = [w for w in items if w["status"].startswith("online")]
        assert len(online) == 2
        assert by_id["w3"]["status"] == "stale"


class TestPauseWritesRedis:
    """暂停真生效：PUT status=paused 必须同步写 Redis admin_status(worker 认领读 Redis 判暂停)。"""

    @pytest.mark.asyncio
    async def test_put_status_writes_redis(self, client, db, redis_mock):
        _make_worker(db, id="w-d")
        resp = await client.put("/api/workers/w-d", json={"status": "paused"})
        assert resp.status_code == 200
        redis_mock.set_worker_field.assert_any_call("w-d", "admin_status", "paused")

    @pytest.mark.asyncio
    async def test_put_tags_writes_db_and_redis(self, client, db, redis_mock):
        _make_worker(db, id="w-t")
        resp = await client.put(
            "/api/workers/w-t", json={"tags": ["vision", "claude-cli"]}
        )
        assert resp.status_code == 200
        assert db.get_worker("w-t").tags == {"vision", "claude-cli"}
        redis_mock.set_worker_field.assert_any_call("w-t", "tags", "claude-cli,vision")

    @pytest.mark.asyncio
    async def test_delete_removes_redis_key(self, client, db, redis_mock):
        _make_worker(db, id="w-del", heartbeat=_utcnow() - timedelta(minutes=2))
        resp = await client.delete("/api/workers/w-del")
        assert resp.status_code == 204
        redis_mock.delete_worker.assert_awaited_with("w-del")


class TestRemoteWorker:
    """仅注册在 Redis 的远程 worker：状态按心跳衍生、累计统计从 hash 读(非硬编码 0)。"""

    @pytest.mark.asyncio
    async def test_remote_worker_merged_with_stats(self, client, redis_mock):
        redis_mock.list_worker_ids.return_value = ["gpu-remote"]
        redis_mock.get_worker_info.return_value = {
            "type": "gpu",
            "pools": "gpu,io",
            "tags": "vision,gpu",
            "status": "idle",
            "hostname": "gpu-box",
            "last_heartbeat": _utcnow().isoformat(),
            "started_at": _utcnow().isoformat(),
            "tasks_completed": "7",
            "tasks_failed": "1",
            "total_duration_sec": "42.5",
        }
        items = (await client.get("/api/workers")).json()
        w = next(x for x in items if x["id"] == "gpu-remote")
        assert w["status"] == "online-idle"
        assert w["tasks_completed"] == 7
        assert w["tasks_failed"] == 1
        assert w["total_duration_sec"] == 42.5
        assert w["tags"] == ["vision", "gpu"]

    @pytest.mark.asyncio
    async def test_remote_worker_stale_heartbeat_offline(self, client, redis_mock):
        redis_mock.list_worker_ids.return_value = ["gpu-remote"]
        redis_mock.get_worker_info.return_value = {
            "type": "gpu", "status": "busy",
            "last_heartbeat": (_utcnow() - timedelta(minutes=2)).isoformat(),
        }
        items = (await client.get("/api/workers")).json()
        w = next(x for x in items if x["id"] == "gpu-remote")
        # 不信 Redis 自报的 busy，按心跳判 offline
        assert w["status"] == "offline"

    @pytest.mark.asyncio
    async def test_delete_remote_only_worker(self, client, db, redis_mock):
        redis_mock.worker_exists.return_value = True
        redis_mock.list_worker_ids.return_value = []
        # DB 里没有这个 worker，但 Redis 活着 -> 需 force 删，并清 Redis key
        resp = await client.delete("/api/workers/gpu-remote")
        assert resp.status_code == 204  # 不在 DB 视作可删
        redis_mock.delete_worker.assert_awaited_with("gpu-remote")


class TestRegistrationToken:
    @pytest.mark.asyncio
    async def test_mint_token(self, client, redis_mock):
        resp = await client.post("/api/workers/registration-token")
        assert resp.status_code == 200
        token = resp.json()["token"]
        assert token.startswith("flw-")
        assert resp.json()["expires_in_sec"] == 86400  # 默认 24h 过期
        redis_mock.set_registration_token.assert_awaited_with(token, ttl_sec=86400)


class TestWorkerTasks:
    @pytest.mark.asyncio
    async def test_worker_task_history(self, client, db):
        from shared.models import Job, JobStatus, Step, StepStatus

        _make_worker(db, id="w-hist")
        db.create_job(Job(
            id="job-1", content_type="video", pipeline="video",
            status=JobStatus.PROCESSING,
        ))
        db.upsert_step(Step(
            job_id="job-1", name="download", pool="io",
            status=StepStatus.DONE, worker_id="w-hist",
            started_at=_utcnow(), finished_at=_utcnow(), duration_sec=3.2,
        ))
        resp = await client.get("/api/workers/w-hist/tasks")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["job_id"] == "job-1"
        assert rows[0]["step"] == "download"
        assert rows[0]["status"] == "done"
