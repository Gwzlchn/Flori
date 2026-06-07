"""tests for api/routes/runner.py — worker-gateway register/heartbeat/offline + token."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from api.main import create_app

REG_TOKEN = "mnw-registration-secret"


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def test_config(tmp_path, configs_dir):
    cfg = load_config(config_dir=configs_dir, data_dir=tmp_path)
    cfg.jobs_dir = tmp_path / "jobs"
    cfg.jobs_dir.mkdir()
    cfg.prompts_dir = tmp_path / "prompts"
    cfg.prompts_dir.mkdir()
    return cfg


@pytest.fixture
def db(test_config):
    d = Database(test_config.db_path)
    d.init_schema()
    yield d
    d.close()


@pytest.fixture
def redis_mock():
    """默认：Redis 已铸 registration token（接入门禁放行）。"""
    rc = AsyncMock()
    rc.get_registration_token.return_value = REG_TOKEN
    rc.get_worker_info.return_value = None
    return rc


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """默认清掉 env 兜底 token，让门禁只认 Redis 铸的那枚。"""
    monkeypatch.delenv("WORKER_REGISTRATION_TOKEN", raising=False)


@pytest.fixture
def app(db, test_config, redis_mock):
    return create_app(db=db, redis=redis_mock, config=test_config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _reg_headers(token=REG_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _register(client, token=REG_TOKEN, **body):
    payload = {"type": "cpu", "pools": ["cpu", "io"], "tags": [], "reject_tags": []}
    payload.update(body)
    return client.post("/api/runner/register", json=payload, headers=_reg_headers(token))


class TestRegisterGate:
    @pytest.mark.asyncio
    async def test_bad_registration_token_401(self, client):
        resp = await _register(client, token="wrong-token")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_registration_token_401(self, client):
        resp = await client.post(
            "/api/runner/register",
            json={"type": "cpu", "pools": ["cpu"]},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_disabled_when_nothing_configured_503(self, client, redis_mock):
        # Redis 没铸 token 且 env 没配 → fail closed 503
        redis_mock.get_registration_token.return_value = None
        resp = await client.post(
            "/api/runner/register",
            json={"type": "cpu", "pools": ["cpu"]},
            headers=_reg_headers(""),
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_env_fallback_token_accepted(self, client, redis_mock, monkeypatch):
        redis_mock.get_registration_token.return_value = None
        monkeypatch.setenv("WORKER_REGISTRATION_TOKEN", "env-secret")
        resp = await _register(client, token="env-secret")
        assert resp.status_code == 200


class TestRegisterAllocates:
    @pytest.mark.asyncio
    async def test_allocates_id_and_token(self, client, db, redis_mock):
        resp = await _register(client)
        assert resp.status_code == 200
        body = resp.json()
        worker_id = body["worker_id"]
        token = body["worker_token"]
        assert worker_id.startswith("cpu-")
        assert token.startswith("mnwt-")
        assert body["heartbeat_sec"] == 10

        # worker_tokens 行写入（仅存 hash）
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = db.get_worker_token_by_hash(token_hash)
        assert row is not None
        assert row["worker_id"] == worker_id
        assert row["revoked"] is False

        # workers 行写入
        assert db.get_worker(worker_id) is not None

        # Redis liveness key 单写（info 形态对齐 RedisTransport）
        redis_mock.register_worker.assert_awaited_once()
        args, kwargs = redis_mock.register_worker.call_args
        assert args[0] == worker_id
        info = args[1]
        assert info["status"] == "idle"
        assert info["pools"] == "cpu,io"
        assert kwargs.get("ttl") == 30

    @pytest.mark.asyncio
    async def test_reuses_supplied_worker_id(self, client, db):
        resp = await _register(client, worker_id="cpu-fixed01")
        assert resp.status_code == 200
        assert resp.json()["worker_id"] == "cpu-fixed01"
        assert db.get_worker("cpu-fixed01") is not None

    @pytest.mark.asyncio
    async def test_tags_sorted_into_redis_info(self, client, redis_mock):
        resp = await _register(client, tags=["vision", "claude-cli"], reject_tags=["b", "a"])
        assert resp.status_code == 200
        info = redis_mock.register_worker.call_args[0][1]
        assert info["tags"] == "claude-cli,vision"
        assert info["reject_tags"] == "a,b"


class TestHeartbeat:
    async def _register_worker(self, client):
        resp = await _register(client)
        body = resp.json()
        return body["worker_id"], body["worker_token"]

    @pytest.mark.asyncio
    async def test_requires_worker_token(self, client):
        resp = await client.post(
            "/api/runner/heartbeat", json={"worker_id": "cpu-x"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_draining_flag(self, client, redis_mock):
        worker_id, token = await self._register_worker(client)
        redis_mock.get_worker_info.return_value = {"status": "idle"}
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"draining": False}

    @pytest.mark.asyncio
    async def test_draining_when_redis_status_draining(self, client, redis_mock):
        worker_id, token = await self._register_worker(client)
        redis_mock.get_worker_info.return_value = {"status": "draining"}
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"draining": True}

    @pytest.mark.asyncio
    async def test_worker_id_mismatch_403(self, client):
        _, token = await self._register_worker(client)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": "cpu-someone-else"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_revoked_token_401(self, client, db):
        worker_id, token = await self._register_worker(client)
        db.revoke_worker_token(worker_id)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401


class TestOffline:
    @pytest.mark.asyncio
    async def test_offline_sets_status(self, client, db):
        resp = await _register(client)
        body = resp.json()
        worker_id, token = body["worker_id"], body["worker_token"]
        resp = await client.post(
            "/api/runner/offline",
            json={"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        row = db._conn.execute(
            "SELECT status FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        assert row["status"] == "offline"

    @pytest.mark.asyncio
    async def test_offline_requires_token(self, client):
        resp = await client.post("/api/runner/offline", json={"worker_id": "cpu-x"})
        assert resp.status_code == 401


class TestTokenRevocationViaDelete:
    """删 worker → 吊销其 token → 后续心跳 401（防复活/防被删 worker 继续心跳）。"""

    @pytest.mark.asyncio
    async def test_delete_worker_revokes_token(self, client, redis_mock):
        resp = await _register(client)
        body = resp.json()
        worker_id, token = body["worker_id"], body["worker_token"]

        # 心跳先验证 token 有效
        redis_mock.get_worker_info.return_value = {"status": "idle"}
        ok = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ok.status_code == 200

        # 删 worker（刚注册 → online，需 force）
        redis_mock.worker_exists.return_value = True
        resp = await client.delete(f"/api/workers/{worker_id}?force=true")
        assert resp.status_code == 204

        # 同一 token 再心跳 → 401
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
