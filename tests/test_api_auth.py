"""api/routes/auth.py 测试。"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.main import create_app


@pytest.fixture
def app(db, test_config):
    return create_app(db=db, redis=AsyncMock(), config=test_config)


class TestAuthStatus:
    @pytest.mark.asyncio
    async def test_no_cookies(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("api.routes.auth.COOKIES_DIR", tmp_path / "cookies")
        resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bilibili"]["has_cookies"] is False
        assert data["youtube"]["has_cookies"] is False


class TestYoutubeCookies:
    @pytest.mark.asyncio
    async def test_upload_cookies(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("api.routes.auth.COOKIES_DIR", tmp_path / "cookies")
        resp = await client.post(
            "/api/auth/youtube/cookies",
            files={"file": ("cookies.txt", b"cookie content", "text/plain")},
        )
        assert resp.status_code == 200
        assert (tmp_path / "cookies" / "youtube.txt").exists()

    @pytest.mark.asyncio
    async def test_upload_cookies_too_large_rejected(self, client, tmp_path, monkeypatch):
        """超过 1 MiB 上限的 cookie 上传 → 413,且不落盘。"""
        monkeypatch.setattr("api.routes.auth.COOKIES_DIR", tmp_path / "cookies")
        big = b"x" * (1024 * 1024 + 10)
        resp = await client.post(
            "/api/auth/youtube/cookies",
            files={"file": ("cookies.txt", big, "text/plain")},
        )
        assert resp.status_code == 413
        assert not (tmp_path / "cookies" / "youtube.txt").exists()


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

    @pytest.mark.asyncio
    async def test_no_token_no_optin_fails_closed(self, test_config, db):
        """未设 API_TOKEN 且未显式 API_ALLOW_NO_AUTH=1 → 503,fail-closed 不静默裸奔。"""
        with patch.dict(os.environ, {"API_TOKEN": "", "API_ALLOW_NO_AUTH": ""}):
            app = create_app(db=db, redis=AsyncMock(), config=test_config)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/jobs")
                assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_no_token_with_optin_allows(self, test_config, db):
        """未设 API_TOKEN 但 API_ALLOW_NO_AUTH=1(可信内网)→ 放行。"""
        with patch.dict(os.environ, {"API_TOKEN": "", "API_ALLOW_NO_AUTH": "1"}):
            app = create_app(db=db, redis=AsyncMock(), config=test_config)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.get("/api/jobs")
                assert resp.status_code == 200
