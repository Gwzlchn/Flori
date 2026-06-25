"""Flori MCP — HTTP(streamable-http)传输 + Bearer token 认证。

为什么用纯 ASGI 中间件而非 starlette BaseHTTPMiddleware:后者会缓冲响应体,
破坏 streamable-http 的流式 SSE。这里只在 http 请求上校验,**lifespan / 其它 scope 直通**
(streamable_http_app 的 lifespan 会启动 session manager,必须放行)。

认证语义对齐 api/deps.verify_token 的 fail-closed:
- 设了 FLORI_MCP_TOKEN → 必须 Bearer 精确匹配,否则 401;
- 未设 → 503,除非 FLORI_MCP_ALLOW_NO_AUTH 为真(仅可信内网,放行并告警一次)。
"""

from __future__ import annotations

import hmac
import os

import structlog

log = structlog.get_logger()

_warned = False


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


class TokenAuthASGI:
    """纯 ASGI Bearer token 鉴权中间件(不缓冲,不破坏流式)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan / websocket 等直通 —— 关键:放行 lifespan 才能启动 session manager
            await self.app(scope, receive, send)
            return

        token = os.environ.get("FLORI_MCP_TOKEN", "")
        if not token:
            if not _truthy(os.environ.get("FLORI_MCP_ALLOW_NO_AUTH")):
                await self._deny(
                    send, 503,
                    "MCP auth not configured: set FLORI_MCP_TOKEN, or FLORI_MCP_ALLOW_NO_AUTH=1 on a trusted network",
                )
                return
            global _warned
            if not _warned:
                _warned = True
                log.warning(
                    "mcp_token_empty",
                    msg="FLORI_MCP_TOKEN 未设且 FLORI_MCP_ALLOW_NO_AUTH=1:MCP 鉴权已关闭(仅限可信内网)",
                )
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {token}"
        if not (auth and hmac.compare_digest(auth.encode(), expected.encode())):
            log.warning("mcp_auth_reject", path=scope.get("path"))
            await self._deny(send, 401, "unauthorized")
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, status: int, msg: str) -> None:
        body = msg.encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_http_app():
    """构造带鉴权的 streamable-http ASGI app(默认挂 /mcp)。供 uvicorn 启动。"""
    from mcp.server.transport_security import TransportSecuritySettings

    from api.mcp_server.server import build_default_server

    mcp = build_default_server(stateless_http=True)

    # DNS-rebinding 保护:其威胁模型是「浏览器被诱导直连 localhost MCP」。本服务总在
    # 反向代理(Caddy/隧道)+ Bearer token 之后,经代理后 Host=公网域名会被默认保护判为非法 → 421。
    # 故按部署主机放行:FLORI_MCP_ALLOWED_HOSTS=逗号分隔 → 保护开但允许这些 host;"*"/未设 → 关保护。
    hosts_env = os.environ.get("FLORI_MCP_ALLOWED_HOSTS", "").strip()
    if hosts_env and hosts_env != "*":
        hosts = [h.strip() for h in hosts_env.split(",") if h.strip()]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=hosts
        )
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

    app = mcp.streamable_http_app()  # Starlette ASGI;path 默认 /mcp
    return TokenAuthASGI(app)
