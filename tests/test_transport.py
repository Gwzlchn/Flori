"""tests for transport — RedisTransport(fakeredis) + GatewayTransport(mock httpx)。"""

from __future__ import annotations

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
