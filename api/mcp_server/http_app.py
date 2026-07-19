"""Flori MCP — HTTP(streamable-http)传输 + Bearer token 认证。

为什么用纯 ASGI 中间件而非 starlette BaseHTTPMiddleware:后者会缓冲响应体,
破坏 streamable-http 的流式 SSE。这里只在 http 请求上校验,lifespan / 其它 scope 直通
(streamable_http_app 的 lifespan 会启动 session manager,必须放行)。

认证语义对齐 api/deps.verify_token 的 fail-closed:
- 设了 FLORI_MCP_TOKEN → 必须 Bearer 精确匹配,否则 401;
- 未设 → 503,除非 FLORI_MCP_ALLOW_NO_AUTH 为真(仅可信内网,放行并告警一次)。

按库作用域 /mcp/{domain}:DomainScopeASGI 在鉴权内层,把 /mcp/{domain}[/...] 改写到
同一 streamable_http_app 的 /mcp[/...],并经 contextvar 锁定该库,使工具无法越库;
/mcp 仍是全局端点。
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from urllib.parse import urlsplit

import structlog

log = structlog.get_logger()

_warned = False


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _valid_port(v: str | None) -> str | None:
    port = (v or "").strip()
    if not port.isdigit():
        return None
    n = int(port)
    if n < 1 or n > 65535:
        return None
    return port


def _mcp_host_ports() -> list[str]:
    """返回 MCP 可能出现在 Host header 中的端口,保序去重。"""
    ports: list[str] = []
    for raw in (
        os.environ.get("FLORI_MCP_PUBLIC_PORT"),
        os.environ.get("MCP_PORT"),
        "8090",
    ):
        port = _valid_port(raw)
        if port and port not in ports:
            ports.append(port)
    return ports


def _normalize_allowed_host(raw: str) -> str:
    host = raw.strip().rstrip("/")
    if "://" not in host:
        return host
    parsed = urlsplit(host)
    return parsed.netloc or parsed.path


def _host_has_port(host: str) -> bool:
    if host.endswith(":*"):
        return True
    if host.startswith("["):
        return "]:" in host
    if host.count(":") > 1:
        return True
    base, sep, port = host.rpartition(":")
    return bool(base and sep and (port.isdigit() or port == "*"))


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _expand_allowed_hosts(raw_hosts: list[str]) -> list[str]:
    """允许配置只写 host,启动时补齐真实 Host header 会携带的端口。"""
    hosts: list[str] = []
    for raw in raw_hosts:
        host = _normalize_allowed_host(raw)
        if not host:
            continue
        _append_unique(hosts, host)
        if _host_has_port(host):
            continue
        for port in _mcp_host_ports():
            _append_unique(hosts, f"{host}:{port}")
    return hosts


def _expand_allowed_origins(hosts: list[str]) -> list[str]:
    origins: list[str] = []
    for host in hosts:
        for scheme in ("http", "https"):
            _append_unique(origins, f"{scheme}://{host}")
    return origins


class RateLimitASGI:
    """纯 ASGI 限流中间件:进程内全局固定时间窗计数器(每分钟 N 次)。

    为什么不缓冲/不用 BaseHTTPMiddleware:与 TokenAuthASGI 同理,要保 streamable-http 的
    流式 SSE 不被破坏。放在最外层(鉴权之前):无谓的请求在做任何鉴权/路由前就被挡掉。

    - 上限来自 env `FLORI_MCP_RATE_LIMIT`(请求/分钟);空或非法值取默认 120;0 = 关闭(永不 429)。
    - 仅对 http scope 计数;lifespan / 其它 scope 直通(放行 session manager 生命周期)。
    - 个人工具粒度:全局单桶(不分 IP/token),固定 60s 窗口 + 计数,窗口翻转即清零。
      threading.Lock 守计数(uvicorn 单事件循环下足够,也容忍多 worker 各自独立计数)。
    - 超限 → 429,小 JSON 体(对齐 TokenAuthASGI._deny 形态)。
    """

    _WINDOW_SEC = 60.0

    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._count = 0

    def _limit(self) -> int:
        raw = (os.environ.get("FLORI_MCP_RATE_LIMIT") or "").strip()
        if not raw:
            return 120
        try:
            return max(0, int(raw))
        except ValueError:
            return 120

    def _allow(self) -> bool:
        """消费一个令牌;返回是否放行。limit<=0 视为关闭(恒放行)。"""
        limit = self._limit()
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._window_start >= self._WINDOW_SEC:
                self._window_start = now
                self._count = 0
            if self._count >= limit:
                return False
            self._count += 1
            return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan / websocket 等直通(不计数,不破坏 session manager 生命周期)
            await self.app(scope, receive, send)
            return
        if not self._allow():
            log.warning("mcp_rate_limited", path=scope.get("path"), limit=self._limit())
            await self._deny(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send) -> None:
        body = b'{"error":"rate_limited"}'
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", b"60"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


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


class DomainScopeASGI:
    """纯 ASGI 中间件:把 /mcp/{domain} 及其子路径映射到单个 streamable-http app(挂 /mcp),
    并经 contextvar 给工具一个作用域 domain,使该端点只能访问对应知识库。

    - 不另起 N 个 server:同一 streamable_http_app(path=/mcp),按请求改写 scope.path + set contextvar。
    - 路径 /mcp 或 /mcp/(无 domain 段)→ 全局无作用域,原样直通。
    - 路径 /mcp/{domain} 或 /mcp/{domain}/... → 抽出 domain,把 path 改写为 "/mcp" + 余下部分,
      在 await 内层前 current_domain.set(domain),finally reset(同一 async task,工具调用可见)。
    - 非 http scope(lifespan 等)直通 —— 放行才不破坏 session manager 生命周期。
    - 纯 ASGI 不缓冲,保流式 SSE。

    放在 TokenAuthASGI 内层:TokenAuthASGI(DomainScopeASGI(streamable_http_app))。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from api.mcp_server.server import current_domain

        path: str = scope.get("path", "") or ""
        domain = self._extract_domain(path)
        if domain is None:
            # /mcp 或 /mcp/ —— 全局端点,无作用域,原样直通
            await self.app(scope, receive, send)
            return

        # /mcp/{domain}[/...] → 改写为 /mcp[/...](streamable_http_path 是 /mcp)
        remainder = path[len("/mcp/") + len(domain):]  # "" 或 "/sub..."
        new_path = "/mcp" + remainder
        scope = dict(scope)  # 不就地改原 scope(避免污染上游)
        scope["path"] = new_path
        scope["raw_path"] = new_path.encode("latin-1")

        token = current_domain.set(domain)
        try:
            log.info("mcp_domain_scope", domain=domain, path=path, rewritten=new_path)
            await self.app(scope, receive, send)
        finally:
            current_domain.reset(token)

    @staticmethod
    def _extract_domain(path: str) -> str | None:
        """从 path 抽出作用域 domain;无作用域(精确 /mcp 或 /mcp/)返回 None。"""
        prefix = "/mcp/"
        if not path.startswith(prefix):
            return None  # 不以 /mcp/ 开头(含精确 /mcp)→ 无作用域
        rest = path[len(prefix):]
        seg = rest.split("/", 1)[0]
        return seg or None  # /mcp/ 时 seg 为空,返回 None


