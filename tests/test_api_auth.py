"""tests for api/routes/auth.py"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

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
def app(db, test_config):
    return create_app(db=db, redis=AsyncMock(), config=test_config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAuthStatus:
    @pytest.mark.asyncio
    async def test_no_cookies(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("api.routes.auth.COOKIES_DIR", tmp_path / "cookies")
        resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bilibili"]["has_cookies"] is False
        assert data["youtube"]["has_cookies"] is False


class TestBilibiliQrcode:
    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_qrcode_success(self, mock_client_cls, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "data": {"url": "https://qr.example.com", "qrcode_key": "key123"},
        }

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_resp)))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_ctx

        resp = await client.post("/api/auth/bilibili/qrcode")
        assert resp.status_code == 200
        assert resp.json()["qrcode_key"] == "key123"

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_qrcode_waf_412_returns_502(self, mock_client_cls, client):
        """B站 WAF 返回 412 + HTML（非 JSON）时应返回友好 502 而非 500。"""
        import json as _json

        mock_resp = MagicMock()
        mock_resp.status_code = 412
        mock_resp.json.side_effect = _json.JSONDecodeError("x", "doc", 0)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_resp)))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_ctx

        resp = await client.post("/api/auth/bilibili/qrcode")
        assert resp.status_code == 502

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_qrcode_sends_browser_ua(self, mock_client_cls, client):
        """请求必须携带浏览器 User-Agent，否则会被 B站 WAF 拦截。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0, "data": {"url": "u", "qrcode_key": "k"},
        }
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_resp)))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_ctx

        await client.post("/api/auth/bilibili/qrcode")
        # AsyncClient(headers=...) 被传入了浏览器 UA
        _, kwargs = mock_client_cls.call_args
        assert "User-Agent" in kwargs.get("headers", {})
        assert "Mozilla" in kwargs["headers"]["User-Agent"]


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


class TestTokenAuth:
    """Test verify_token middleware with API_TOKEN set."""

    @pytest.mark.asyncio
    async def test_no_token_returns_401(self, test_config, db):
        """Request without Bearer token should get 401."""
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
