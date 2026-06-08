"""tests for api/routes/runner.py — worker-gateway register/heartbeat/offline + token."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from shared.redis_client import RedisClient
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


# ── 认领/上报端点:用真 fakeredis,让服务端编排真正跑起来 ──


@pytest.fixture
async def real_redis():
    rc = RedisClient.__new__(RedisClient)
    rc._url = "redis://fake"
    rc._redis = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    await rc.set_registration_token(REG_TOKEN)  # 接入门禁放行
    yield rc
    await rc.close()


@pytest.fixture
def jobs_app(db, test_config, real_redis):
    return create_app(db=db, redis=real_redis, config=test_config)


@pytest.fixture
async def jobs_client(jobs_app):
    transport = ASGITransport(app=jobs_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register_real(client):
    payload = {"type": "cpu", "pools": ["cpu", "io"], "tags": ["vision"], "reject_tags": []}
    resp = await client.post("/api/runner/register", json=payload, headers=_reg_headers())
    body = resp.json()
    return body["worker_id"], body["worker_token"]


class TestJobsRequest:
    @pytest.mark.asyncio
    async def test_requires_worker_token(self, jobs_client):
        resp = await jobs_client.post("/api/runner/jobs/request", json={"pools": ["cpu"]})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_enriched_claim(self, jobs_client, real_redis):
        worker_id, token = await _register_real(jobs_client)
        await real_redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await real_redis.set_step_status("j1", "A", "ready")
        await real_redis.init_job("j1", "video", {"domain": "lecture",
                                                   "style_tags": '["formal"]'})

        resp = await jobs_client.post(
            "/api/runner/jobs/request",
            json={"pools": ["cpu"], "pool_limits": {"cpu": 3},
                  "tags": ["vision"], "reject_tags": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        claim = resp.json()["claim"]
        assert claim["job_id"] == "j1" and claim["step"] == "A" and claim["pool"] == "cpu"
        assert claim["exec_id"].startswith(f"{worker_id}:")
        # enrich:pipeline/domain/style_tags 塞进 claim,gateway worker 无需回读 redis
        assert claim["pipeline"] == "video"
        assert claim["domain"] == "lecture"
        assert claim["style_tags"] == ["formal"]
        assert await real_redis.get_step_status("j1", "A") == "running"

    @pytest.mark.asyncio
    async def test_returns_null_when_empty(self, jobs_client, monkeypatch):
        import api.routes.runner as runner_mod
        monkeypatch.setattr(runner_mod, "_CLAIM_WINDOW_SEC", 0.0)  # 窗口归零→立刻返回

        _, token = await _register_real(jobs_client)
        resp = await jobs_client.post(
            "/api/runner/jobs/request",
            json={"pools": ["cpu"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"claim": None}

    @pytest.mark.asyncio
    async def test_in_scope_pool_claims(self, jobs_client, real_redis):
        # token 注册池 [cpu,io]，请求 cpu(范围内) → 认到 cpu 步。
        worker_id, token = await _register_real(jobs_client)
        await real_redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await real_redis.set_step_status("j1", "A", "ready")
        await real_redis.init_job("j1", "video", {})

        resp = await jobs_client.post(
            "/api/runner/jobs/request",
            json={"pools": ["cpu"], "tags": ["vision"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        claim = resp.json()["claim"]
        assert claim["job_id"] == "j1" and claim["pool"] == "cpu"

    @pytest.mark.asyncio
    async def test_out_of_scope_pool_not_served(self, jobs_client, real_redis):
        # token 注册池 [cpu,io]，请求 gpu(范围外) → null，即便 gpu 步 ready 也不服务。
        worker_id, token = await _register_real(jobs_client)
        await real_redis.enqueue_step("gpu", "j1", "A", [], priority=0)
        await real_redis.set_step_status("j1", "A", "ready")
        await real_redis.init_job("j1", "video", {})

        resp = await jobs_client.post(
            "/api/runner/jobs/request",
            json={"pools": ["gpu"], "tags": ["vision"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"claim": None}
        # 范围外认领被早退裁掉，gpu 步未被翻成 running。
        assert await real_redis.get_step_status("j1", "A") == "ready"

    @pytest.mark.asyncio
    async def test_partial_scope_filters_to_allowed(self, jobs_client, real_redis):
        # 请求 [cpu,gpu]，token 仅授权 [cpu,io] → 裁剪到 cpu，仍能认到 cpu 步。
        worker_id, token = await _register_real(jobs_client)
        await real_redis.enqueue_step("cpu", "j1", "A", [], priority=0)
        await real_redis.set_step_status("j1", "A", "ready")
        await real_redis.init_job("j1", "video", {})

        resp = await jobs_client.post(
            "/api/runner/jobs/request",
            json={"pools": ["gpu", "cpu"], "tags": ["vision"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        claim = resp.json()["claim"]
        assert claim["pool"] == "cpu"

    @pytest.mark.asyncio
    async def test_unrestricted_token_claims_any_pool(self, jobs_client, real_redis):
        # 空 pools 的 token=不限范围(兼容旧 token) → 任意池可认。
        payload = {"type": "gpu", "pools": [], "tags": ["vision"], "reject_tags": []}
        resp = await jobs_client.post(
            "/api/runner/register", json=payload, headers=_reg_headers(),
        )
        token = resp.json()["worker_token"]
        await real_redis.enqueue_step("gpu", "j1", "A", [], priority=0)
        await real_redis.set_step_status("j1", "A", "ready")
        await real_redis.init_job("j1", "video", {})

        resp = await jobs_client.post(
            "/api/runner/jobs/request",
            json={"pools": ["gpu"], "tags": ["vision"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        claim = resp.json()["claim"]
        assert claim["job_id"] == "j1" and claim["pool"] == "gpu"


class TestJobsComplete:
    @pytest.mark.asyncio
    async def test_requires_token(self, jobs_client):
        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/complete",
            json={"pool": "cpu", "exec_id": "e", "duration": 1.0, "started_at": 0.0},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_writes_db_and_increments(self, jobs_client, db, real_redis):
        from shared.models import Job, Step, StepStatus

        worker_id, token = await _register_real(jobs_client)
        db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))

        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/complete",
            json={"pool": "cpu", "exec_id": f"{worker_id}:1",
                  "duration": 12.34, "started_at": 100.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert db.get_steps("j1")[0].status == StepStatus.DONE
        assert db.get_worker(worker_id).tasks_completed == 1


class TestJobsFail:
    @pytest.mark.asyncio
    async def test_count_stats_true_increments(self, jobs_client, db, real_redis):
        from shared.models import Job, Step, StepStatus

        worker_id, token = await _register_real(jobs_client)
        db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))

        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/fail",
            json={"pool": "cpu", "exec_id": f"{worker_id}:1", "error": "boom",
                  "error_type": "segfault", "duration": 2.0, "started_at": 0.0,
                  "count_stats": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert db.get_steps("j1")[0].status == StepStatus.FAILED
        assert db.get_worker(worker_id).tasks_failed == 1


class TestJobsRelease:
    @pytest.mark.asyncio
    async def test_release_unfreezes_scene_and_idles(self, jobs_client, real_redis):
        worker_id, token = await _register_real(jobs_client)
        await real_redis.try_acquire_slot("scene", limit=1)
        await real_redis.freeze_pool("cpu")

        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/release",
            json={"pool": "scene", "exec_id": "e"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert await real_redis.get_pool_count("scene") == 0
        assert await real_redis.is_pool_frozen("cpu") is False
        assert (await real_redis.get_worker_info(worker_id))["status"] == "idle"


class TestJobsProgress:
    @pytest.mark.asyncio
    async def test_requires_token(self, jobs_client):
        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/progress", json={"payload": {}},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_publishes_to_events_channel(self, jobs_client, real_redis):
        import asyncio

        _, token = await _register_real(jobs_client)
        events = []

        async def capture():
            async for msg in real_redis.subscribe("events:j1"):
                events.append(msg)
                break

        listener = asyncio.create_task(capture())
        await asyncio.sleep(0.05)
        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/progress",
            json={"payload": {"line": "hello"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        await asyncio.wait_for(listener, timeout=2.0)
        assert events[0] == {"event": "step_progress", "line": "hello"}


class TestUsage:
    @pytest.mark.asyncio
    async def test_requires_token(self, jobs_client):
        resp = await jobs_client.post(
            "/api/runner/usage",
            json={"exec_id": "e", "provider": "p", "model": "m"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_records_usage_row(self, jobs_client, db):
        _, token = await _register_real(jobs_client)
        resp = await jobs_client.post(
            "/api/runner/usage",
            json={"exec_id": "e1", "provider": "anthropic", "model": "claude",
                  "job_id": "j1", "step": "A", "input_tokens": 10,
                  "output_tokens": 20, "cost_usd": 0.5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        summary = db.get_usage_summary(job_id="j1")
        assert summary["total_input_tokens"] == 10


# ── 产物代理端点:worker token 鉴权,经 API 读写 storage ──


class TestArtifacts:
    @pytest.mark.asyncio
    async def test_all_require_worker_token(self, jobs_client):
        assert (await jobs_client.get("/api/runner/jobs/j1/artifacts")).status_code == 401
        assert (
            await jobs_client.get("/api/runner/jobs/j1/artifacts/job.json")
        ).status_code == 401
        assert (
            await jobs_client.put("/api/runner/jobs/j1/artifacts/job.json", content=b"x")
        ).status_code == 401

    @pytest.mark.asyncio
    async def test_put_then_list_and_get(self, jobs_client, test_config):
        _, token = await _register_real(jobs_client)
        h = {"Authorization": f"Bearer {token}"}

        put = await jobs_client.put(
            "/api/runner/jobs/j1/artifacts/output/notes.md", content=b"hello", headers=h,
        )
        assert put.status_code == 200 and put.json() == {"ok": True}
        # 落到 API 端 LocalStorage(jobs_dir)
        assert (test_config.jobs_dir / "j1" / "output" / "notes.md").read_bytes() == b"hello"

        listed = await jobs_client.get("/api/runner/jobs/j1/artifacts", headers=h)
        assert listed.status_code == 200
        assert listed.json()["files"] == ["output/notes.md"]

        got = await jobs_client.get(
            "/api/runner/jobs/j1/artifacts/output/notes.md", headers=h,
        )
        assert got.status_code == 200
        assert got.content == b"hello"
        assert got.headers["content-type"] == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_get_missing_returns_404(self, jobs_client):
        _, token = await _register_real(jobs_client)
        resp = await jobs_client.get(
            "/api/runner/jobs/j1/artifacts/nope.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, jobs_client):
        _, token = await _register_real(jobs_client)
        h = {"Authorization": f"Bearer {token}"}
        # rel:path 含 ".." → 400(get 与 put 同守卫);用 %2e%2e 避免客户端折叠掉 ".."
        assert (
            await jobs_client.get(
                "/api/runner/jobs/j1/artifacts/%2e%2e/secret", headers=h,
            )
        ).status_code == 400
        assert (
            await jobs_client.put(
                "/api/runner/jobs/j1/artifacts/%2e%2e/secret", content=b"x", headers=h,
            )
        ).status_code == 400

    @pytest.mark.asyncio
    async def test_job_id_traversal_rejected(self, jobs_client):
        _, token = await _register_real(jobs_client)
        h = {"Authorization": f"Bearer {token}"}
        # job_id 段含 ".." → 400(list/get/put 三端点同守卫),挡经 job_id 读写中心数据
        assert (
            await jobs_client.get("/api/runner/jobs/%2e%2e/artifacts", headers=h)
        ).status_code == 400
        assert (
            await jobs_client.get(
                "/api/runner/jobs/%2e%2e/artifacts/db%2Fanalyzer.db", headers=h,
            )
        ).status_code == 400
        assert (
            await jobs_client.put(
                "/api/runner/jobs/%2e%2e/artifacts/x", content=b"x", headers=h,
            )
        ).status_code == 400
