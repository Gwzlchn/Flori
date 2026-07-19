"""tests for api/mcp_server/http_app.py —— Bearer token 鉴权中间件(纯 ASGI)。

只验鉴权逻辑(MCP 协议本身见 test_mcp.py):用 dummy 内层 app 包 TokenAuthASGI,
经 httpx ASGITransport 驱动,断 503/401/放行;另验 lifespan 直通 + build_http_app 可构造。
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from api.mcp_server.http_app import DomainScopeASGI, RateLimitASGI, TokenAuthASGI

_LOCAL_MCP_HOST = "127.0.0.1"
_PUBLIC_MCP_PORT = "18090"


async def _dummy(scope, receive, send):
    """内层 app:走到这里即鉴权已放行。"""
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def _post(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/mcp", headers=headers or {})


@pytest.mark.asyncio
async def test_no_token_no_allow_503(monkeypatch):
    monkeypatch.delenv("FLORI_MCP_TOKEN", raising=False)
    monkeypatch.delenv("FLORI_MCP_ALLOW_NO_AUTH", raising=False)
    r = await _post(TokenAuthASGI(_dummy))
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_token_set_missing_or_wrong_bearer_401(monkeypatch):
    monkeypatch.setenv("FLORI_MCP_TOKEN", "secret123")
    monkeypatch.delenv("FLORI_MCP_ALLOW_NO_AUTH", raising=False)
    assert (await _post(TokenAuthASGI(_dummy))).status_code == 401
    assert (await _post(TokenAuthASGI(_dummy), {"Authorization": "Bearer wrong"})).status_code == 401


@pytest.mark.asyncio
async def test_token_set_correct_bearer_passes(monkeypatch):
    monkeypatch.setenv("FLORI_MCP_TOKEN", "secret123")
    r = await _post(TokenAuthASGI(_dummy), {"Authorization": "Bearer secret123"})
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.asyncio
async def test_allow_no_auth_passes(monkeypatch):
    monkeypatch.delenv("FLORI_MCP_TOKEN", raising=False)
    monkeypatch.setenv("FLORI_MCP_ALLOW_NO_AUTH", "1")
    assert (await _post(TokenAuthASGI(_dummy))).status_code == 200


@pytest.mark.asyncio
async def test_lifespan_passthrough():
    """非 http scope(lifespan)必须直通内层 —— 否则 streamable-http 的 session manager 起不来。"""
    seen = {}

    async def inner(scope, receive, send):
        seen["type"] = scope["type"]

    await TokenAuthASGI(inner)({"type": "lifespan"}, None, None)
    assert seen["type"] == "lifespan"


def test_build_http_app_smoke(monkeypatch, tmp_path):
    """build_http_app 能构造(create_storage 本地后端 + streamable_http_app)。"""
    monkeypatch.setenv("CONFIG_DIR", "/app/configs")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MINIO_URL", raising=False)
    monkeypatch.setenv("FLORI_MCP_ALLOW_NO_AUTH", "1")
    from api.mcp_server.http_app import build_http_app

    app = build_http_app()
    assert callable(app)
    app.close()


def test_mcp_default_server_blocks_import_until_lifespan_resources_close(
    monkeypatch, tmp_path,
):
    from shared import content_maintenance as maintenance
    from api.mcp_server.http_app import build_http_app

    monkeypatch.setenv("CONFIG_DIR", "/app/configs")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(maintenance.LOCK_DIR_ENV, str(tmp_path / "locks"))
    monkeypatch.delenv("MINIO_URL", raising=False)
    monkeypatch.setenv("FLORI_MCP_ALLOW_NO_AUTH", "1")
    app = build_http_app()
    resources = app.maintenance_lease.resources
    with pytest.raises(maintenance.MaintenanceLockError, match="blocked"):
        maintenance.acquire_maintenance_lease(
            resources, exclusive=True, owner="content-import",
        )
    app.close()
    exclusive = maintenance.acquire_maintenance_lease(
        resources, exclusive=True, owner="content-import",
    )
    exclusive.close()


@pytest.mark.asyncio
async def test_mcp_holder_blocks_content_import_and_release_allows_it(
    monkeypatch, tmp_path,
):
    from shared import content_import
    from shared import content_maintenance as maintenance
    from api.mcp_server.http_app import build_http_app

    monkeypatch.setenv("CONFIG_DIR", "/app/configs")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(maintenance.LOCK_DIR_ENV, str(tmp_path / "locks"))
    monkeypatch.delenv("MINIO_URL", raising=False)
    monkeypatch.setenv("FLORI_MCP_ALLOW_NO_AUTH", "1")
    app = build_http_app()

    class Storage:
        jobs_dir = tmp_path / "jobs"

    Storage.jobs_dir.mkdir(exist_ok=True)

    async def imported(**_kwargs):
        return "imported"

    monkeypatch.setattr(content_import, "_run_import_locked", imported)
    kwargs = {
        "repository": object(),
        "snapshot": "latest",
        "target_db_path": app.database._path,
        "storage": Storage(),
        "journal_path": tmp_path / "import-journal.sqlite3",
        "target_generation": "gen-mcp-lock",
    }
    with pytest.raises(maintenance.MaintenanceLockError, match="blocked"):
        await content_import.run_import(**kwargs)
    app.close()
    assert await content_import.run_import(**kwargs) == "imported"


def test_mcp_initialization_failure_releases_maintenance_lease(
    monkeypatch, tmp_path,
):
    from api.mcp_server import server

    monkeypatch.setenv("CONFIG_DIR", "/app/configs")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    closed = False
    database_closed = False

    class Lease:
        def close(self):
            nonlocal closed
            closed = True

    class BrokenDatabase:
        def __init__(self, _path):
            pass

        def init_schema(self):
            raise RuntimeError("database init failed")

        def close(self):
            nonlocal database_closed
            database_closed = True

    monkeypatch.setattr(server, "acquire_service_lease", lambda **_kwargs: Lease())
    monkeypatch.setattr(server, "Database", BrokenDatabase)
    with pytest.raises(RuntimeError, match="database init failed"):
        server.build_default_server(stateless_http=True)
    assert closed
    assert database_closed


def test_mcp_http_app_construction_failure_releases_database_and_lease(
    monkeypatch, tmp_path,
):
    from mcp.server.fastmcp import FastMCP
    from shared import content_maintenance as maintenance
    from api.mcp_server import server
    from api.mcp_server.http_app import build_http_app

    monkeypatch.setenv("CONFIG_DIR", "/app/configs")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(maintenance.LOCK_DIR_ENV, str(tmp_path / "locks"))
    monkeypatch.delenv("MINIO_URL", raising=False)
    leases = []
    real_acquire = server.acquire_service_lease

    def capture_lease(**kwargs):
        lease = real_acquire(**kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(server, "acquire_service_lease", capture_lease)
    monkeypatch.setattr(
        FastMCP, "streamable_http_app",
        lambda _self: (_ for _ in ()).throw(RuntimeError("ASGI init failed")),
    )
    with pytest.raises(RuntimeError, match="ASGI init failed"):
        build_http_app()
    assert len(leases) == 1
    assert leases[0]._descriptors == []
    assert leases[0]._directory_fd is None
    # 构造失败不能遗留共享锁；同进程随后可取得同资源的独占维护租约。
    exclusive = maintenance.acquire_maintenance_lease(
        leases[0].resources, exclusive=True, owner="content-import",
    )
    exclusive.close()


# 限流 RateLimitASGI(纯 ASGI 时间窗计数器,最外层)


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_under_limit_passes(self, monkeypatch):
        """上限内的请求全部放行(200)。"""
        monkeypatch.setenv("FLORI_MCP_RATE_LIMIT", "3")
        app = RateLimitASGI(_dummy)  # 每实例独立窗口/计数,确定性
        for _ in range(3):
            r = await _post(app)
            assert r.status_code == 200
            assert r.text == "ok"

    @pytest.mark.asyncio
    async def test_over_limit_429(self, monkeypatch):
        """超过上限 → 429,小 JSON 体。"""
        monkeypatch.setenv("FLORI_MCP_RATE_LIMIT", "2")
        app = RateLimitASGI(_dummy)
        assert (await _post(app)).status_code == 200
        assert (await _post(app)).status_code == 200
        r = await _post(app)
        assert r.status_code == 429
        assert "rate_limited" in r.text

    @pytest.mark.asyncio
    async def test_disabled_never_429(self, monkeypatch):
        """FLORI_MCP_RATE_LIMIT=0 → 关闭,任意次数都不 429。"""
        monkeypatch.setenv("FLORI_MCP_RATE_LIMIT", "0")
        app = RateLimitASGI(_dummy)
        for _ in range(50):
            assert (await _post(app)).status_code == 200

    @pytest.mark.asyncio
    async def test_empty_disabled_default(self, monkeypatch):
        """空值 → 用默认上限 120(此处只验远低于默认的请求不被挡)。"""
        monkeypatch.delenv("FLORI_MCP_RATE_LIMIT", raising=False)
        app = RateLimitASGI(_dummy)
        for _ in range(10):
            assert (await _post(app)).status_code == 200

    @pytest.mark.asyncio
    async def test_lifespan_passthrough_not_counted(self, monkeypatch):
        """非 http scope 直通且不计数:即便上限=1,lifespan 不消耗令牌。"""
        monkeypatch.setenv("FLORI_MCP_RATE_LIMIT", "1")
        seen = {"lifespan": 0}

        async def inner(scope, receive, send):
            # 直通安全:lifespan 仅记次数;http 回 200 ok。
            if scope["type"] != "http":
                seen["lifespan"] += 1
                return
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})

        # 同实例:上限=1,先吃 5 个 lifespan(若被计数则上限早耗尽),首个 http 仍 200。
        app = RateLimitASGI(inner)
        for _ in range(5):
            await app({"type": "lifespan"}, None, None)
        assert seen["lifespan"] == 5  # 全部直通内层
        assert (await _post(app)).status_code == 200  # lifespan 未耗令牌 → http 放行
        assert (await _post(app)).status_code == 429  # 第二个 http 超上限=1


# 按库作用域 /mcp/{domain}:DomainScopeASGI 路径改写 + contextvar 设置


class TestDomainScopeMiddleware:
    @pytest.mark.asyncio
    async def test_scoped_path_rewritten_and_contextvar_set(self):
        """/mcp/finance/sub → 内层看到 path=/mcp/sub,且 contextvar 在 await 内层时可见。"""
        from api.mcp_server.server import current_domain

        seen = {}

        async def inner(scope, receive, send):
            seen["path"] = scope["path"]
            seen["raw_path"] = scope.get("raw_path")
            seen["domain"] = current_domain.get(None)  # 设在 await 前 → 同 task 可见
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = DomainScopeASGI(inner)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/mcp/finance/sub")
        assert r.status_code == 200
        assert seen["path"] == "/mcp/sub"
        assert seen["raw_path"] == b"/mcp/sub"
        assert seen["domain"] == "finance"
        # 请求后 contextvar 已 reset(finally)
        assert current_domain.get(None) is None

    @pytest.mark.asyncio
    async def test_scoped_root_rewritten(self):
        """/mcp/finance(无子路径)→ 内层看到 path=/mcp,domain=finance。"""
        from api.mcp_server.server import current_domain

        seen = {}

        async def inner(scope, receive, send):
            seen["path"] = scope["path"]
            seen["domain"] = current_domain.get(None)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        transport = httpx.ASGITransport(app=DomainScopeASGI(inner))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/mcp/finance")
        assert seen["path"] == "/mcp"
        assert seen["domain"] == "finance"

    @pytest.mark.asyncio
    async def test_bare_mcp_no_scope(self):
        """精确 /mcp → 不作用域,path 原样,domain 为 None。"""
        from api.mcp_server.server import current_domain

        seen = {}

        async def inner(scope, receive, send):
            seen["path"] = scope["path"]
            seen["domain"] = current_domain.get(None)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        transport = httpx.ASGITransport(app=DomainScopeASGI(inner))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/mcp")
        assert seen["path"] == "/mcp"
        assert seen["domain"] is None

    @pytest.mark.asyncio
    async def test_lifespan_passthrough(self):
        """非 http scope 直通(不破坏 session manager lifespan)。"""
        seen = {}

        async def inner(scope, receive, send):
            seen["type"] = scope["type"]

        await DomainScopeASGI(inner)({"type": "lifespan"}, None, None)
        assert seen["type"] == "lifespan"


@contextlib.asynccontextmanager
async def _run_lifespan(app):
    """手动驱动 ASGI lifespan,使 streamable-http 的 session manager task group 初始化
    (httpx ASGITransport 默认不跑 lifespan)。退出时优雅 shutdown。"""
    recv_q: asyncio.Queue = asyncio.Queue()
    send_events: list[dict] = []

    async def receive():
        return await recv_q.get()

    async def send(msg):
        send_events.append(msg)

    await recv_q.put({"type": "lifespan.startup"})
    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    # 等 startup 完成
    for _ in range(200):
        if any(e["type"].startswith("lifespan.startup") for e in send_events):
            break
        await asyncio.sleep(0.005)
    try:
        yield app
    finally:
        await recv_q.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=2)


class TestScopedEndpointRouteAccepted:
    """端到端:经 build_http_app() 的真实 streamable_http_app,先跑 lifespan 初始化 session
    manager。验作用域路由被接受(非 404),且改写后 bare /mcp 仍可用;深层 MCP 协议见 test_mcp.py。"""

    def _app(self, monkeypatch, tmp_path, allowed_hosts: str | None = None):
        monkeypatch.setenv("CONFIG_DIR", "/app/configs")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("MINIO_URL", raising=False)
        monkeypatch.delenv("FLORI_MCP_TOKEN", raising=False)
        monkeypatch.setenv("FLORI_MCP_ALLOW_NO_AUTH", "1")  # 鉴权放行
        if allowed_hosts is None:
            monkeypatch.delenv("FLORI_MCP_ALLOWED_HOSTS", raising=False)
        else:
            monkeypatch.setenv("FLORI_MCP_ALLOWED_HOSTS", allowed_hosts)
        from api.mcp_server.http_app import build_http_app

        return build_http_app()

    _INIT = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    _HEADERS = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json"}

    @pytest.mark.asyncio
    async def test_scoped_initialize_not_404(self, monkeypatch, tmp_path):
        app = self._app(monkeypatch, tmp_path)
        async with _run_lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/mcp/finance", json=self._INIT, headers=self._HEADERS)
        # 路由被接受(路径已改写到 /mcp);非 404/405。initialize 成功通常 200。
        assert r.status_code not in (404, 405), r.text

    @pytest.mark.asyncio
    async def test_bare_initialize_still_works(self, monkeypatch, tmp_path):
        app = self._app(monkeypatch, tmp_path)
        async with _run_lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post("/mcp", json=self._INIT, headers=self._HEADERS)
        assert r.status_code not in (404, 405), r.text

    @pytest.mark.asyncio
    async def test_allowed_hosts_accept_local_url_host_with_port(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MCP_PORT", "8090")
        monkeypatch.setenv("FLORI_MCP_PUBLIC_PORT", _PUBLIC_MCP_PORT)
        app = self._app(monkeypatch, tmp_path, allowed_hosts="localhost,127.0.0.1")
        headers = {
            **self._HEADERS,
            "Origin": f"http://{_LOCAL_MCP_HOST}:{_PUBLIC_MCP_PORT}",
        }
        async with _run_lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=f"http://{_LOCAL_MCP_HOST}:{_PUBLIC_MCP_PORT}",
            ) as c:
                r = await c.post("/mcp", json=self._INIT, headers=headers)
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_allowed_hosts_reject_unknown_host_with_port(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MCP_PORT", "8090")
        monkeypatch.setenv("FLORI_MCP_PUBLIC_PORT", _PUBLIC_MCP_PORT)
        app = self._app(monkeypatch, tmp_path, allowed_hosts="localhost,127.0.0.1")
        async with _run_lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=f"http://evil.test:{_PUBLIC_MCP_PORT}") as c:
                r = await c.post("/mcp", json=self._INIT, headers=self._HEADERS)
        assert r.status_code == 421
