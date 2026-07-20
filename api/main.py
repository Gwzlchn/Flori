"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI


class _RunnerPollAccessFilter(logging.Filter):
    """丢弃 runner 高频轮询端点(heartbeat / jobs/request)的 uvicorn access 记录。
    这些请求行高频低信号,会刷爆 api 容器日志、把真正重要的日志淹没(Dozzle 里看不到);
    worker 连接/认证状态由结构化事件呈现(worker_registered/auth_rejected/throttled → Dozzle + /system 事件页)。
    不影响其余端点的 access,也不影响任何 structlog 业务/审计日志。"""

    _NOISY = ("/api/runner/heartbeat", "/api/runner/jobs/request")

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access 记录 args=(client_addr, method, full_path, http_version, status);path 在 [2]。
        args = record.args
        path = args[2] if isinstance(args, tuple) and len(args) >= 3 else None
        if isinstance(path, str) and any(path.startswith(p) for p in self._NOISY):
            return False
        return True

from shared.config import load_config
from shared.content_maintenance import acquire_service_lease
from shared.db import Database
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.storage import create_storage
from shared.exact_dr_maintenance import read_barrier
from api.exact_dr import ExactDrError, ExactDrMutationGate
from api.pricing_store import PricingStore
from api.minio_capacity_store import MinioCapacityStore

_UPLOAD_FINALIZER_DRAIN_TIMEOUT_SEC = 15.0


async def _subscription_sync_loop(app: FastAPI) -> None:
    """周期同步所有启用自动追更的订阅集合。失败只记日志,不影响 API。"""
    import asyncio
    import structlog
    log = structlog.get_logger(component="subscription-sync")
    hours = float(os.environ.get("SUBSCRIPTION_SYNC_HOURS", "6"))
    if hours <= 0:
        return
    from api.routes.collections import sync_collection
    await asyncio.sleep(120)  # 启动后等服务稳定再首扫
    while True:
        try:
            colls = await asyncio.to_thread(app.state.db.list_subscription_collections, True)
            for coll in colls:
                try:
                    await sync_collection(coll, app.state.db, app.state.redis, app.state.storage)
                except Exception as e:
                    log.warning("sync_failed", coll=coll.id, error=str(e)[:200])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sync_loop_error")
        await asyncio.sleep(hours * 3600)


