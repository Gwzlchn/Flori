"""worker transport 单测:RedisTransport 用 fakeredis,GatewayTransport 用 mock httpx."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import make_fakeredis
from shared.db import Database
from shared.runner_ops import (
    TaskLease,
    bind_task_lease,
    clear_task_lease,
    current_task_lease,
)
from tests.pubsub_helpers import subscription_barrier
from worker.transport import (
    RedisTransport,
    WorkerAuthRejected,
    WorkerConfigError,
    WorkerContractError,
)
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


# RedisTransport 只验证薄适配,状态机语义由 test_runner_ops.py 覆盖.


WORKER_ID = "w_t1"
POOL_LIMITS = {"cpu": 3, "io": 999, "scene": 1}


async def _registered(redis, db):
    """注册一个 worker 并返回 transport(让 _worker_id 就位)。"""
    t = RedisTransport(redis, db)
    await t.register(WORKER_ID, **REGISTER_ARGS)
    return t


class TestRunnerOpsDelegation:
    @pytest.mark.asyncio
    async def test_request_step_forwards_and_remembers_worker(
        self, redis, db, monkeypatch,
    ):
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e1"}
        delegate = AsyncMock(return_value=claim)
        monkeypatch.setattr("worker.transport.runner_ops.claim_step", delegate)
        transport = RedisTransport(redis, db)

        result = await transport.request_step(
            WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, {"private"},
        )

        assert result is claim
        assert transport._worker_id == WORKER_ID
        delegate.assert_awaited_once_with(
            redis, db, WORKER_ID, ["cpu"], POOL_LIMITS, {"vision"}, {"private"},
        )

    @pytest.mark.asyncio
    async def test_report_done_forwards_worker_and_timing(self, redis, db, monkeypatch):
        delegate = AsyncMock()
        monkeypatch.setattr("worker.transport.runner_ops.report_step_done", delegate)
        transport = RedisTransport(redis, db)
        transport._worker_id = WORKER_ID
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e1"}

        await transport.report_done(claim, 12.3, 100.0)

        delegate.assert_awaited_once_with(
            redis, db, WORKER_ID, claim, 12.3, 100.0,
        )

    @pytest.mark.asyncio
    async def test_report_failed_forwards_count_policy(self, redis, db, monkeypatch):
        delegate = AsyncMock()
        monkeypatch.setattr("worker.transport.runner_ops.report_step_failed", delegate)
        transport = RedisTransport(redis, db)
        transport._worker_id = WORKER_ID
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e1"}

        await transport.report_failed(
            claim, "timeout", "timeout", 3.0, 100.0, count_stats=False,
        )

        delegate.assert_awaited_once_with(
            redis, db, WORKER_ID, claim, "timeout", "timeout", 3.0, 100.0, False,
        )

    @pytest.mark.asyncio
    async def test_release_forwards_current_worker(self, redis, db, monkeypatch):
        delegate = AsyncMock()
        monkeypatch.setattr("worker.transport.runner_ops.release_step", delegate)
        transport = RedisTransport(redis, db)
        transport._worker_id = WORKER_ID
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e1"}

        await transport.release(claim)

        delegate.assert_awaited_once_with(redis, db, WORKER_ID, claim)


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
            json_data={"worker_id": "w_srv", "worker_token": "flwt-secret"},
        )

        returned = await gw.register("w_local", **REGISTER_ARGS)

        assert returned == "w_srv"
        # 注册 token 通过 Authorization 头下发
        _, kwargs = gw._client.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer flw-tok"
        assert kwargs["json"]["worker_id"] == "w_local"
        assert kwargs["json"]["tags"] == ["vision"]
        # 服务端回的 worker_token 被记下,供后续心跳鉴权
        assert gw._worker_token == "flwt-secret"
        # 服务端回的 worker_id 落盘
        assert id_file.read_text().strip() == "w_srv"
        token_file = id_file.with_name("worker.token")
        assert token_file.read_text().strip() == "flwt-secret"
        assert (token_file.stat().st_mode & 0o777) == 0o600
        # 影子写:redis/db 也有这行
        assert await redis.get_worker_info("w_srv") is not None

    @pytest.mark.asyncio
    async def test_reuses_cached_id_on_second_bootstrap_without_cached_token(
        self, redis, db, tmp_path,
    ):
        gw, id_file = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_first", "worker_token": "flwt-1"},
        )
        await gw.register("w_local", **REGISTER_ARGS)
        assert id_file.read_text().strip() == "w_first"
        id_file.with_name("worker.token").unlink()

        # 没有 cached token 才重新 register,但 worker_id 仍复用 id_file。
        gw2, _ = make_gateway(redis, db, tmp_path)
        gw2._client.post.return_value = make_response(
            json_data={"worker_token": "flwt-2"},
        )
        returned = await gw2.register("w_other", **REGISTER_ARGS)

        _, kwargs = gw2._client.post.call_args
        assert kwargs["json"]["worker_id"] == "w_first"
        assert returned == "w_first"

    @pytest.mark.asyncio
    async def test_cached_token_resumes_without_registration_token(
        self, redis, db, tmp_path,
    ):
        gw, id_file = make_gateway(redis, db, tmp_path, registration_token="")
        id_file.write_text("w_cached")
        id_file.with_name("worker.token").write_text("flwt-cached\n")
        gw._client.post.return_value = make_response(
            json_data={"desired_config": {"concurrency": 2}, "cfg_rev": 7},
        )

        returned = await gw.register("w_local", **REGISTER_ARGS)

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/resume"
        assert kwargs["headers"]["Authorization"] == "Bearer flwt-cached"
        assert kwargs["json"]["worker_id"] == "w_cached"
        assert returned == "w_cached"
        assert gw.worker_token == "flwt-cached"
        assert gw.initial_config == {
            "desired_config": {"concurrency": 2}, "cfg_rev": 7,
        }

    @pytest.mark.asyncio
    async def test_resume_auth_rejected_does_not_fall_back_to_register(self, tmp_path):
        fallback = "flw-" + "fallback"
        gw, id_file = make_pure_gateway(tmp_path, registration_token=fallback)
        id_file.write_text("w_cached")
        id_file.with_name("worker.token").write_text("flwt-revoked\n")
        gw._client.post.return_value = make_response(status_code=403)

        with pytest.raises(WorkerAuthRejected):
            await gw.register("w_local", **REGISTER_ARGS)

        assert gw._client.post.call_count == 1
        assert gw._client.post.call_args.args[0] == "/api/runner/resume"
        assert id_file.with_name("worker.token").read_text().strip() == "flwt-revoked"

    @pytest.mark.asyncio
    async def test_first_bootstrap_without_registration_token_fails_fast(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path, registration_token="")
        with pytest.raises(WorkerConfigError):
            await gw.register("w_local", **REGISTER_ARGS)

    @pytest.mark.asyncio
    async def test_register_requires_worker_token_in_response(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv"},
        )
        with pytest.raises(WorkerContractError):
            await gw.register("w_local", **REGISTER_ARGS)

    @pytest.mark.asyncio
    async def test_worker_token_file_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom" / "token"
        monkeypatch.setenv("WORKER_TOKEN_FILE", str(custom))
        gw, _ = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "flwt-custom"},
        )

        await gw.register("w_local", **REGISTER_ARGS)

        assert custom.read_text().strip() == "flwt-custom"


class TestGatewayHeartbeat:
    @pytest.mark.asyncio
    async def test_401_raises_worker_auth_rejected(
        self, redis, db, tmp_path, monkeypatch,
    ):
        # 心跳被 401(per-worker token 失效)时抛 WorkerAuthRejected,交主循环停机。
        # 认证失败优先上抛,不 fall-through 到 inner。
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=401)
        inner_hb = AsyncMock()
        monkeypatch.setattr(gw._inner, "heartbeat", inner_hb)

        with pytest.raises(WorkerAuthRejected):
            await gw.heartbeat("w1")

        inner_hb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_403_raises_worker_auth_rejected(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=403)
        inner_hb = AsyncMock()
        monkeypatch.setattr(gw._inner, "heartbeat", inner_hb)

        with pytest.raises(WorkerAuthRejected):
            await gw.heartbeat("w1")

        inner_hb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_429_raises_worker_auth_rejected(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=429)
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

        inner_hb.assert_awaited_once_with("w1", load=None, concurrency=None)

    @pytest.mark.asyncio
    async def test_posts_worker_id_and_current_status(
        self, redis, db, tmp_path, monkeypatch,
    ):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        monkeypatch.setattr(gw._inner, "heartbeat", AsyncMock())
        monkeypatch.setattr(gw._inner, "update_status", AsyncMock())

        # 心跳须带当前状态、并发与已应用配置版本,否则 runner API 无法判定漂移。
        await gw.update_status("w1", "busy", "job1", "03_scene")
        await gw.heartbeat("w1", concurrency=3)

        _, kwargs = gw._client.post.call_args
        assert kwargs["json"] == {
            "worker_id": "w1", "status": "busy",
            "current_job": "job1", "current_step": "03_scene",
            "applied_cfg_rev": 0,
            "concurrency": 3,
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
    async def test_request_step_returns_ai_claim_without_running_marker(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        setattr(gw, "_worker_" + "token", "wt")
        claim = {
            "kind": "ai", "task_id": "at_codex", "step": "synthesis",
            "pool": "ai", "exec_id": "e", "provider": "codex-cli",
        }
        gw._client.post.return_value = make_response(json_data={"claim": claim})

        result = await gw.request_step("w1", ["ai"], {"ai": 1}, {"codex-cli"}, set())

        assert result == claim
        assert gw._running == {}
        assert gw._ai_execs == {"e"}
        lease = current_task_lease()
        assert lease is not None and lease.job_id == "at_codex" and lease.exec_id == "e"
        clear_task_lease()

    @pytest.mark.asyncio
    async def test_ai_lifecycle_posts_with_bound_lease(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        setattr(gw, "_worker_" + "token", "wt")
        claim = {
            "kind": "ai", "task_id": "at_gateway", "step": "synthesis",
            "pool": "ai", "exec_id": "exec-ai", "provider": "codex-cli",
        }
        gw._client.post.return_value = make_response(json_data={"claim": claim})
        await gw.request_step("w1", ["ai"], {"ai": 1}, {"codex-cli"}, set())
        assert current_task_lease().job_id == "at_gateway"

        gw._client.post.return_value = make_response(json_data={"ok": True})
        assert await gw.mark_ai_task_executing(claim) is True
        assert gw._client.post.call_args.args[0].endswith("/executing")
        await gw.set_ai_result("at_gateway", {"content": "answer"})
        assert gw._client.post.call_args.kwargs["json"] == {
            "result": {"content": "answer"},
        }
        assert await gw.record_ai_task_log({"task_id": "at_gateway"}) is True
        assert await gw.renew_ai_task_claim(claim) is True
        assert await gw.finish_ai_task_claim(claim, "succeeded") is True
        await gw.publish_step_event("events:at_gateway", {"event": "ai_task_done"})
        before_release = gw._client.post.await_count
        await gw.release(claim)
        assert gw._client.post.await_count == before_release + 1
        assert gw._client.post.call_args.args[0].endswith("/release")
        assert gw._ai_execs == set()
        assert current_task_lease() is None

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
    async def test_request_step_403_raises_auth_rejected(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=403)

        with pytest.raises(WorkerAuthRejected):
            await gw.request_step("w1", ["cpu"], {"cpu": 3}, set(), set())

    @pytest.mark.asyncio
    async def test_request_step_429_raises_auth_rejected(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=429)

        with pytest.raises(WorkerAuthRejected):
            await gw.request_step("w1", ["cpu"], {"cpu": 3}, set(), set())

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
    async def test_report_done_auth_rejected_raises(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response(status_code=401)
        claim = {"job_id": "j1", "step": "A", "pool": "cpu", "exec_id": "e"}

        with pytest.raises(WorkerAuthRejected):
            await gw.report_done(claim, 1.0, 0.0)

    @pytest.mark.asyncio
    async def test_publish_step_event_maps_progress(self, redis, db, tmp_path):
        gw, _ = make_gateway(redis, db, tmp_path)
        gw._client.post.return_value = make_response()
        bind_task_lease(TaskLease("w1", "j1", "A", "exec-1"))
        try:
            await gw.publish_step_event("events:j1", {"event": "step_log", "line": "x"})
        finally:
            clear_task_lease()

        url, kwargs = gw._client.post.call_args
        assert url[0] == "/api/runner/jobs/j1/steps/A/progress"
        assert kwargs["json"] == {"payload": {"event": "step_log", "line": "x"}}
        assert kwargs["headers"]["X-Flori-Lease-Exec"] == "exec-1"


class TestGatewayPureMode:
    """inner=None(纯网关零隧道):无影子写,无内层退回,委派返回安全默认值."""

    @pytest.mark.asyncio
    async def test_register_returns_server_id_no_shadow_write(self, redis, tmp_path):
        gw, id_file = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "flwt-secret"},
        )

        returned = await gw.register("w_local", **REGISTER_ARGS)

        assert returned == "w_srv"
        assert gw._worker_token == "flwt-secret"
        assert id_file.read_text().strip() == "w_srv"
        # 无内层时 redis 不应有这行(无影子写).
        assert await redis.get_worker_info("w_srv") is None

    @pytest.mark.asyncio
    async def test_worker_token_property_exposes_token(self, tmp_path):
        gw, _ = make_pure_gateway(tmp_path)
        gw._client.post.return_value = make_response(
            json_data={"worker_id": "w_srv", "worker_token": "flwt-xyz"},
        )
        await gw.register("w_local", **REGISTER_ARGS)
        # GatewayStorage 经此属性拿 per-worker token
        assert gw.worker_token == "flwt-xyz"

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
            json_data={"worker_id": "w_srv", "worker_token": "flwt-shadow"},
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
        # 槽位已满时,不同 holder 第二次失败.
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

        # 期望匹配时推进成功.
        assert await t.cas_step_status("j1", "A", "ready", "running") is True
        assert await redis.get_step_status("j1", "A") == "running"
        # 期望不匹配(仍是 ready 的旧期望)时失败,状态不变.
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
        # error=None(默认)时不传 error kwarg,旧 error 列保持不变.
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
        # failed/duration 为 0 时不写这两个 Redis 字段.
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
        # completed=0 时不写 tasks_completed.
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
        """同 exec_id 二次落库不翻倍计费(ai_usage.exec_id UNIQUE,第二次 no-op)."""
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
    async def test_publish_step_event_delivers_to_subscriber(
        self, redis, db, monkeypatch,
    ):
        import asyncio as _asyncio

        t = RedisTransport(redis, db)
        received = []

        async def capture():
            async for msg in redis.subscribe("events:j1"):
                received.append(msg)
                break

        ready = subscription_barrier(redis, monkeypatch)
        listener = _asyncio.create_task(capture())
        await _asyncio.wait_for(ready.wait(), timeout=1.0)
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
        # redis 非 None 时内层 RedisTransport 注入(混合模式影子写).
        assert isinstance(t._inner, RedisTransport)
        assert t._registration_token == "tok-1"

    def test_gateway_url_with_none_redis_has_no_inner(self, monkeypatch):
        from worker.transport import create_transport

        monkeypatch.setenv("GATEWAY_URL", "https://flori.example")
        monkeypatch.delenv("WORKER_REGISTRATION_TOKEN", raising=False)
        monkeypatch.setenv("WORKER_ID_FILE", "/tmp/.flori_worker_id_test")

        # 纯网关零隧道:redis/db 均 None 时 inner=None.
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
