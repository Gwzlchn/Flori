"""tests for api/routes/runner.py — worker-gateway register/heartbeat/offline + token."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import make_fakeredis
from api.main import create_app

REG_TOKEN = "flw-registration-secret"


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def redis_mock():
    """默认: Redis 已铸 registration token(接入门禁放行)。"""
    rc = AsyncMock()
    rc.get_registration_token.return_value = REG_TOKEN
    rc.get_worker_info.return_value = None
    return rc


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """默认清掉 env 兜底 token,让门禁只认 Redis 铸的那枚。"""
    monkeypatch.delenv("WORKER_REGISTRATION_TOKEN", raising=False)


@pytest.fixture
def app(db, test_config, redis_mock):
    return create_app(db=db, redis=redis_mock, config=test_config)


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
        assert token.startswith("flwt-")

        # worker_tokens 行写入(仅存 hash)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = db.get_worker_token_by_hash(token_hash)
        assert row is not None
        assert row["worker_id"] == worker_id
        assert row["revoked"] is False

        # workers 行写入
        assert db.get_worker(worker_id) is not None

        # Redis liveness key 单写(info 形态对齐 RedisTransport)
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
    async def test_valid_token_heartbeat_ok(self, client, redis_mock):
        # 心跳只刷存活,返回 {"ok": True};暂停由 claim_step 据 admin_status 兜底(不经心跳回发,见 test_runner_ops)。
        worker_id, token = await self._register_worker(client)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # 心跳响应即配置热下发通道(docs/03 §1.7.2):未配置时 desired_config=None/rev=0。
        assert resp.json() == {"ok": True, "desired_config": None, "cfg_rev": 0}

    @pytest.mark.asyncio
    async def test_heartbeat_writes_live_load(self, client, redis_mock):
        # 心跳带 load → 经网关写 redis worker hash 的 load 字段(JSON)。
        import json as _json
        worker_id, token = await self._register_worker(client)
        resp = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": worker_id, "status": "idle",
                  "load": {"cpu_pct": 30.0, "mem_pct": 55.0, "loadavg": 1.2}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # 找到那次写 load 字段的调用。
        load_calls = [c for c in redis_mock.set_worker_field.call_args_list
                      if c.args[1] == "load"]
        assert load_calls, "load 未写入 redis worker hash"
        assert _json.loads(load_calls[-1].args[2])["cpu_pct"] == 30.0

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
    """删 worker 会吊销其 token,后续心跳 401(防复活/防被删 worker 继续心跳)。"""

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

        # 删 worker(刚注册 → online,需 force)
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


# 认领/上报端点:用真 fakeredis,让服务端编排真正跑起来


@pytest.fixture
async def real_redis():
    rc = make_fakeredis()
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
        # token 注册池 [cpu,io],请求 cpu(范围内) → 认到 cpu 步。
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
        # token 注册池 [cpu,io],请求 gpu(范围外) → null,即便 gpu 步 ready 也不服务。
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
        # 范围外认领被早退裁掉,gpu 步未被翻成 running。
        assert await real_redis.get_step_status("j1", "A") == "ready"

    @pytest.mark.asyncio
    async def test_partial_scope_filters_to_allowed(self, jobs_client, real_redis):
        # 请求 [cpu,gpu],token 仅授权 [cpu,io] → 裁剪到 cpu,仍能认到 cpu 步。
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

    @pytest.mark.asyncio
    async def test_count_stats_false_no_increment(self, jobs_client, db, real_redis):
        """count_stats=False(timeout/异常分支)→ 步落 FAILED 但不累加 worker 失败计数。"""
        from shared.models import Job, Step, StepStatus

        worker_id, token = await _register_real(jobs_client)
        db.create_job(Job(id="j1", content_type="video", pipeline="video", domain="general"))
        db.upsert_step(Step(job_id="j1", name="A", status=StepStatus.RUNNING, pool="cpu"))

        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/fail",
            json={"pool": "cpu", "exec_id": f"{worker_id}:1", "error": "timeout",
                  "error_type": "timeout", "duration": 2.0, "started_at": 0.0,
                  "count_stats": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert db.get_steps("j1")[0].status == StepStatus.FAILED
        assert db.get_worker(worker_id).tasks_failed == 0


class TestJobsRelease:
    @pytest.mark.asyncio
    async def test_release_slot_and_idles(self, jobs_client, real_redis):
        worker_id, token = await _register_real(jobs_client)
        await real_redis.try_acquire_slot("cpu", 1, "e")   # holder = 下方 release 的 exec_id

        resp = await jobs_client.post(
            "/api/runner/jobs/j1/steps/A/release",
            json={"pool": "cpu", "exec_id": "e"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert await real_redis.get_pool_count("cpu") == 0
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
        # 计费接缝:输出 token 与成本必须落库(否则金额端点恒 0)。
        assert summary["total_input_tokens"] == 10
        assert summary["total_output_tokens"] == 20
        assert summary["total_cost_usd"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_cost_filled_from_litellm_pricing(self, jobs_app, jobs_client, db):
        """非 cli provider:api 侧 LiteLLM 价表命中 → 覆盖 worker 上报成本(权威,缓存感知)。"""
        jobs_app.state.pricing._table = {
            "claude-opus-4-8": {
                "input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05,
                "cache_read_input_token_cost": 5e-07,
            },
        }
        _, token = await _register_real(jobs_client)
        resp = await jobs_client.post(
            "/api/runner/usage",
            json={"exec_id": "p1", "provider": "anthropic", "model": "claude-opus-4-8",
                  "job_id": "jp", "step": "A", "input_tokens": 1_000_000, "output_tokens": 0,
                  "cache_read_input_tokens": 1_000_000, "cost_usd": 0.0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # input 5e-6*1e6 + cache_read 5e-7*1e6 = 5 + 0.5 = 5.5,覆盖上报的 0
        assert db.get_usage_summary(job_id="jp")["total_cost_usd"] == pytest.approx(5.5)

    @pytest.mark.asyncio
    async def test_claude_cli_cost_not_overridden(self, jobs_app, jobs_client, db):
        """claude-cli 订阅:用 CLI total_cost_usd(等价成本),价表不覆盖。"""
        jobs_app.state.pricing._table = {"claude-opus-4-8": {"input_cost_per_token": 5e-06}}
        _, token = await _register_real(jobs_client)
        await jobs_client.post(
            "/api/runner/usage",
            json={"exec_id": "p2", "provider": "claude-cli", "model": "claude-opus-4-8",
                  "job_id": "jc", "step": "A", "input_tokens": 1_000_000, "output_tokens": 0,
                  "cost_usd": 0.123},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert db.get_usage_summary(job_id="jc")["total_cost_usd"] == pytest.approx(0.123)

    @pytest.mark.asyncio
    async def test_duplicate_usage_not_double_billed(self, jobs_client, db):
        """同 exec_id 二次上报(worker 重试/双发)→ 200 ok 但不翻倍计费;端点 docstring 承诺去重。"""
        _, token = await _register_real(jobs_client)
        body = {"exec_id": "dup1", "provider": "anthropic", "model": "claude",
                "job_id": "j9", "step": "A", "input_tokens": 10,
                "output_tokens": 20, "cost_usd": 0.5}
        h = {"Authorization": f"Bearer {token}"}
        assert (await jobs_client.post("/api/runner/usage", json=body, headers=h)).status_code == 200
        assert (await jobs_client.post("/api/runner/usage", json=body, headers=h)).status_code == 200
        summary = db.get_usage_summary(job_id="j9")
        assert summary["calls"] == 1
        assert summary["total_cost_usd"] == pytest.approx(0.5)


# 产物代理端点:worker token 鉴权,经 API 读写 storage


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
    async def test_credential_sidecar_not_listed_or_served(self, jobs_client, test_config):
        """敏感凭证侧载文件:远端 worker 既列不到、也取不到(404),只供同机本地读。"""
        _, token = await _register_real(jobs_client)
        h = {"Authorization": f"Bearer {token}"}
        # 直接在 API 端 LocalStorage 放一个凭证文件 + 一个普通产物
        jd = test_config.jobs_dir / "j1"
        (jd / "input").mkdir(parents=True, exist_ok=True)
        (jd / "input" / ".credentials.json").write_text('{"sessdata": "SECRET"}')
        (jd / "output").mkdir(parents=True, exist_ok=True)
        (jd / "output" / "notes.md").write_text("hi")

        listed = (await jobs_client.get("/api/runner/jobs/j1/artifacts", headers=h)).json()
        assert "input/.credentials.json" not in listed["files"]
        assert "output/notes.md" in listed["files"]

        got = await jobs_client.get(
            "/api/runner/jobs/j1/artifacts/input/.credentials.json", headers=h,
        )
        assert got.status_code == 404

    @pytest.mark.asyncio
    async def test_get_missing_returns_404(self, jobs_client):
        _, token = await _register_real(jobs_client)
        resp = await jobs_client.get(
            "/api/runner/jobs/j1/artifacts/nope.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_traffic_counted_by_direction_and_worker(self, jobs_client, real_redis):
        """put 计入库方向 push,get 计出库方向 pull:按方向 + worker 归因计字节;404 不计。"""
        worker_id, token = await _register_real(jobs_client)
        h = {"Authorization": f"Bearer {token}"}
        # 入库:PUT body 5 字节
        await jobs_client.put("/api/runner/jobs/j1/artifacts/a.md", content=b"hello", headers=h)
        # 出库:GET 同一文件(5 字节)
        await jobs_client.get("/api/runner/jobs/j1/artifacts/a.md", headers=h)
        # 404 不计入出库
        await jobs_client.get("/api/runner/jobs/j1/artifacts/missing.md", headers=h)

        push = await real_redis.get_traffic("push")
        pull = await real_redis.get_traffic("pull")
        assert push["total"] == 5 and push["by_worker"] == {worker_id: 5}
        assert pull["total"] == 5 and pull["by_worker"] == {worker_id: 5}

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
    async def test_rel_absolute_and_null_rejected(self, jobs_client):
        # _validate_rel 不止挡 "..",绝对路径(/ 开头)与空字节也要 400
        _, token = await _register_real(jobs_client)
        h = {"Authorization": f"Bearer {token}"}
        # rel 以 / 开头(绝对路径)
        assert (
            await jobs_client.get("/api/runner/jobs/j1/artifacts//etc/passwd", headers=h)
        ).status_code == 400
        # rel 含空字节
        assert (
            await jobs_client.get("/api/runner/jobs/j1/artifacts/a%00b", headers=h)
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


class TestWorkerTokenThrottle:
    """无效 per-worker token 连续 401 → 达阈值返回 429+Retry-After(挡旧 worker 死刷 jobs/request);
    有效 token 命中即清计数,不被误限流。"""

    @pytest.mark.asyncio
    async def test_repeated_invalid_token_429_with_retry_after(self, client):
        from api import deps
        deps._AUTH_FAIL.clear()
        hdr = {
            "Authorization": "Bearer flwt-bogus-not-registered",
            "X-Worker-Id": "foreign-dl-old", "X-Worker-Host": "ecs-edge",
            "X-Worker-Version": "1.0.0",
        }
        body = {"pools": ["cpu"], "pool_limits": {}, "tags": [], "reject_tags": []}
        for _ in range(deps._AUTH_FAIL_THRESHOLD - 1):       # 阈值前:401
            r = await client.post("/api/runner/jobs/request", json=body, headers=hdr)
            assert r.status_code == 401
        r = await client.post("/api/runner/jobs/request", json=body, headers=hdr)
        assert r.status_code == 429                           # 达阈值:429
        assert r.headers.get("Retry-After") == str(deps._AUTH_RETRY_AFTER_SEC)

    @pytest.mark.asyncio
    async def test_valid_token_clears_counter(self, client):
        from api import deps
        reg = (await _register(client)).json()
        token, wid = reg["worker_token"], reg["worker_id"]
        h = hashlib.sha256(token.encode()).hexdigest()
        deps._AUTH_FAIL[h] = 99                                # 预置高计数(模拟历史失败)
        r = await client.post(
            "/api/runner/heartbeat",
            json={"worker_id": wid, "status": "idle"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code not in (401, 429)                # 有效 token 通过鉴权
        assert h not in deps._AUTH_FAIL                        # 计数已清


class TestRunnerPollAccessFilter:
    """runner 轮询端点(heartbeat/jobs/request)的 uvicorn access 记录被过滤掉,其余保留;为 dozzle 降噪。"""

    def test_filters_poll_endpoints_keeps_others(self):
        import logging
        from api.main import _RunnerPollAccessFilter
        f = _RunnerPollAccessFilter()

        def rec(path):
            return logging.LogRecord(
                "uvicorn.access", logging.INFO, "", 0,
                '%s - "%s %s HTTP/%s" %d',
                ("1.2.3.4", "POST", path, "1.1", 401), None,
            )
        assert f.filter(rec("/api/runner/jobs/request")) is False
        assert f.filter(rec("/api/runner/heartbeat")) is False
        assert f.filter(rec("/api/status")) is True
        assert f.filter(rec("/api/runner/register")) is True


class TestDispatchCredentials:
    """GET /api/runner/credentials/{key}(docs/03 §1.7.1):白名单 + 鉴权 + redis/DB 兜底。"""

    async def _register_worker(self, client):
        resp = await _register(client)
        body = resp.json()
        return body["worker_id"], body["worker_token"]

    @pytest.mark.asyncio
    async def test_requires_worker_token(self, client):
        resp = await client.get("/api/runner/credentials/bili_sessdata")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_key_404(self, client):
        _, token = await self._register_worker(client)
        resp = await client.get(
            "/api/runner/credentials/aws_root_key",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_serves_redis_mirror(self, client, redis_mock):
        _, token = await self._register_worker(client)
        redis_mock.get_dispatch_credential.return_value = "mirrored-sess"
        resp = await client.get(
            "/api/runner/credentials/bili_sessdata",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"key": "bili_sessdata", "value": "mirrored-sess"}

    @pytest.mark.asyncio
    async def test_miss_falls_back_to_db_and_remirrors(self, client, redis_mock, db):
        import json as _json
        _, token = await self._register_worker(client)
        redis_mock.get_dispatch_credential.return_value = None
        db.set_credential("bili_cookies", _json.dumps({"sessdata": "db-sess"}))
        resp = await client.get(
            "/api/runner/credentials/bili_sessdata",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.json()["value"] == "db-sess"
        redis_mock.set_dispatch_credential.assert_awaited_with("bili_sessdata", "db-sess")

    @pytest.mark.asyncio
    async def test_unconfigured_returns_null_value(self, client, redis_mock):
        _, token = await self._register_worker(client)
        redis_mock.get_dispatch_credential.return_value = None
        resp = await client.get(
            "/api/runner/credentials/youtube_cookies",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] is None


class TestConfigDispatch:
    """运行配置热下发(docs/03 §1.7.2):注册/心跳响应带 desired_config+cfg_rev;
    心跳回报 applied_cfg_rev 落 redis hash。"""

    @pytest.mark.asyncio
    async def test_register_response_carries_config(self, client, db):
        from shared.models import Worker as _W
        db.upsert_worker(_W(id="cpu-pre001", type="cpu", pools=["cpu"]))
        db.set_worker_desired_config("cpu-pre001", {"concurrency": 6})
        resp = await _register(client, worker_id="cpu-pre001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["desired_config"] == {"concurrency": 6} and body["cfg_rev"] == 1

    @pytest.mark.asyncio
    async def test_register_without_config_returns_null(self, client):
        resp = await _register(client)
        body = resp.json()
        assert body["desired_config"] is None and body["cfg_rev"] == 0

    @pytest.mark.asyncio
    async def test_heartbeat_reports_applied_and_returns_config(self, client, db, redis_mock):
        resp = await _register(client, worker_id="cpu-hb001")
        token = resp.json()["worker_token"]
        db.set_worker_desired_config("cpu-hb001", {"pools": ["cpu", "io"]})
        r = await client.post(
            "/api/runner/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={"worker_id": "cpu-hb001", "applied_cfg_rev": 3},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["desired_config"] == {"pools": ["cpu", "io"]} and body["cfg_rev"] == 1
        redis_mock.set_worker_field.assert_any_call("cpu-hb001", "cfg_applied_rev", "3")


@pytest.mark.asyncio
async def test_heartbeat_running_refreshes_step_progress(jobs_client, real_redis):
    """心跳捎带 running 集合 → 每个并发步的进度心跳被刷新(alive 单点不达的根治通道)。"""
    worker_id, token = await _register_real(jobs_client)
    r = await jobs_client.post("/api/runner/heartbeat",
                               headers={"Authorization": f"Bearer {token}"}, json={
        "worker_id": worker_id, "status": "busy",
        "running": [{"job_id": "j1", "step": "01_download"},
                    {"job_id": "j2", "step": "03_scene"}],
    })
    assert r.status_code == 200
    assert await real_redis.get_step_progress_at("j1", "01_download") is not None
    assert await real_redis.get_step_progress_at("j2", "03_scene") is not None
