"""tests for transport — RedisTransport(fakeredis) + GatewayTransport(mock httpx)。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import fakeredis.aioredis

from shared.db import Database
from shared.redis_client import RedisClient
from worker.transport import RedisTransport
from worker.gateway_transport import GatewayTransport


# ── Fixtures ──


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
async def redis():
    client = RedisClient.__new__(RedisClient)
    client._url = "redis://fake"
    client._redis = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    yield client
    await client.close()


def make_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


REGISTER_ARGS = dict(
    worker_type="cpu", pools=["cpu", "io"],
    tags={"vision"}, reject_tags={"private"},
    hostname="host-1", now=datetime.now(timezone.utc),
)


# ── RedisTransport ──


class TestRedisTransportRegister:
    @pytest.mark.asyncio
    async def test_returns_id_and_writes_redis_and_db(self, redis, db):
        transport = RedisTransport(redis, db)
        returned = await transport.register("w_abc", **REGISTER_ARGS)

        assert returned == "w_abc"
        info = await redis.get_worker_info("w_abc")
        assert info is not None
        assert info["type"] == "cpu"
        assert db.get_worker("w_abc") is not None


# ── RedisTransport 粗粒度认领/上报(P3a) ──


WORKER_ID = "w_t1"
POOL_LIMITS = {"cpu": 3, "io": 999, "scene": 1}


async def _registered(redis, db):
    """注册一个 worker 并返回 transport(让 _worker_id 就位)。"""
    t = RedisTransport(redis, db)
    await t.register(WORKER_ID, **REGISTER_ARGS)
    return t


class TestRequestStep:
    @pytest.mark.asyncio
    async def test_claims_ready_step(self, redis, db):
        t = await _registered(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "lecture", "style_tags": '["formal"]'})

        claim = await t.request_step(WORKER_ID, ["cpu"], POOL_LIMITS,
                                     {"vision"}, {"private"})

        assert claim is not None
        assert claim["job_id"] == "j1"
        assert claim["step"] == "A"
        assert claim["pool"] == "cpu"
        # 认领只返回最小 claim;pipeline/domain/style_tags 由 worker 在 execute 内回读。
        assert claim["exec_id"].startswith(f"{WORKER_ID}:")
        # CAS 把状态从 ready 推进到 running
        assert await redis.get_step_status("j1", "A") == "running"
        # 槽位已占用
        assert await redis.get_pool_count("cpu") == 1

    @pytest.mark.asyncio
    async def test_scene_freezes_cpu(self, redis, db):
        t = await _registered(redis, db)
        await redis.enqueue_step("scene", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await t.request_step(WORKER_ID, ["scene"], POOL_LIMITS,
                                     {"vision"}, set())

        assert claim is not None
        assert claim["pool"] == "scene"
        assert await redis.is_pool_frozen("cpu") is True

    @pytest.mark.asyncio
    async def test_draining_returns_none(self, redis, db):
        t = await _registered(redis, db)
        await redis.set_worker_field(WORKER_ID, "status", "draining")
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await t.request_step(WORKER_ID, ["cpu"], POOL_LIMITS,
                                     {"vision"}, set())
        assert claim is None

    @pytest.mark.asyncio
    async def test_tag_mismatch_returns_to_queue(self, redis, db):
        t = await _registered(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", ["heavy"], priority=0,
                                 require_tags=["heavy"])

        claim = await t.request_step(WORKER_ID, ["cpu"], POOL_LIMITS,
                                     {"vision"}, set())

        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1  # 放回队列
        # 占用的槽位已释放
        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_reject_tag_returns_to_queue(self, redis, db):
        t = await _registered(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", ["vision", "private"], priority=0,
                                 require_tags=["vision"])

        claim = await t.request_step(WORKER_ID, ["cpu"], POOL_LIMITS,
                                     {"vision"}, {"private"})

        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 1

    @pytest.mark.asyncio
    async def test_max_tries_returns_none(self, redis, db):
        t = await _registered(redis, db)
        for i in range(6):
            await redis.enqueue_step("cpu", f"j_{i}", "A", ["exotic"], priority=0,
                                     require_tags=["exotic"])

        claim = await t.request_step(WORKER_ID, ["cpu"], POOL_LIMITS,
                                     {"vision"}, set())
        assert claim is None
        queue = await redis.get_queue_info("cpu")
        assert queue["length"] == 6

    @pytest.mark.asyncio
    async def test_cas_lost_releases_slot_and_continues(self, redis, db):
        t = await _registered(redis, db)
        await redis.enqueue_step("scene", "j1", "A", [], priority=0)
        # 状态已是 running → CAS ready->running 失败。
        await redis.set_step_status("j1", "A", "running")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await t.request_step(WORKER_ID, ["scene"], POOL_LIMITS,
                                     {"vision"}, set())

        assert claim is None
        # CAS 失败路径:槽位释放 + scene 触发的 cpu 冻结被解除
        assert await redis.get_pool_count("scene") == 0
        assert await redis.is_pool_frozen("cpu") is False


class TestReportDone:
    @pytest.mark.asyncio
    async def test_publishes_writes_and_increments(self, redis, db):
        import asyncio as _asyncio
        from shared.models import Job, Step, StepStatus

        t = await _registered(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))

        claim = {"job_id": "j1", "step": "A", "pool": "cpu",
                 "exec_id": f"{WORKER_ID}:1"}

        events = []

        async def capture():
            async for msg in redis.subscribe("step_completed"):
                events.append(msg)
                break

        listener = _asyncio.create_task(capture())
        await _asyncio.sleep(0.05)
        await t.report_done(claim, 12.34, time.time() - 12.34)
        await _asyncio.wait_for(listener, timeout=2.0)

        assert len(events) == 1
        assert events[0]["status"] == "done"
        assert events[0]["duration"] == 12.3
        assert events[0]["exec_id"] == f"{WORKER_ID}:1"
        assert events[0]["worker"] == WORKER_ID

        db_step = db.get_steps("j1")[0]
        assert db_step.status == StepStatus.DONE
        assert db_step.worker_id == WORKER_ID

        db_worker = db.get_worker(WORKER_ID)
        assert db_worker.tasks_completed == 1


class TestReportFailed:
    @pytest.mark.asyncio
    async def test_count_stats_true_includes_exec_id_and_increments(self, redis, db):
        import asyncio as _asyncio
        from shared.models import Job, Step, StepStatus

        t = await _registered(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))

        claim = {"job_id": "j1", "step": "A", "pool": "cpu",
                 "exec_id": f"{WORKER_ID}:9"}

        topic_events, ws_events = [], []

        async def capture_topic():
            async for msg in redis.subscribe("step_failed"):
                topic_events.append(msg)
                break

        async def capture_ws():
            async for msg in redis.subscribe("events:j1"):
                if msg.get("event") == "step_failed":
                    ws_events.append(msg)
                    break

        l1 = _asyncio.create_task(capture_topic())
        l2 = _asyncio.create_task(capture_ws())
        await _asyncio.sleep(0.05)
        long_err = "x" * 600
        await t.report_failed(claim, long_err, "segfault", 5.0,
                              time.time() - 5.0, count_stats=True)
        await _asyncio.wait_for(l1, timeout=2.0)
        await _asyncio.wait_for(l2, timeout=2.0)

        # rc!=0 分支:topic payload 带 exec_id
        assert topic_events[0]["exec_id"] == f"{WORKER_ID}:9"
        assert topic_events[0]["error_type"] == "segfault"
        # events 用 error[:200]
        assert len(ws_events[0]["error"]) == 200

        db_step = db.get_steps("j1")[0]
        assert db_step.status == StepStatus.FAILED

        db_worker = db.get_worker(WORKER_ID)
        assert db_worker.tasks_failed == 1

    @pytest.mark.asyncio
    async def test_count_stats_false_skips_increment_and_omits_exec_id(self, redis, db):
        import asyncio as _asyncio
        from shared.models import Job, Step, StepStatus

        t = await _registered(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))

        claim = {"job_id": "j1", "step": "A", "pool": "cpu",
                 "exec_id": f"{WORKER_ID}:9"}

        topic_events, ws_events = [], []

        async def capture_topic():
            async for msg in redis.subscribe("step_failed"):
                topic_events.append(msg)
                break

        async def capture_ws():
            async for msg in redis.subscribe("events:j1"):
                if msg.get("event") == "step_failed":
                    ws_events.append(msg)
                    break

        l1 = _asyncio.create_task(capture_topic())
        l2 = _asyncio.create_task(capture_ws())
        await _asyncio.sleep(0.05)
        await t.report_failed(claim, "timeout", "timeout", 3.0,
                              time.time() - 3.0, count_stats=False)
        await _asyncio.wait_for(l1, timeout=2.0)
        await _asyncio.wait_for(l2, timeout=2.0)

        # timeout/异常分支:topic payload 不带 exec_id
        assert "exec_id" not in topic_events[0]
        # events 用完整 error(不截断/不同于 rc!=0)
        assert ws_events[0]["error"] == "timeout"

        db_step = db.get_steps("j1")[0]
        assert db_step.status == StepStatus.FAILED

        # count_stats=False → 不累加 failed
        db_worker = db.get_worker(WORKER_ID)
        assert db_worker.tasks_failed == 0


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_slot_and_unfreeze_scene(self, redis, db):
        t = await _registered(redis, db)
        await redis.try_acquire_slot("scene", limit=1)
        await redis.freeze_pool("cpu")

        await t.release({"job_id": "j1", "step": "A", "pool": "scene",
                         "exec_id": "e"})

        assert await redis.get_pool_count("scene") == 0
        assert await redis.is_pool_frozen("cpu") is False
        # worker 回到 idle
        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"

    @pytest.mark.asyncio
    async def test_release_non_scene_does_not_unfreeze(self, redis, db):
        t = await _registered(redis, db)
        await redis.try_acquire_slot("cpu", limit=3)
        await redis.freeze_pool("cpu")  # 外部冻结,非 scene 释放不应解冻

        await t.release({"job_id": "j1", "step": "A", "pool": "cpu",
                         "exec_id": "e"})

        assert await redis.get_pool_count("cpu") == 0
        assert await redis.is_pool_frozen("cpu") is True


# ── GatewayTransport ──


def make_gateway(redis, db, tmp_path, *, registration_token="mnw-tok"):
    """构造 GatewayTransport,并注入 mock httpx client(不建真实连接)。"""
    id_file = tmp_path / ".worker_id"
    gw = GatewayTransport(
        "https://mnemo.example",
        registration_token=registration_token,
        id_file=str(id_file),
        inner=RedisTransport(redis, db),
    )
    client = MagicMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    gw._client = client
    return gw, id_file


class TestGatewayRegister:
    @pytest.mark.asyncio
    async def test_sends_token_stores_worker_token_and_persists_id(
        self, redis, db, tmp_path,
    ):
        gw, id_file = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "wt-secret"},
        )

        returned = await gw.register("w_local", **REGISTER_ARGS)

        assert returned == "w_srv"
        # 注册 token 通过 Authorization 头下发
        _, kwargs = gw._client.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer mnw-tok"
        assert kwargs["json"]["worker_id"] == "w_local"
        assert kwargs["json"]["tags"] == ["vision"]
        # 服务端回的 worker_token 被记下,供后续心跳鉴权
        assert gw._worker_token == "wt-secret"
        # 服务端回的 worker_id 落盘
        assert id_file.read_text().strip() == "w_srv"
        # 影子写:redis/db 也有这行
        assert await redis.get_worker_info("w_srv") is not None

    @pytest.mark.asyncio
    async def test_reuses_cached_id_on_second_register(
        self, redis, db, tmp_path,
    ):
        gw, id_file = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_first", "worker_token": "wt1"},
        )
        await gw.register("w_local", **REGISTER_ARGS)
        assert id_file.read_text().strip() == "w_first"

        # 第二次注册:缓存 id 优先于传入的 id
        gw2, _ = make_gateway(redis, db, tmp_path)
        gw2._client.post.return_value = make_response(
            json_data={"worker_token": "wt2"},
        )
        returned = await gw2.register("w_other", **REGISTER_ARGS)

        _, kwargs = gw2._client.post.call_args
        assert kwargs["json"]["worker_id"] == "w_first"
        assert returned == "w_first"


class TestGatewayHeartbeat:
    @pytest.mark.asyncio
    async def test_401_falls_through_to_inner_without_crash(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=401)
        inner_hb = AsyncMock()
        monkeypatch.setattr(gw._inner, "heartbeat", inner_hb)

        await gw.heartbeat("w1")

        inner_hb.assert_awaited_once_with("w1")

    @pytest.mark.asyncio
    async def test_httpx_error_falls_back_to_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        import httpx

        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.side_effect = httpx.ConnectError("down")
        inner_hb = AsyncMock()
        monkeypatch.setattr(gw._inner, "heartbeat", inner_hb)

        await gw.heartbeat("w1")

        inner_hb.assert_awaited_once_with("w1")

    @pytest.mark.asyncio
    async def test_posts_worker_id_and_current_status(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        monkeypatch.setattr(gw._inner, "heartbeat", AsyncMock())
        monkeypatch.setattr(gw._inner, "update_status", AsyncMock())

        # 心跳须带 worker_id + update_status 记下的当前状态(不能漏 body 导致 422)。
        await gw.update_status("w1", "busy", "job1", "01_scene")
        await gw.heartbeat("w1")

        _, kwargs = gw._client.post.call_args
        assert kwargs["json"] == {
            "worker_id": "w1", "status": "busy",
            "current_job": "job1", "current_step": "01_scene",
        }


class TestGatewayDelegation:
    @pytest.mark.asyncio
    async def test_dequeue_delegates_to_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        inner_dequeue = AsyncMock(return_value=("raw", {"job_id": "j1"}, 1.0))
        monkeypatch.setattr(gw._inner, "dequeue_step_raw", inner_dequeue)

        result = await gw.dequeue_step_raw("cpu")

        inner_dequeue.assert_awaited_once_with("cpu")
        assert result == ("raw", {"job_id": "j1"}, 1.0)

    @pytest.mark.asyncio
    async def test_update_status_offline_posts_then_delegates(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        inner_update = AsyncMock()
        monkeypatch.setattr(gw._inner, "update_status", inner_update)

        await gw.update_status("w1", "offline")

        gw._client.post.assert_awaited_once()
        _, kwargs = gw._client.post.call_args
        assert kwargs["json"] == {"worker_id": "w1"}
        inner_update.assert_awaited_once_with("w1", "offline", "", "")


class TestGatewayCoarseDelegation:
    """P3a 影子模式:粗粒度认领/上报原样委派内层。"""

    @pytest.mark.asyncio
    async def test_request_step_delegates_to_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e",
                 "pipeline": "test", "domain": "general", "style_tags": []}
        inner = AsyncMock(return_value=claim)
        monkeypatch.setattr(gw._inner, "request_step", inner)

        result = await gw.request_step("w1", ["cpu"], {"cpu": 3},
                                       {"vision"}, {"private"})

        inner.assert_awaited_once_with("w1", ["cpu"], {"cpu": 3},
                                       {"vision"}, {"private"})
        assert result == claim

    @pytest.mark.asyncio
    async def test_report_done_delegates_to_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        inner = AsyncMock()
        monkeypatch.setattr(gw._inner, "report_done", inner)
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        await gw.report_done(claim, 1.5, 100.0)

        inner.assert_awaited_once_with(claim, 1.5, 100.0)

    @pytest.mark.asyncio
    async def test_report_failed_delegates_to_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        inner = AsyncMock()
        monkeypatch.setattr(gw._inner, "report_failed", inner)
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        await gw.report_failed(claim, "boom", "processing", 2.0, 50.0, False)

        inner.assert_awaited_once_with(claim, "boom", "processing", 2.0, 50.0, False)

    @pytest.mark.asyncio
    async def test_release_delegates_to_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        inner = AsyncMock()
        monkeypatch.setattr(gw._inner, "release", inner)
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        await gw.release(claim)

        inner.assert_awaited_once_with(claim)
