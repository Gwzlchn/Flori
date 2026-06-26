"""MCP 接入信息(供「系统页 · 接入 MCP」卡片渲染)。

只读:工具清单(从 MCP server 实时派生,不写死,防漂移)、是否已配 token、传输路径。
token 明文单独经 /api/mcp/token 取(前端默认遮掩、点击才显示/复制)——避免每次 info 都带明文。
鉴权:挂在 /api 下,经 Caddy basic_auth(公网)/ API_ALLOW_NO_AUTH(可信内网)收口。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from api.deps import get_db, get_redis, get_storage, verify_token
from shared.db import Database
from shared.redis_client import RedisClient
from shared.storage import StorageBackend

router = APIRouter(prefix="/api/mcp", tags=["mcp"], dependencies=[Depends(verify_token)])


async def _mcp_stats(redis: RedisClient) -> dict:
    """MCP 工具调用计数(best-effort):redis 缺失/异常/形态不符 → 零值,不让 info 端点 5xx。"""
    try:
        stats = await redis.get_mcp_call_stats()
        if (
            isinstance(stats, dict)
            and isinstance(stats.get("total"), int)
            and isinstance(stats.get("by_tool"), dict)
        ):
            return {"total": stats["total"], "by_tool": stats["by_tool"]}
    except Exception:
        pass
    return {"total": 0, "by_tool": {}}


@router.get("/info")
async def mcp_info(
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
) -> dict:
    """MCP 接入信息:工具清单(实时派生)、传输、是否已配 token、调用统计。不回传 token 明文。"""
    from api.mcp_server.server import build_server

    mcp = build_server(db, storage)
    tools = await mcp.list_tools()
    return {
        "enabled": True,
        "http_path": "/mcp",  # 公网端点 = <当前站点 origin> + 此路径(前端据 window.location 拼)
        "stdio_module": "api.mcp_server",  # 本地 stdio:python -m <此>
        "token_configured": bool(os.environ.get("FLORI_MCP_TOKEN")),
        "tools": [
            {"name": t.name, "description": (t.description or "").strip().splitlines()[0]}
            for t in tools
        ],
        "stats": await _mcp_stats(redis),  # {total, by_tool};redis 不可用 → 0s
    }


@router.get("/token")
async def mcp_token() -> dict:
    """返回 FLORI_MCP_TOKEN 明文(前端「显示/复制」时取)。未配置则 null。"""
    return {"token": os.environ.get("FLORI_MCP_TOKEN") or None}
