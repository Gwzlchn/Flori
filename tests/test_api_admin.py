"""api/routes/admin.py 测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.main import create_app


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.ping = AsyncMock(return_value=True)
    r.get_pool_count = AsyncMock(return_value=0)
    r.get_queue_info = AsyncMock(return_value={"length": 0})
    r.get_all_pool_limit_overrides = AsyncMock(return_value={})
    r.publish = AsyncMock()
    # 组件探测(build_full_status):scheduler 心跳缺失→unknown;redis server_info 给一份;events 空。
    r.get_component_heartbeat = AsyncMock(return_value=None)
    r.server_info = AsyncMock(return_value={
        "version": "7.2.4", "ping_ms": 1.0, "used_memory_human": "1.0M",
        "used_memory_mb": 1.0, "maxmemory_mb": 0.0, "uptime_sec": 100,
        "connected_clients": 1,
    })
    # 中转流量(build_full_status 读 pull/push 总量);裸 AsyncMock 的 await→AsyncMock 会 500。
    r.get_traffic = AsyncMock(return_value={"total": 0, "by_worker": {}})
    r.list_worker_ids = AsyncMock(return_value=[])
    r.get_worker_info = AsyncMock(return_value={})
    r.r = MagicMock()
    r.r.lrange = AsyncMock(return_value=[])
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


def _db_worker(*, status: str = "online-idle", pools: list[str] | None = None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id="db-worker", type="cpu", pools=pools or ["cpu"], tags=set(), reject_tags=set(),
        hostname="test", gpu_name=None, gpu_memory_mb=None, concurrency=1, remote_addr=None,
        status=status, current_job=None, current_step=None, tasks_completed=0, tasks_failed=0,
        total_duration_sec=0.0, first_seen=now, started_at=now, last_heartbeat=now,
        admin_note=None, desired_config=None, cfg_rev=0,
    )


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_reports_not_ready_without_scheduler_or_workers(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["ready"] is False
        assert data["checks"]["redis"]["status"] == "ok"
        assert data["checks"]["db"]["status"] == "ok"
        assert data["checks"]["scheduler"]["status"] == "error"
        assert data["checks"]["pool:io"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_ready_endpoint_healthy_core_and_optional_gpu_degraded(
        self, client, mock_redis, db, monkeypatch,
    ):
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": datetime.now(timezone.utc).isoformat(),
            "version": "test",
            "loop_lag_sec": "0",
        })
        workers = [_db_worker(pools=["io", "cpu", "ai"])]
        monkeypatch.setattr(db, "list_workers", lambda *_args: workers)

        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert resp.status_code == 200
        assert data["status"] == "degraded"
        assert data["ready"] is True
        assert data["checks"]["pool:gpu"]["required"] is False
        assert data["checks"]["pool:gpu"]["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_ready_endpoint_returns_503_for_required_pool_offline(
        self, client, mock_redis,
    ):
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": datetime.now(timezone.utc).isoformat(), "loop_lag_sec": "0",
        })
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 503
        assert resp.json()["ready"] is False

    @pytest.mark.asyncio
    async def test_liveness_survives_dependency_failure(self, client, mock_redis):
        mock_redis.server_info = AsyncMock(side_effect=Exception("down"))
        resp = await client.get("/api/health/live")
        assert resp.status_code == 200
        assert resp.json()["alive"] is True

    @pytest.mark.asyncio
    async def test_health_redis_down(self, client, mock_redis):
        mock_redis.server_info = AsyncMock(side_effect=Exception("down"))
        resp = await client.get("/api/health")
        data = resp.json()
        assert data["checks"]["redis"]["status"] == "error"
        assert data["status"] == "not_ready"

    @pytest.mark.asyncio
    async def test_data_readonly_blocks_readiness(self, client, monkeypatch):
        def denied(_path):
            raise PermissionError("read-only")

        monkeypatch.setattr("api.routes.admin._probe_data_path", denied)
        data = (await client.get("/api/health")).json()
        assert data["checks"]["data_writable"]["status"] == "error"
        assert any(r["code"] == "data_writable" for r in data["reasons"])

    @pytest.mark.asyncio
    async def test_disk_below_threshold_blocks_readiness(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.admin.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=1024**3),
        )
        data = (await client.get("/api/health")).json()
        assert data["checks"]["disk"]["status"] == "error"
        assert data["checks"]["disk"]["free_gb"] == 1.0

    @pytest.mark.asyncio
    async def test_disk_threshold_boundary_is_ready_for_disk_check(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.admin.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=100 * 1024**3, used=95 * 1024**3, free=5 * 1024**3),
        )
        data = (await client.get("/api/health")).json()
        assert data["checks"]["disk"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_required_workers_recover_without_restarting_api(
        self, client, mock_redis, db, monkeypatch,
    ):
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": datetime.now(timezone.utc).isoformat(), "loop_lag_sec": "0",
        })
        workers: list[SimpleNamespace] = []
        monkeypatch.setattr(db, "list_workers", lambda *_args: workers)

        blocked = await client.get("/api/health/ready")
        assert blocked.status_code == 503

        workers.append(_db_worker(pools=["io", "cpu", "ai"]))
        recovered = await client.get("/api/health/ready")
        assert recovered.status_code == 200
        assert recovered.json()["ready"] is True

    @pytest.mark.asyncio
    async def test_redis_only_multi_pool_worker_satisfies_readiness(
        self, client, mock_redis,
    ):
        now = datetime.now(timezone.utc).isoformat()
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": now, "version": "test", "loop_lag_sec": "0",
        })
        mock_redis.list_worker_ids = AsyncMock(return_value=["remote-1"])
        mock_redis.get_worker_info = AsyncMock(return_value={
            "type": "cpu", "pools": "io,cpu,ai", "last_heartbeat": now,
            "started_at": now, "admin_status": "", "current_job": "",
        })

        response = await client.get("/api/health/ready")

        assert response.status_code == 200
        checks = response.json()["checks"]
        assert checks["workers"]["online"] == 1
        assert all(checks[f"pool:{pool}"]["online"] == 1 for pool in ("io", "cpu", "ai"))
        live = (await client.get("/api/status")).json()
        assert all(live["workers"][pool]["online"] == 1 for pool in ("io", "cpu", "ai"))

    @pytest.mark.asyncio
    async def test_configured_heartbeat_window_is_used_for_redis_worker(
        self, client, mock_redis, test_config,
    ):
        now = datetime.now(timezone.utc)
        test_config.pools["worker_status"]["online_window_sec"] = 60
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": now.isoformat(), "loop_lag_sec": "0",
        })
        mock_redis.list_worker_ids = AsyncMock(return_value=["remote-window"])
        mock_redis.get_worker_info = AsyncMock(return_value={
            "type": "cpu", "pools": "io,cpu,ai",
            "last_heartbeat": (now - timedelta(seconds=40)).isoformat(),
            "started_at": now.isoformat(), "admin_status": "", "current_job": "",
        })

        response = await client.get("/api/health/ready")

        assert response.status_code == 200
        assert response.json()["checks"]["workers"]["online"] == 1

    @pytest.mark.asyncio
    async def test_all_required_workers_paused_blocks_with_explicit_reason(
        self, client, mock_redis,
    ):
        now = datetime.now(timezone.utc).isoformat()
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": now, "loop_lag_sec": "0",
        })
        mock_redis.list_worker_ids = AsyncMock(return_value=["paused-1"])
        mock_redis.get_worker_info = AsyncMock(return_value={
            "type": "cpu", "pools": "io,cpu,ai", "last_heartbeat": now,
            "started_at": now, "admin_status": "paused", "current_job": "",
        })

        response = await client.get("/api/health/ready")

        assert response.status_code == 503
        check = response.json()["checks"]["pool:ai"]
        assert check["online"] == 0 and check["paused"] == 1
        assert "全部暂停" in check["detail"]

    @pytest.mark.asyncio
    async def test_degraded_scheduler_does_not_turn_readiness_into_503(
        self, client, mock_redis,
    ):
        now = datetime.now(timezone.utc)
        mock_redis.get_component_heartbeat = AsyncMock(return_value={
            "ts": (now - timedelta(seconds=60)).isoformat(), "loop_lag_sec": "0",
        })
        mock_redis.list_worker_ids = AsyncMock(return_value=["remote-1"])
        mock_redis.get_worker_info = AsyncMock(return_value={
            "type": "cpu", "pools": "io,cpu,ai", "last_heartbeat": now.isoformat(),
            "started_at": now.isoformat(), "admin_status": "", "current_job": "",
        })

        response = await client.get("/api/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["checks"]["scheduler"]["status"] == "degraded"


class TestReadinessProbes:
    def test_sqlite_probe_executes_wal_write_and_leaves_no_schema(self, db, test_config):
        from api.routes.admin import _probe_sqlite_write

        result = _probe_sqlite_write(test_config.db_path)

        assert result == {"journal_mode": "wal"}
        connection = sqlite3.connect(test_config.db_path)
        try:
            names = connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '__flori_readiness_%'",
            ).fetchall()
        finally:
            connection.close()
        assert names == []

    @pytest.mark.asyncio
    async def test_expensive_probe_is_singleflight_and_short_cached(self, app):
        from api.routes.admin import _singleflight_probe

        calls = 0

        async def probe():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"value": calls}

        results = await asyncio.gather(*[
            _singleflight_probe(
                app, "canary", ttl_sec=5, timeout_sec=1, probe=probe,
            )
            for _ in range(20)
        ])
        cached = await _singleflight_probe(
            app, "canary", ttl_sec=5, timeout_sec=1, probe=probe,
        )

        assert calls == 1
        assert all(result["ok"] for result in results)
        assert cached["value"] == {"value": 1}

    @pytest.mark.asyncio
    async def test_probe_timeout_is_cached_fail_closed(self, app):
        from api.routes.admin import _singleflight_probe

        calls = 0

        async def probe():
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)

        first = await _singleflight_probe(
            app, "timeout", ttl_sec=5, timeout_sec=0.01, probe=probe,
        )
        second = await _singleflight_probe(
            app, "timeout", ttl_sec=5, timeout_sec=0.01, probe=probe,
        )

        assert first == second == {"ok": False, "error_type": "TimeoutError"}
        assert calls == 1


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_prometheus_text(self, client):
        resp = await client.get("/api/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "flori_up 1" in body
        assert "flori_ready 0" in body
        assert "flori_redis_up 1" in body
        assert "flori_db_up 1" in body
        assert "flori_workers_online" in body
        assert "flori_disk_free_gb" in body

    @pytest.mark.asyncio
    async def test_metrics_redis_down_reflected(self, client, mock_redis):
        mock_redis.server_info = AsyncMock(side_effect=Exception("down"))
        body = (await client.get("/api/metrics")).text
        assert "flori_redis_up 0" in body

    @pytest.mark.asyncio
    async def test_metrics_db_worker_query_failure_never_500(self, client, app, db, monkeypatch):
        monkeypatch.setattr(db, "list_jobs", MagicMock(side_effect=OSError("db unavailable")))
        monkeypatch.setattr(db, "list_workers", MagicMock(side_effect=OSError("db unavailable")))
        monkeypatch.setattr(
            "api.routes.admin._probe_sqlite_write",
            MagicMock(side_effect=OSError("db unavailable")),
        )
        app.state.readiness_probe_cache = {}
        resp = await client.get("/api/metrics")
        assert resp.status_code == 200
        assert "flori_db_up 0" in resp.text
        assert "flori_workers_total 0" in resp.text


class TestStatus:
    @pytest.mark.asyncio
    async def test_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        # 空 db + mock redis(pool/queue=0):断具体值,而非只断 key 存在(后者源码返错结构也假绿)。
        assert data["workers"] == {}                       # 无 worker
        assert data["jobs"]["total"] == 0 and data["jobs"]["pending"] == 0
        assert data["pools"] and all(                      # 每个池 used/queue 归零
            p["used"] == 0 and p["queue"] == 0 for p in data["pools"].values())
        assert "available_gb" in data["disk"]
        assert "total_gb" in data["disk"] and "used_pct" in data["disk"]
        assert "version" in data
        assert data["health"]["status"] == "not_ready"
        assert data["throughput_1h"] == {"done": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_status_components_ordered(self, client):
        """components 为有序数组,顺序固定 api→scheduler→redis→minio。"""
        data = (await client.get("/api/status")).json()
        comps = data["components"]
        assert [c["kind"] for c in comps] == ["api", "scheduler", "redis", "minio"]
        api = next(c for c in comps if c["kind"] == "api")
        assert api["status"] == "up"   # API 能响应即 up
        sched = next(c for c in comps if c["kind"] == "scheduler")
        assert sched["status"] == "unknown"   # 无心跳 → unknown(不误报挂)
        redis_c = next(c for c in comps if c["kind"] == "redis")
        assert redis_c["status"] == "up" and redis_c["version"] == "7.2.4"
        minio_c = next(c for c in comps if c["kind"] == "minio")
        # 测试 storage=LocalStorage(create_storage 无 MINIO_URL)→ mode=local/unknown,不标红。
        assert minio_c["status"] == "unknown" and minio_c["extra"]["mode"] == "local"

    @pytest.mark.asyncio
    async def test_status_redis_probe_timeout_never_500(self, client, mock_redis):
        """redis server_info 抛异常 → redis 组件 unknown + detail,/api/status 不 500。"""
        from unittest.mock import AsyncMock as _AM
        mock_redis.server_info = _AM(side_effect=Exception("conn refused"))
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        redis_c = next(c for c in resp.json()["components"] if c["kind"] == "redis")
        assert redis_c["status"] == "unknown"
        assert redis_c["detail"] == "redis 探活失败: Exception"

    @pytest.mark.asyncio
    async def test_status_live_fragment_failure_never_500(self, client, mock_redis):
        mock_redis.get_all_pool_limit_overrides = AsyncMock(side_effect=ConnectionError("down"))
        mock_redis.server_info = AsyncMock(side_effect=ConnectionError("down"))
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pools"] == {}
        assert data["health"]["ready"] is False
        assert data["health"]["checks"]["redis"]["status"] == "error"


class TestUsageAggregate:
    @pytest.mark.asyncio
    async def test_usage_empty(self, client):
        data = (await client.get("/api/usage")).json()
        assert data["calls"] == 0 and data["total_cost_usd"] == 0
        assert data["cache_hit_rate_pct"] == 0.0 and data["by_model"] == []

    @pytest.mark.asyncio
    async def test_usage_aggregate_hit_rate_by_model(self, client, db):
        from datetime import datetime, timezone
        from shared.models import AIUsage
        db.record_ai_usage(AIUsage(
            exec_id="e1", job_id="j1", step="s", worker_id="w1",
            provider="anthropic", model="claude-x",
            input_tokens=100, output_tokens=50,
            cache_creation_input_tokens=20, cache_read_input_tokens=80,
            cost_usd=0.5, duration_sec=2.0, num_turns=3, cached=True,
            created_at=datetime.now(timezone.utc),
        ))
        data = (await client.get("/api/usage")).json()
        assert data["calls"] == 1
        assert data["total_cache_read_tokens"] == 80
        # 命中率 = 80/(100+20+80) = 40%
        assert data["cache_hit_rate_pct"] == 40.0
        assert len(data["by_model"]) == 1
        assert data["by_model"][0]["model"] == "claude-x"


class TestEvents:
    @pytest.mark.asyncio
    async def test_events_empty(self, client):
        data = (await client.get("/api/events")).json()
        assert data == {"events": []}

    @pytest.mark.asyncio
    async def test_events_reads_redis_list(self, client, mock_redis):
        from unittest.mock import AsyncMock as _AM
        mock_redis.r.lrange = _AM(return_value=[
            '{"ts": 1.0, "kind": "no_worker", "job_id": "j1"}',
            'not-json',  # 坏行跳过,不报错
        ])
        data = (await client.get("/api/events?limit=10")).json()
        assert len(data["events"]) == 1
        assert data["events"][0]["kind"] == "no_worker"


class TestPoolsConfig:
    @pytest.mark.asyncio
    async def test_get_pools(self, client):
        resp = await client.get("/api/config/pools")
        assert resp.status_code == 200
        assert "pools" in resp.json()

    @pytest.mark.asyncio
    async def test_get_pool_limits(self, client):
        resp = await client.get("/api/config/pool-limits")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    @pytest.mark.asyncio
    async def test_put_pool_limit_unknown_400(self, client):
        resp = await client.put("/api/config/pool-limits", json={"no_such_pool": 1})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_put_pool_limit_valid(self, client, mock_redis):
        pools = (await client.get("/api/config/pool-limits")).json()
        pool = next(iter(pools), None)
        if pool:
            resp = await client.put("/api/config/pool-limits", json={pool: 256})
            assert resp.status_code == 200
            mock_redis.set_pool_limit_override.assert_awaited_with(pool, 256)


class TestStylesConfig:
    @pytest.mark.asyncio
    async def test_get_styles_empty_when_no_dir(self, client):
        resp = await client.get("/api/config/styles")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_get_styles_reads_yaml(self, client, test_config):
        styles_dir = test_config.prompts_dir / "styles"
        styles_dir.mkdir(parents=True, exist_ok=True)
        (styles_dir / "lecture.yaml").write_text("tag: lecture\nname: 课堂\n")
        (styles_dir / "talk.yaml").write_text("name: 演讲\n")  # no tag -> falls back to stem
        resp = await client.get("/api/config/styles")
        assert resp.status_code == 200
        body = resp.json()
        assert "lecture" in body
        assert "talk" in body


class TestPricing:
    @pytest.mark.asyncio
    async def test_status_fresh_store(self, client):
        # 空表(测试态从未拉取):ready=False、0 模型、fetched_at=None、source_url 为 LiteLLM 常量。
        resp = await client.get("/api/pricing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False
        assert body["model_count"] == 0
        assert body["fetched_at"] is None
        assert "litellm" in body["source_url"].lower()

    @pytest.mark.asyncio
    async def test_refresh_success_updates_status(self, client, app, monkeypatch):
        # 手动更新成功:拉到表 → 200 + ready/model_count/fetched_at 全到位。
        import api.pricing_store as ps

        async def fake_fetch(*a, **k):
            return {"claude-opus-4-8": {"input_cost_per_token": 5e-06}}

        monkeypatch.setattr(ps, "fetch_litellm_pricing", fake_fetch)
        resp = await client.post("/api/pricing/refresh")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["model_count"] == 1
        assert body["fetched_at"] is not None
        # 内存表已更新,后续 GET /api/pricing 一致。
        assert app.state.pricing.model_count == 1

    @pytest.mark.asyncio
    async def test_refresh_failure_returns_502(self, client, monkeypatch):
        # 上游拉取失败:不 crash,回 502 + 保留旧表(此处空表)。
        import api.pricing_store as ps

        async def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(ps, "fetch_litellm_pricing", boom)
        resp = await client.post("/api/pricing/refresh")
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_raw_returns_table(self, client, app):
        app.state.pricing._table = {"gpt-4o": {"input_cost_per_token": 2.5e-06}}
        resp = await client.get("/api/pricing/raw")
        assert resp.status_code == 200
        assert resp.json() == {"gpt-4o": {"input_cost_per_token": 2.5e-06}}
