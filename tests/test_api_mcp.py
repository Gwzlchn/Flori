"""MCP 接入信息端点(供系统页「接入 MCP」卡片)。"""

from __future__ import annotations

import pytest

from api.main import create_app
from httpx import ASGITransport, AsyncClient
from tests.conftest import make_fakeredis

_LOCAL_MCP_HOST = "127.0.0.1"
_PUBLIC_MCP_PORT = "18090"


class TestMcpInfo:
    @pytest.mark.asyncio
    async def test_info_lists_tools(self, client):
        r = await client.get("/api/mcp/info")
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is True
        assert d["http_path"] == "/mcp"
        # 统一走 HTTP:本地端点直连 mcp-http(127.0.0.1:<MCP_PORT>/mcp),无 stdio_module 字段。
        assert d["local_url"].startswith("http://127.0.0.1:") and d["local_url"].endswith("/mcp")
        names = {t["name"] for t in d["tools"]}
        assert {"list_knowledge_bases", "search", "get_note"} <= names
        # 描述非空(取 docstring 首行)
        assert all(t["description"] for t in d["tools"])

    @pytest.mark.asyncio
    async def test_info_local_url_uses_public_mcp_port(self, client, monkeypatch):
        monkeypatch.setenv("FLORI_MCP_PUBLIC_PORT", _PUBLIC_MCP_PORT)
        monkeypatch.setenv("MCP_PORT", "8090")
        r = await client.get("/api/mcp/info")
        assert r.status_code == 200
        assert r.json()["local_url"] == f"http://{_LOCAL_MCP_HOST}:{_PUBLIC_MCP_PORT}/mcp"

    @pytest.mark.asyncio
    async def test_info_stats_zero_without_redis(self, client):
        """默认 fixture 的 redis 是 AsyncMock(无真实计数)→ stats 须为零值,且端点不 5xx。"""
        r = await client.get("/api/mcp/info")
        assert r.status_code == 200
        stats = r.json()["stats"]
        assert isinstance(stats, dict)
        assert stats["total"] == 0
        assert stats["by_tool"] == {}

    @pytest.mark.asyncio
    async def test_info_stats_reads_counters(self, db, test_config):
        """有真实(fake)redis 且已写入计数 → /api/mcp/info 读出 total + by_tool。"""
        redis = make_fakeredis()
        await redis.r.set("mcp:calls:total", 5)
        await redis.r.set("mcp:calls:tool:search", 3)
        await redis.r.set("mcp:calls:tool:get_note", 2)
        app = create_app(db=db, redis=redis, config=test_config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            stats = (await c.get("/api/mcp/info")).json()["stats"]
        assert stats["total"] == 5
        assert stats["by_tool"] == {"search": 3, "get_note": 2}
        await redis.close()

    @pytest.mark.asyncio
    async def test_info_token_configured_flag(self, client, monkeypatch):
        monkeypatch.setenv("FLORI_MCP_TOKEN", "x")
        assert (await client.get("/api/mcp/info")).json()["token_configured"] is True
        monkeypatch.delenv("FLORI_MCP_TOKEN", raising=False)
        assert (await client.get("/api/mcp/info")).json()["token_configured"] is False

    @pytest.mark.asyncio
    async def test_token_endpoint(self, client, monkeypatch):
        monkeypatch.setenv("FLORI_MCP_TOKEN", "tok-test-xyz")
        assert (await client.get("/api/mcp/token")).json()["token"] == "tok-test-xyz"

    @pytest.mark.asyncio
    async def test_token_null_when_unset(self, client, monkeypatch):
        monkeypatch.delenv("FLORI_MCP_TOKEN", raising=False)
        assert (await client.get("/api/mcp/token")).json()["token"] is None
