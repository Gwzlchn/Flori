"""tests for api/routes/admin.py"""

from __future__ import annotations

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
    r.r = MagicMock()
    r.r.lrange = AsyncMock(return_value=[])
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["checks"]["redis"] == "ok"
        assert data["checks"]["db"] == "ok"

    @pytest.mark.asyncio
    async def test_health_redis_down(self, client, mock_redis):
        mock_redis.ping = AsyncMock(side_effect=Exception("down"))
        resp = await client.get("/api/health")
        data = resp.json()
        assert data["checks"]["redis"] == "error"
        assert data["status"] == "unhealthy"


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_prometheus_text(self, client):
        resp = await client.get("/api/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "flori_up 1" in body
        assert "flori_redis_up 1" in body
        assert "flori_db_up 1" in body
        assert "flori_workers_online" in body
        assert "flori_disk_free_gb" in body

    @pytest.mark.asyncio
    async def test_metrics_redis_down_reflected(self, client, mock_redis):
        mock_redis.ping = AsyncMock(side_effect=Exception("down"))
        body = (await client.get("/api/metrics")).text
        assert "flori_redis_up 0" in body


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
        # 批3 新增:disk 补 total_gb/used_pct、version、throughput_1h。
        assert "total_gb" in data["disk"] and "used_pct" in data["disk"]
        assert "version" in data
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
        assert redis_c["detail"] and "conn refused" in redis_c["detail"]


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
