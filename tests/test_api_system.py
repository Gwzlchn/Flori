"""tests for batch-2 system endpoints: /api/pipelines, /api/providers, /api/workers/registration-token"""

import pytest
from unittest.mock import AsyncMock
from httpx import ASGITransport, AsyncClient

from api.main import create_app


@pytest.mark.asyncio
async def test_pipelines_endpoint(client):
    r = await client.get("/api/pipelines")
    assert r.status_code == 200
    pipelines = r.json()["pipelines"]
    names = {p["name"] for p in pipelines}
    assert "video" in names            # 真实 configs/pipelines.yaml 有 video
    # 模板 / '.'前缀 / default 不算 pipeline
    assert not any(n.startswith(".") or n == "default" for n in names)
    vid = next(p for p in pipelines if p["name"] == "video")
    assert vid["steps"], "video 应有步骤"
    assert all("key" in s and "label" in s for s in vid["steps"])


@pytest.mark.asyncio
async def test_registration_token_status(db, test_config):
    redis = AsyncMock()
    redis.get_registration_token.return_value = "flw-abc"
    redis.get_registration_token_ttl.return_value = 3600
    app = create_app(db=db, redis=redis, config=test_config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/workers/registration-token")
    assert r.status_code == 200
    assert r.json() == {"exists": True, "expires_in_sec": 3600}


@pytest.mark.asyncio
async def test_registration_token_absent(db, test_config):
    redis = AsyncMock()
    redis.get_registration_token.return_value = None
    app = create_app(db=db, redis=redis, config=test_config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/workers/registration-token")
    assert r.json() == {"exists": False, "expires_in_sec": None}