async def _initialization_recovery_loop(app: FastAPI) -> None:
    """启动即恢复中断上传,之后周期清理 marker 与全局 staging。"""
    import structlog

    from api.routes.jobs import reconcile_incomplete_job_uploads

    log = structlog.get_logger(component="upload-recovery")
    interval = 3600
    while True:
        try:
            await reconcile_incomplete_job_uploads(
                app.state.db,
                app.state.redis,
                app.state.storage,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("upload_recovery_loop_error")
        await asyncio.sleep(interval)


def create_app(
    db: Database | None = None,
    redis: RedisClient | None = None,
    config=None,
) -> FastAPI:
    setup_logging()  # 与 scheduler/worker 一致输出结构化 JSON 日志
    # runner 轮询端点(heartbeat/jobs/request)的 access 记录从 uvicorn.access 摘掉 → dozzle 主流不被刷屏。
    logging.getLogger("uvicorn.access").addFilter(_RunnerPollAccessFilter())
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # api 进程启动时刻(供 /api/status 的 api 组件算 uptime_sec)。两条资源路径都记。
        app.state.started_at = datetime.now(timezone.utc)
        maintenance_lease = None
        if not hasattr(app.state, "db") or app.state.db is None:
            cfg = load_config(
                config_dir=os.environ.get("CONFIG_DIR", "/data/configs"),
                data_dir=os.environ.get("DATA_DIR", "/data"),
            )
            maintenance_lease = acquire_service_lease(
                db_path=cfg.db_path,
                jobs_dir=cfg.jobs_dir,
                object_bucket=os.environ.get("MINIO_BUCKET"),
                config_root=cfg.prompts_dir,
                owner="api",
            )
            try:
                app.state.config = cfg
                app.state.db = Database(cfg.db_path)
                app.state.db.init_schema()
                app.state.redis = RedisClient(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
                await app.state.redis.connect()
                app.state.storage = create_storage(cfg.jobs_dir)
                app.state._own_resources = True
            except BaseException:
                maintenance_lease.close()
                raise
        else:
            app.state._own_resources = False
            cfg = getattr(app.state, "config", None)
            if cfg is not None:
                maintenance_lease = acquire_service_lease(
                    db_path=cfg.db_path,
                    jobs_dir=cfg.jobs_dir,
                    object_bucket=os.environ.get("MINIO_BUCKET"),
                    config_root=cfg.prompts_dir,
                    owner="api",
                )

        # 周期自动同步订阅(默认每 6h;SUBSCRIPTION_SYNC_HOURS=0 关闭)。
        mutable_background_tasks: dict[str, asyncio.Task] = {}
        capacity_task = None
        background_lock = asyncio.Lock()

        async def pause_exact_dr_background_writers() -> None:
            async with background_lock:
                tasks = list(mutable_background_tasks.values())
                mutable_background_tasks.clear()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        async def resume_exact_dr_background_writers() -> None:
            if not getattr(app.state, "_own_resources", False):
                return
            async with background_lock:
                if mutable_background_tasks:
                    return
                mutable_background_tasks.update({
                    "upload_recovery": asyncio.create_task(
                        _initialization_recovery_loop(app)
                    ),
                    "subscription_sync": asyncio.create_task(_subscription_sync_loop(app)),
                    "pricing": asyncio.create_task(
                        app.state.pricing.daily_loop(app.state.storage)
                    ),
                })

        app.state.pause_exact_dr_background_writers = pause_exact_dr_background_writers
        app.state.resume_exact_dr_background_writers = resume_exact_dr_background_writers
        # API 若在 Redis CLIENT PAUSE WRITE 期间退出,重启必须先解除本代暂停再清理旧代。
        if await asyncio.to_thread(read_barrier, cfg.data_dir) is not None:
            await app.state.redis.resume_writes_after_exact_dr()
        await app.state.exact_dr_gate.recover_stale(Path(cfg.data_dir))
        if getattr(app.state, "_own_resources", False):
            await resume_exact_dr_background_writers()
            # MinIO 容量:后台每 10min 全量扫一次,内存缓存供 /api/status 读(绝不同步阻塞)。
            if app.state.storage is not None:
                capacity_task = asyncio.create_task(
                    app.state.minio_cap.loop(app.state.storage)
                )

        try:
            yield
        finally:
            try:
                exact_task = getattr(app.state, "exact_dr_task", None)
                if exact_task is not None and not exact_task.done():
                    exact_task.cancel()
                    await asyncio.gather(exact_task, return_exceptions=True)
                recovery_tasks = list(
                    getattr(app.state, "recovery_tasks", {}).values()
                )
                for task in recovery_tasks:
                    task.cancel()
                if recovery_tasks:
                    await asyncio.gather(*recovery_tasks, return_exceptions=True)
                await pause_exact_dr_background_writers()
                wait_for_finalizers = getattr(
                    getattr(app.state, "storage", None), "wait_for_finalizers", None,
                )
                if callable(wait_for_finalizers):
                    try:
                        await asyncio.wait_for(
                            wait_for_finalizers(),
                            timeout=_UPLOAD_FINALIZER_DRAIN_TIMEOUT_SEC,
                        )
                    except TimeoutError:
                        import structlog
                        structlog.get_logger(component="upload-recovery").error(
                            "upload_finalizer_drain_timeout",
                            timeout_sec=_UPLOAD_FINALIZER_DRAIN_TIMEOUT_SEC,
                            recovery="initialization_marker_reconciler",
                        )
                    except Exception:
                        import structlog
                        structlog.get_logger(component="upload-recovery").exception(
                            "upload_finalizer_drain_failed",
                        )
                if capacity_task:
                    capacity_task.cancel()
                if getattr(app.state, "_own_resources", False):
                    try:
                        await app.state.redis.close()
                    finally:
                        app.state.db.close()
            finally:
                if maintenance_lease is not None:
                    maintenance_lease.close()

    app = FastAPI(title="AI Knowledge Base", lifespan=lifespan)

    # 错误体统一 {error, message},见 docs/03-contracts.md §5。error 用状态码派生机器码。
    from fastapi import Request as _Request
    from fastapi.exceptions import RequestValidationError as _RequestValidationError
    from fastapi.responses import JSONResponse as _JSONResponse
    from starlette.exceptions import HTTPException as _StarletteHTTPException

    _STATUS_ERROR_CODE = {
        400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
        409: "conflict", 413: "payload_too_large", 416: "range_not_satisfiable",
        422: "invalid_request", 429: "rate_limited", 502: "bad_gateway",
        503: "unavailable",
    }

    @app.exception_handler(_StarletteHTTPException)
    async def _http_exc_handler(request: _Request, exc: _StarletteHTTPException):
        from api.business_admission import BusinessAdmissionError

        error_code = (
            exc.error_code if isinstance(exc, BusinessAdmissionError)
            else _STATUS_ERROR_CODE.get(exc.status_code, "error")
        )
        return _JSONResponse(
            status_code=exc.status_code,
            content={"error": error_code,
                     "message": exc.detail},
            headers=exc.headers,   # 透传 HTTPException 的头(如 429 的 Retry-After);多数为 None 无影响
        )

    @app.exception_handler(_RequestValidationError)
    async def _validation_exc_handler(request: _Request, exc: _RequestValidationError):
        return _JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "message": str(exc.errors())},
        )

    # 兜底:URL 路径或查询串含空字节(null byte)会让 sqlite3 绑定 / pathlib.resolve() 抛异常 → 裸 500;
    # 这类输入恒为非法,入口统一拦成 400(schemathesis fuzz 发现 /assets/x%00、/search?q=%00 两例)。
    from urllib.parse import unquote as _unquote

    @app.middleware("http")
    async def _exact_dr_write_barrier(request: _Request, call_next):
        entered = False
        try:
            entered = await app.state.exact_dr_gate.enter_request(
                request.method,
                request.url.path,
            )
        except ExactDrError as exc:
            return _JSONResponse(
                status_code=503,
                content={"error": "exact_dr_maintenance", "message": str(exc)},
                headers={"Retry-After": "5"},
            )
        try:
            return await call_next(request)
        finally:
            await app.state.exact_dr_gate.leave_request(entered)

    @app.middleware("http")
    async def _reject_null_bytes(request: _Request, call_next):
        if "\x00" in _unquote(request.url.path) or "\x00" in _unquote(request.url.query):
            return _JSONResponse(
                status_code=400,
                content={"error": "bad_request", "message": "null byte in request URL"},
            )
        return await call_next(request)

    if db is not None:
        app.state.db = db
        app.state.redis = redis
        app.state.config = config
        app.state.storage = create_storage(config.jobs_dir) if config is not None else None

    # LiteLLM 价表缓存(无条件置,供 runner 算价);daily_loop 仅生产在 lifespan 起,测试/注入态空表→回退。
    app.state.pricing = PricingStore()
    # MinIO 容量缓存(无条件置,build_full_status 读;loop 仅生产在 lifespan 起,测试/注入态空快照→不填)。
    app.state.minio_cap = MinioCapacityStore()
    app.state.recovery_tasks = {}
    app.state.exact_dr_task = None
    app.state.exact_dr_gate = ExactDrMutationGate()

    from api.routes import (
        jobs, notes, workers, ws, auth, admin, profiles, runner, bili,
        collections, search, glossary, domains, mcp, ask, radar, queue,
        ai_tasks, prompts, study, sources, evidence, recovery,
    )
    app.include_router(jobs.router)
    app.include_router(jobs.providers_router)
    app.include_router(domains.router)
    app.include_router(notes.router)
    app.include_router(workers.router)
    app.include_router(ws.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(profiles.router)
    app.include_router(runner.router)
    app.include_router(bili.router)
    app.include_router(collections.router)
    app.include_router(search.router)
    app.include_router(glossary.router)
    app.include_router(mcp.router)
    app.include_router(ask.router)
    app.include_router(radar.router)
    app.include_router(ai_tasks.router)
    app.include_router(queue.router)
    app.include_router(prompts.router)
    app.include_router(study.router)
    app.include_router(sources.router)
    app.include_router(evidence.router)
    app.include_router(recovery.router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    # 生产默认关 reload(避免 StatReload 常驻 stat 源码树);开发用 API_RELOAD=1 开启。
    reload = os.environ.get("API_RELOAD", "0") == "1"
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=reload)
