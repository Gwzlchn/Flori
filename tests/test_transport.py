"""worker transport 单测:RedisTransport 用 fakeredis,GatewayTransport 用 mock httpx。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import make_fakeredis
from shared.db import Database
from worker.transport import RedisTransport
from worker.gateway_transport import GatewayTransport


# Fixtures


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
async def redis():
    client = make_fakeredis()
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


# RedisTransport


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


# RedisTransport 粗粒度认领/上报


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
    async def test_claim_does_not_freeze(self, redis, db):
        t = await _registered(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await redis.set_step_status("j1", "A", "ready")
        await redis.init_job("j1", "test", {"domain": "general", "style_tags": "[]"})

        claim = await t.request_step(WORKER_ID, ["cpu"], POOL_LIMITS,
                                     {"vision"}, set())

        assert claim is not None
        assert claim["pool"] == "cpu"
        # 认领不冻结其他池。
        assert await redis.is_pool_frozen("cpu") is False

    @pytest.mark.asyncio
    async def test_paused_returns_none(self, redis, db):
        t = await _registered(redis, db)
        await redis.set_worker_field(WORKER_ID, "admin_status", "paused")
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
        # CAS 失败路径:槽位释放,不留下池冻结
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
    async def test_release_slot_and_idle(self, redis, db):
        t = await _registered(redis, db)
        await redis.try_acquire_slot("cpu", 1, "e")   # holder = 下方 release 的 exec_id

        await t.release({"job_id": "j1", "step": "A", "pool": "cpu",
                         "exec_id": "e"})

        assert await redis.get_pool_count("cpu") == 0
        # worker 回到 idle
        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"

    @pytest.mark.asyncio
    async def test_release_non_scene_does_not_unfreeze(self, redis, db):
        t = await _registered(redis, db)
        await redis.try_acquire_slot("cpu", 3, "e")   # holder = 下方 release 的 exec_id
        await redis.freeze_pool("cpu")  # 外部冻结,非 scene 释放不应解冻

        await t.release({"job_id": "j1", "step": "A", "pool": "cpu",
                         "exec_id": "e"})

        assert await redis.get_pool_count("cpu") == 0
        assert await redis.is_pool_frozen("cpu") is True


# GatewayTransport


def make_gateway(redis, db, tmp_path, *, registration_token="flw-tok"):
    """构造 GatewayTransport,并注入 mock httpx client(不建真实连接)。"""
    id_file = tmp_path / ".worker_id"
    gw = GatewayTransport(
        "https://flori.example",
        registration_token=registration_token,
        id_file=str(id_file),
        inner=RedisTransport(redis, db),
    )
    client = MagicMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    gw._client = client
    return gw, id_file


def make_pure_gateway(tmp_path, *, registration_token="flw-tok"):
    """纯网关模式:inner=None(无 redis/db),只出站 HTTPS。"""
    id_file = tmp_path / ".worker_id"
    gw = GatewayTransport(
        "https://flori.example",
        registration_token=registration_token,
        id_file=str(id_file),
        inner=None,
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
        assert kwargs["headers"]["Authorization"] == "Bearer flw-tok"
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
    async def test_401_raises_worker_auth_rejected(
        self, redis, db, tmp_path, monkeypatch,
    ):
        # 心跳被 401(per-worker token 失效)→ 抛 WorkerAuthRejected,交主循环走重注册/退避/6h自杀;
        # 认证失败优先上抛,不 fall-through 到 inner。
        from worker.transport import WorkerAuthRejected
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=401)
        inner_hb = AsyncMock()
        monkeypatch.setattr(gw._inner, "heartbeat", inner_hb)

        with pytest.raises(WorkerAuthRejected):
            await gw.heartbeat("w1")

        inner_hb.assert_not_awaited()

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

        inner_hb.assert_awaited_once_with("w1", load=None)

    @pytest.mark.asyncio
    async def test_posts_worker_id_and_current_status(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        monkeypatch.setattr(gw._inner, "heartbeat", AsyncMock())
        monkeypatch.setattr(gw._inner, "update_status", AsyncMock())

        # 心跳须带当前状态与已应用配置版本,否则 runner API 无法判定漂移。
        await gw.update_status("w1", "busy", "job1", "03_scene")
        await gw.heartbeat("w1")

        _, kwargs = gw._client.post.call_args
        assert kwargs["json"] == {
            "worker_id": "w1", "status": "busy",
            "current_job": "job1", "current_step": "03_scene",
            "applied_cfg_rev": 0,
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


class TestGatewayCoarseHTTP:
    """粗粒度认领/上报走 gateway HTTP,不委派内层,避免经 redis 双重认领。"""

    @pytest.mark.asyncio
    async def test_request_step_posts_and_parses_claim(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._worker_token = "wt"
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e",
                 "pipeline": "test", "domain": "general", "style_tags": []}
        gw._client.post.return_value = make_response(json_data={"claim": claim})

        result = await gw.request_step("w1", ["cpu"], {"cpu": 3},
                                       {"vision"}, {"private"})

        assert result == claim
        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/jobs/request"
        assert kwargs["headers"]["Authorization"] == "Bearer wt"
        assert kwargs["json"] == {
            "pools": ["cpu"], "pool_limits": {"cpu": 3},
            "tags": ["vision"], "reject_tags": ["private"],
        }

    @pytest.mark.asyncio
    async def test_request_step_null_claim_returns_none(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(json_data={"claim": None})

        result = await gw.request_step("w1", ["cpu"], {"cpu": 3}, set(), set())
        assert result is None

    @pytest.mark.asyncio
    async def test_request_step_httpx_error_returns_none_no_inner(
        self, redis, db, tmp_path, monkeypatch,
    ):
        import httpx

        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.side_effect = httpx.ConnectError("down")
        inner = AsyncMock()
        monkeypatch.setattr(gw._inner, "request_step", inner)

        result = await gw.request_step("w1", ["cpu"], {"cpu": 3}, set(), set())

        assert result is None
        inner.assert_not_awaited()  # 绝不退回内层,否则经 redis 双重认领

    @pytest.mark.asyncio
    async def test_report_done_posts_complete(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._worker_token = "wt"
        gw._client.post.return_value = make_response()
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        await gw.report_done(claim, 1.5, 100.0)

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/jobs/j1/steps/A/complete"
        assert kwargs["json"] == {
            "pool": "cpu", "exec_id": "e", "duration": 1.5, "started_at": 100.0,
        }

    @pytest.mark.asyncio
    async def test_report_failed_posts_fail(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        await gw.report_failed(claim, "boom", "processing", 2.0, 50.0, False)

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/jobs/j1/steps/A/fail"
        assert kwargs["json"] == {
            "pool": "cpu", "exec_id": "e", "error": "boom",
            "error_type": "processing", "duration": 2.0, "started_at": 50.0,
            "count_stats": False,
        }

    @pytest.mark.asyncio
    async def test_release_posts_release(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        await gw.release(claim)

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/jobs/j1/steps/A/release"
        assert kwargs["json"] == {"pool": "cpu", "exec_id": "e"}

    @pytest.mark.asyncio
    async def test_record_usage_posts_usage(self, redis, db, tmp_path):
        from shared.models import AIUsage

        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        usage = AIUsage(exec_id="e1", provider="anthropic", model="claude",
                        job_id="j1", step="A", input_tokens=10, output_tokens=20,
                        cost_usd=0.5, duration_sec=1.2, cached=False)

        await gw.record_ai_usage(usage)

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/usage"
        assert kwargs["json"]["exec_id"] == "e1"
        assert kwargs["json"]["input_tokens"] == 10
        # 计费接缝:成本/输出 token 必须随 POST body 上报(否则服务端记 0,金额静默丢失)。
        assert kwargs["json"]["output_tokens"] == 20
        assert kwargs["json"]["cost_usd"] == 0.5
        assert "created_at" not in kwargs["json"]

    @pytest.mark.asyncio
    async def test_publish_step_event_maps_progress(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()

        await gw.publish_step_event("events:j1", {"event": "step_log", "line": "x"})

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/jobs/j1/steps/_/progress"
        assert kwargs["json"] == {"payload": {"event": "step_log", "line": "x"}}


class TestGatewayPureMode:
    """inner=None(纯网关零隧道)——无影子写/无内层退回/委派返回安全默认值。"""

    @pytest.mark.asyncio
    async def test_register_returns_server_id_no_shadow_write(self, redis, tmp_path):
        gw, id_file = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "wt-secret"},
        )

        returned = await gw.register("w_local", **REGISTER_ARGS)

        assert returned == "w_srv"
        assert gw._worker_token == "wt-secret"
        assert id_file.read_text().strip() == "w_srv"
        # 无内层 → redis 不应有这行(无影子写)
        assert await redis.get_worker_info("w_srv") is None

    @pytest.mark.asyncio
    async def test_worker_token_property_exposes_token(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "wt-xyz"},
        )
        await gw.register("w_local", **REGISTER_ARGS)
        # GatewayStorage 经此属性拿 per-worker token
        assert gw.worker_token == "wt-xyz"

    @pytest.mark.asyncio
    async def test_heartbeat_no_inner_fallback_no_crash_on_httpx_error(self, tmp_path):
        import httpx

        gw, _ = make_pure_gateway(tmp_path)
        gw._client.post.side_effect = httpx.ConnectError("down")
        # 无内层可退回:只 log,不抛
        await gw.heartbeat("w1")

    @pytest.mark.asyncio
    async def test_get_worker_status_returns_none(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        assert await gw.get_worker_status("w1") is None

    @pytest.mark.asyncio
    async def test_offline_posts_then_no_inner_delegate(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response()
        # offline 仍打 gateway;无内层委派,不崩
        await gw.update_status("w1", "offline")
        gw._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_defensive_delegators_safe_defaults(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        assert await gw.get_job_pipeline("j1") is None
        assert await gw.get_job_info("j1") == {}
        assert await gw.is_pool_frozen("cpu") is False
        assert await gw.dequeue_step_raw("cpu") is None
        # 无返回值的委派也不应抛
        await gw.release_slot("cpu", "h1")
        await gw.set_step_worker("j1", "A", "w1")

    @pytest.mark.asyncio
    async def test_close_without_inner(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        await gw.close()


class TestGatewayShadowWriteWithInner:
    """对照:inner 存在时影子写仍发生(混合模式不退化)。"""

    @pytest.mark.asyncio
    async def test_register_shadow_writes_redis(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "wt"},
        )
        await gw.register("w_local", **REGISTER_ARGS)
        assert await redis.get_worker_info("w_srv") is not None


# RedisTransport 生命周期 / 心跳(直连转调)


class TestRedisTransportLifecycle:
    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_redis_and_db(self, redis, db):
        t = await _registered(redis, db)
        before = db.get_worker(WORKER_ID).last_heartbeat

        await t.heartbeat(WORKER_ID)

        # Redis key 仍存在(心跳续命),DB last_heartbeat 被刷新(>=注册时刻)
        assert await redis.get_worker_info(WORKER_ID) is not None
        after = db.get_worker(WORKER_ID).last_heartbeat
        assert after >= before

    @pytest.mark.asyncio
    async def test_update_status_writes_redis_fields_and_db(self, redis, db):
        t = await _registered(redis, db)

        await t.update_status(WORKER_ID, "busy", "j9", "03_scene")

        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "busy"
        assert info["current_job"] == "j9"
        assert info["current_step"] == "03_scene"
        w = db.get_worker(WORKER_ID)
        assert w.current_job == "j9"
        assert w.current_step == "03_scene"

    @pytest.mark.asyncio
    async def test_update_status_defaults_empty_job_and_step(self, redis, db):
        t = await _registered(redis, db)

        await t.update_status(WORKER_ID, "idle")

        info = await redis.get_worker_info(WORKER_ID)
        assert info["status"] == "idle"
        assert info["current_job"] == ""
        assert info["current_step"] == ""

    @pytest.mark.asyncio
    async def test_get_worker_status_reads_redis(self, redis, db):
        t = await _registered(redis, db)
        await redis.set_worker_field(WORKER_ID, "status", "busy")

        assert await t.get_worker_status(WORKER_ID) == "busy"

    @pytest.mark.asyncio
    async def test_get_worker_status_missing_returns_none(self, redis, db):
        t = RedisTransport(redis, db)
        assert await t.get_worker_status("nope") is None


# RedisTransport 资源池 / 队列(纯转调)


class TestRedisTransportPoolPassthrough:
    @pytest.mark.asyncio
    async def test_freeze_and_is_frozen_and_unfreeze(self, redis, db):
        t = RedisTransport(redis, db)

        assert await t.is_pool_frozen("cpu") is False
        await t.freeze_pool("cpu")
        assert await t.is_pool_frozen("cpu") is True
        await t.unfreeze_pool("cpu")
        assert await t.is_pool_frozen("cpu") is False

    @pytest.mark.asyncio
    async def test_try_acquire_slot_respects_limit(self, redis, db):
        t = RedisTransport(redis, db)

        assert await t.try_acquire_slot("cpu", 1, "h1") is True
        # 槽位已满 → 不同 holder 第二次失败
        assert await t.try_acquire_slot("cpu", 1, "h2") is False
        assert await redis.get_pool_count("cpu") == 1

    @pytest.mark.asyncio
    async def test_release_slot_decrements(self, redis, db):
        t = RedisTransport(redis, db)
        await t.try_acquire_slot("cpu", 3, "h1")
        assert await redis.get_pool_count("cpu") == 1

        await t.release_slot("cpu", "h1")

        assert await redis.get_pool_count("cpu") == 0

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self, redis, db):
        t = RedisTransport(redis, db)
        assert await t.dequeue_step_raw("cpu") is None

    @pytest.mark.asyncio
    async def test_enqueue_then_dequeue_roundtrip(self, redis, db):
        t = RedisTransport(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=5)

        raw, payload, score = await t.dequeue_step_raw("cpu")

        assert payload["job_id"] == "j1"
        assert payload["step"] == "A"
        assert score == 5
        # 队列已空
        assert await t.dequeue_step_raw("cpu") is None

    @pytest.mark.asyncio
    async def test_return_step_puts_back_on_queue(self, redis, db):
        t = RedisTransport(redis, db)
        await redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        raw, _, score = await t.dequeue_step_raw("cpu")
        # 取出后队列为空
        assert (await redis.get_queue_info("cpu"))["length"] == 0

        await t.return_step("cpu", raw, score)

        # 放回后队列又有一条
        assert (await redis.get_queue_info("cpu"))["length"] == 1


# RedisTransport 步骤状态机(纯转调)


class TestRedisTransportStepMachine:
    @pytest.mark.asyncio
    async def test_cas_step_status_success_and_failure(self, redis, db):
        t = RedisTransport(redis, db)
        await redis.set_step_status("j1", "A", "ready")

        # 期望匹配 → 推进成功
        assert await t.cas_step_status("j1", "A", "ready", "running") is True
        assert await redis.get_step_status("j1", "A") == "running"
        # 期望不匹配(仍是 ready 的旧期望)→ 失败,状态不变
        assert await t.cas_step_status("j1", "A", "ready", "done") is False
        assert await redis.get_step_status("j1", "A") == "running"

    @pytest.mark.asyncio
    async def test_set_step_worker_records_owner(self, redis, db):
        t = RedisTransport(redis, db)

        await t.set_step_worker("j1", "A", "w_x")

        assert await redis.get_step_worker("j1", "A") == "w_x"

    @pytest.mark.asyncio
    async def test_update_step_result_with_error_writes_db(self, redis, db):
        from shared.models import Job, Step, StepStatus

        t = RedisTransport(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test",
                          domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING,
                            pool="cpu"))

        started = datetime.now(timezone.utc)
        finished = datetime.now(timezone.utc)
        await t.update_step_result(
            "j1", "A", status="failed", worker_id="w_x",
            started_at=started, finished_at=finished,
            duration_sec=3.5, error="boom",
        )

        step = db.get_steps("j1")[0]
        assert step.status == StepStatus.FAILED
        assert step.worker_id == "w_x"
        assert step.error == "boom"
        assert step.duration_sec == 3.5

    @pytest.mark.asyncio
    async def test_update_step_result_without_error_omits_error_kwarg(self, redis, db):
        from shared.models import Job, Step, StepStatus

        t = RedisTransport(redis, db)
        db.create_job(Job(id="j1", content_type="video", pipeline="test",
                          domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING,
                            pool="cpu", error="prev"))

        started = datetime.now(timezone.utc)
        finished = datetime.now(timezone.utc)
        # error=None(默认)→ 不传 error kwarg,旧 error 列保持不变
        await t.update_step_result(
            "j1", "A", status="done", worker_id="w_x",
            started_at=started, finished_at=finished, duration_sec=1.0,
        )

        step = db.get_steps("j1")[0]
        assert step.status == StepStatus.DONE
        assert step.error == "prev"


class TestRedisTransportIncrementStats:
    @pytest.mark.asyncio
    async def test_completed_only_increments_db_and_redis(self, redis, db):
        t = await _registered(redis, db)

        await t.increment_worker_stats(WORKER_ID, completed=2)

        assert db.get_worker(WORKER_ID).tasks_completed == 2
        info = await redis.get_worker_info(WORKER_ID)
        assert info["tasks_completed"] == "2"
        # failed/duration 为 0 → 不写这两个 Redis 字段
        assert "tasks_failed" not in info
        assert "total_duration_sec" not in info

    @pytest.mark.asyncio
    async def test_failed_and_duration_increment_redis_floats(self, redis, db):
        t = await _registered(redis, db)

        await t.increment_worker_stats(WORKER_ID, failed=1, duration=4.5)

        w = db.get_worker(WORKER_ID)
        assert w.tasks_failed == 1
        assert w.total_duration_sec == 4.5
        info = await redis.get_worker_info(WORKER_ID)
        assert info["tasks_failed"] == "1"
        assert float(info["total_duration_sec"]) == 4.5
        # completed=0 → 不写 tasks_completed
        assert "tasks_completed" not in info

    @pytest.mark.asyncio
    async def test_all_zero_skips_all_redis_writes(self, redis, db):
        t = await _registered(redis, db)

        await t.increment_worker_stats(WORKER_ID)

        # DB 仍被调用(全 0 加法),但 Redis 三个统计字段都不写
        info = await redis.get_worker_info(WORKER_ID)
        assert "tasks_completed" not in info
        assert "tasks_failed" not in info
        assert "total_duration_sec" not in info


class TestRedisTransportAIUsageAndJob:
    @pytest.mark.asyncio
    async def test_record_ai_usage_persists_row(self, redis, db):
        from shared.models import AIUsage

        t = RedisTransport(redis, db)
        usage = AIUsage(exec_id="e1", provider="anthropic", model="claude",
                        job_id="j1", step="A", input_tokens=10, output_tokens=20,
                        cost_usd=0.5, duration_sec=1.2, cached=False)

        await t.record_ai_usage(usage)

        summary = db.get_usage_summary(job_id="j1")
        assert summary["calls"] == 1
        assert summary["total_input_tokens"] == 10
        assert summary["total_output_tokens"] == 20
        assert summary["total_cost_usd"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_record_ai_usage_dedup_same_exec_id(self, redis, db):
        """同 exec_id 二次落库不翻倍计费(ai_usage.exec_id UNIQUE → 第二次 no-op)。"""
        from shared.models import AIUsage

        t = RedisTransport(redis, db)
        usage = AIUsage(exec_id="dup1", provider="anthropic", model="claude",
                        job_id="j2", step="A", input_tokens=10, output_tokens=20,
                        cost_usd=0.5)
        await t.record_ai_usage(usage)
        await t.record_ai_usage(usage)   # 重复上报(worker 重试/双发)

        summary = db.get_usage_summary(job_id="j2")
        assert summary["calls"] == 1
        assert summary["total_cost_usd"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_get_job_pipeline_reads_redis(self, redis, db):
        t = RedisTransport(redis, db)
        await redis.init_job("j1", "video_pipeline", {"domain": "general"})

        assert await t.get_job_pipeline("j1") == "video_pipeline"

    @pytest.mark.asyncio
    async def test_get_job_pipeline_missing_returns_none(self, redis, db):
        t = RedisTransport(redis, db)
        assert await t.get_job_pipeline("missing") is None

    @pytest.mark.asyncio
    async def test_get_job_info_returns_dict(self, redis, db):
        t = RedisTransport(redis, db)
        await redis.init_job("j1", "video_pipeline",
                             {"domain": "lecture", "style_tags": ["formal"]})

        info = await t.get_job_info("j1")
        assert info["pipeline"] == "video_pipeline"
        assert info["domain"] == "lecture"

    @pytest.mark.asyncio
    async def test_get_job_info_missing_returns_empty_dict(self, redis, db):
        t = RedisTransport(redis, db)
        assert await t.get_job_info("missing") == {}


class TestRedisTransportEventsAndClose:
    @pytest.mark.asyncio
    async def test_publish_step_event_delivers_to_subscriber(self, redis, db):
        import asyncio as _asyncio

        t = RedisTransport(redis, db)
        received = []

        async def capture():
            async for msg in redis.subscribe("events:j1"):
                received.append(msg)
                break

        listener = _asyncio.create_task(capture())
        await _asyncio.sleep(0.05)
        await t.publish_step_event("events:j1", {"event": "step_log", "line": "x"})
        await _asyncio.wait_for(listener, timeout=2.0)

        assert received[0]["event"] == "step_log"
        assert received[0]["line"] == "x"

    @pytest.mark.asyncio
    async def test_close_is_noop(self, redis, db):
        t = RedisTransport(redis, db)
        # close 不负责关 redis/db(由 main.py 负责),仅须不抛
        await t.close()
        # redis 仍可用
        assert await redis.is_pool_frozen("cpu") is False


# create_transport 工厂(按 env 切换)


class TestCreateTransport:
    def test_no_gateway_url_returns_redis_transport(self, redis, db, monkeypatch):
        from worker.transport import create_transport

        monkeypatch.delenv("GATEWAY_URL", raising=False)
        t = create_transport(redis, db)

        assert isinstance(t, RedisTransport)
        assert t._redis is redis
        assert t._db is db
        assert t.worker_token == ""

    def test_gateway_url_returns_gateway_transport_with_inner(
        self, redis, db, monkeypatch,
    ):
        from worker.transport import create_transport

        monkeypatch.setenv("GATEWAY_URL", "https://flori.example")
        monkeypatch.setenv("WORKER_REGISTRATION_TOKEN", "tok-1")
        monkeypatch.setenv("WORKER_ID_FILE", "/tmp/.flori_worker_id_test")

        t = create_transport(redis, db)

        assert isinstance(t, GatewayTransport)
        # redis 非 None → 内层 RedisTransport 注入(混合模式影子写)
        assert isinstance(t._inner, RedisTransport)
        assert t._registration_token == "tok-1"

    def test_gateway_url_with_none_redis_has_no_inner(self, monkeypatch):
        from worker.transport import create_transport

        monkeypatch.setenv("GATEWAY_URL", "https://flori.example")
        monkeypatch.delenv("WORKER_REGISTRATION_TOKEN", raising=False)
        monkeypatch.setenv("WORKER_ID_FILE", "/tmp/.flori_worker_id_test")

        # 纯网关零隧道:redis/db 均 None → inner=None
        t = create_transport(None, None)

        assert isinstance(t, GatewayTransport)
        assert t._inner is None
        assert t._registration_token == ""


# get_credential(下载凭证中心分发,docs/03 §1.7.1)


class TestRedisTransportGetCredential:
    @pytest.mark.asyncio
    async def test_reads_redis_mirror(self, redis, db):
        t = RedisTransport(redis, db)
        await redis.set_dispatch_credential("bili_sessdata", "mirrored-token")
        assert await t.get_credential("bili_sessdata") == "mirrored-token"

    @pytest.mark.asyncio
    async def test_miss_falls_back_to_db_and_remirrors(self, redis, db):
        import json as _json
        db.set_credential("bili_cookies", _json.dumps({"sessdata": "from-db"}))
        t = RedisTransport(redis, db)
        assert await t.get_credential("bili_sessdata") == "from-db"
        # DB 兜底后回灌镜像,下次直接命中 redis
        assert await redis.get_dispatch_credential("bili_sessdata") == "from-db"

    @pytest.mark.asyncio
    async def test_unconfigured_returns_none(self, redis, db):
        t = RedisTransport(redis, db)
        assert await t.get_credential("youtube_cookies") is None


class TestGatewayTransportGetCredential:
    def _gw(self):
        return GatewayTransport(
            "https://gw", registration_token="reg", id_file="/tmp/nonexistent-id",
        )

    @pytest.mark.asyncio
    async def test_fetches_from_gateway(self):
        gw = self._gw()
        gw._client = AsyncMock()
        gw._client.get.return_value = make_response(
            200, {"key": "bili_sessdata", "value": "gw-token"})
        assert await gw.get_credential("bili_sessdata") == "gw-token"
        gw._client.get.assert_awaited_once()
        assert gw._client.get.call_args[0][0] == "/api/runner/credentials/bili_sessdata"

    @pytest.mark.asyncio
    async def test_401_raises_auth_rejected(self):
        from worker.transport import WorkerAuthRejected
        gw = self._gw()
        gw._client = AsyncMock()
        gw._client.get.return_value = make_response(401)
        with pytest.raises(WorkerAuthRejected):
            await gw.get_credential("bili_sessdata")

    @pytest.mark.asyncio
    async def test_network_error_degrades_to_none(self):
        gw = self._gw()
        gw._client = AsyncMock()
        gw._client.get.side_effect = RuntimeError("conn reset")
        assert await gw.get_credential("youtube_cookies") is None
