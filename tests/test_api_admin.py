"""tests for api/routes/admin.py"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import load_config
from shared.db import Database
from api.main import create_app


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
def mock_redis():
    r = AsyncMock()
    r.ping = AsyncMock(return_value=True)
    r.get_pool_count = AsyncMock(return_value=0)
    r.get_queue_info = AsyncMock(return_value={"length": 0})
    r.publish = AsyncMock()
    return r


@pytest.fixture
def app(db, mock_redis, test_config):
    return create_app(db=db, redis=mock_redis, config=test_config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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


class TestStatus:
    @pytest.mark.asyncio
    async def test_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "workers" in data
        assert "pools" in data
        assert "jobs" in data
        assert "disk" in data


class TestPoolsConfig:
    @pytest.mark.asyncio
    async def test_get_pools(self, client):
        resp = await client.get("/api/config/pools")
        assert resp.status_code == 200
        assert "pools" in resp.json()


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
