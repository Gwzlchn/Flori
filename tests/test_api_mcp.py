"""MCP 接入信息端点(供系统页「接入 MCP」卡片)。"""

from __future__ import annotations

import pytest


class TestMcpInfo:
    @pytest.mark.asyncio
    async def test_info_lists_tools(self, client):
        r = await client.get("/api/mcp/info")
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is True
        assert d["http_path"] == "/mcp"
        assert d["stdio_module"] == "api.mcp_server"
        names = {t["name"] for t in d["tools"]}
        assert {"list_knowledge_bases", "search", "get_note"} <= names
        # 描述非空(取 docstring 首行)
        assert all(t["description"] for t in d["tools"])

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