class MaintenanceLeaseASGI:
    """把MCP默认数据库和共享维护租约绑定到ASGI lifespan。"""

    def __init__(self, app, *, database, maintenance_lease) -> None:
        self.app = app
        self.database = database
        self.maintenance_lease = maintenance_lease
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.database.close()
        finally:
            self.maintenance_lease.close()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "lifespan":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def build_http_app():
    """构造带鉴权的 streamable-http ASGI app(默认挂 /mcp)。供 uvicorn 启动。"""
    from mcp.server.transport_security import TransportSecuritySettings

    from api.mcp_server.server import build_default_server

    mcp = build_default_server(stateless_http=True)
    try:
        # DNS-rebinding 保护:其威胁模型是浏览器被诱导直连 localhost MCP。本服务总在
        # 反向代理(Caddy/隧道)+ Bearer token 之后,经代理后 Host=公网域名会被默认保护判为非法。
        # 故按部署主机放行:FLORI_MCP_ALLOWED_HOSTS=逗号分隔。配置可省略端口,这里按公开端口补齐。
        hosts_env = os.environ.get("FLORI_MCP_ALLOWED_HOSTS", "").strip()
        if hosts_env and hosts_env != "*":
            hosts = _expand_allowed_hosts([h.strip() for h in hosts_env.split(",") if h.strip()])
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=hosts,
                allowed_origins=_expand_allowed_origins(hosts),
            )
        else:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )

        app = mcp.streamable_http_app()  # Starlette ASGI;path 默认 /mcp
        # 限流在最外层,挡掉的请求不耗鉴权;鉴权次之;
        # 作用域中间件最内,把 /mcp/{domain} 改写到 /mcp 并 set contextvar。
        protected = RateLimitASGI(TokenAuthASGI(DomainScopeASGI(app)))
        return MaintenanceLeaseASGI(
            protected,
            database=mcp._flori_database,
            maintenance_lease=mcp._flori_maintenance_lease,
        )
    except BaseException:
        try:
            mcp._flori_database.close()
        finally:
            mcp._flori_maintenance_lease.close()
        raise
