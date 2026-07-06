"""api/routes/auth.py 测试:凭证入库 + 镜像分发(cookie 文件共享已废除)。"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app


@pytest.fixture
def redis_mock():
    return AsyncMock()


@pytest.fixture
def app(db, test_config, redis_mock):
    return create_app(db=db, redis=redis_mock, config=test_config)


class TestAuthStatus:
    @pytest.mark.asyncio
    async def test_nothing_configured(self, client):
        resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bilibili"]["has_cookies"] is False
        assert data["youtube"]["has_cookies"] is False

    @pytest.mark.asyncio
    async def test_reflects_db_credentials(self, client, db):
        db.set_credential("bili_cookies", json.dumps({"sessdata": "s3ss"}))
        db.set_credential("youtube_cookies", "# netscape")
        resp = await client.get("/api/auth/status")
        data = resp.json()
        assert data["bilibili"]["status"] == "ok"
        assert data["youtube"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_bili_without_sessdata_is_missing(self, client, db):
        # 扫码残缺(无 sessdata 字段)不算已配置。
        db.set_credential("bili_cookies", json.dumps({"uname": "x"}))
        resp = await client.get("/api/auth/status")
        assert resp.json()["bilibili"]["has_cookies"] is False


class TestYoutubeCookies:
    @pytest.mark.asyncio
    async def test_upload_stores_db_and_mirrors_redis(self, client, db, redis_mock):
        resp = await client.post(
            "/api/auth/youtube/cookies",
            files={"file": ("cookies.txt", b"# netscape cookie content", "text/plain")},
        )
        assert resp.status_code == 200
        assert db.get_credential("youtube_cookies") == "# netscape cookie content"
        redis_mock.set_dispatch_credential.assert_awaited_once_with(
            "youtube_cookies", "# netscape cookie content")

    @pytest.mark.asyncio
    async def test_unknown_platform_400(self, client, db):
        resp = await client.post(
            "/api/auth/evil/cookies",
            files={"file": ("x.txt", b"data", "text/plain")},
        )
        assert resp.status_code == 400
        assert db.get_credential("evil_cookies") is None

    @pytest.mark.asyncio
    async def test_upload_cookies_too_large_rejected(self, client, db):
        """超过 1 MiB 上限的 cookie 上传 → 413,且不入库。"""
        big = b"x" * (1024 * 1024 + 10)
        resp = await client.post(
            "/api/auth/youtube/cookies",
            files={"file": ("cookies.txt", big, "text/plain")},
        )
        assert resp.status_code == 413
        assert db.get_credential("youtube_cookies") is None


class TestTokenAuth:
    """verify_token 中间件:设置 API_TOKEN 后的鉴权行为。"""

    @pytest.mark.asyncio
    async def test_no_token_returns_401(self, test_config, db):
        with patch.dict(os.environ, {"API_TOKEN": "secret123"}):
            app = create_app(db=db, redis=AsyncMock(), config=test_config)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/jobs")
                assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_returns_401(self, test_config, db):
        with patch.dict(os.environ, {"API_TOKEN": "secret123"}):
            app = create_app(db=db, redis=AsyncMock(), config=test_config)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/jobs", headers={"Authorization": "Bearer wrong"})
                assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_token_passes(self, test_config, db):
        with patch.dict(os.environ, {"API_TOKEN": "secret123"}):
            app = create_app(db=db, redis=AsyncMock(), config=test_config)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/jobs", headers={"Authorization": "Bearer secret123"})
                assert resp.status_code == 200
