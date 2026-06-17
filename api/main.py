"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.config import load_config
from shared.db import Database
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.storage import create_storage


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


def create_app(
    db: Database | None = None,
    redis: RedisClient | None = None,
    config=None,
) -> FastAPI:
    setup_logging()  # 与 scheduler/worker 一致输出结构化 JSON 日志
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not hasattr(app.state, "db") or app.state.db is None:
            cfg = load_config(
                config_dir=os.environ.get("CONFIG_DIR", "/data/configs"),
                data_dir=os.environ.get("DATA_DIR", "/data"),
            )
            app.state.config = cfg
            app.state.db = Database(cfg.db_path)
            app.state.db.init_schema()
            app.state.redis = RedisClient(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
            await app.state.redis.connect()
            app.state.storage = create_storage(cfg.jobs_dir)
            app.state._own_resources = True
        else:
            app.state._own_resources = False

        # 周期自动同步订阅(默认每 6h;SUBSCRIPTION_SYNC_HOURS=0 关闭)。
        sync_task = None
        if getattr(app.state, "_own_resources", False):
            import asyncio
            sync_task = asyncio.create_task(_subscription_sync_loop(app))

        yield

        if sync_task:
            sync_task.cancel()
        if getattr(app.state, "_own_resources", False):
            await app.state.redis.close()
            app.state.db.close()

    app = FastAPI(title="AI Knowledge Base", lifespan=lifespan)

    if db is not None:
        app.state.db = db
        app.state.redis = redis
        app.state.config = config
        app.state.storage = create_storage(config.jobs_dir) if config is not None else None

    from api.routes import (
        jobs, notes, workers, ws, auth, admin, profiles, runner, bili,
        collections, search, glossary, domains,
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

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    # 生产默认关 reload(避免 StatReload 常驻 stat 源码树);开发用 API_RELOAD=1 开启。
    reload = os.environ.get("API_RELOAD", "0") == "1"
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=reload)
