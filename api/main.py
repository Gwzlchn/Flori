"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.config import load_config
from shared.db import Database
from shared.redis_client import RedisClient
from shared.storage import create_storage


def create_app(
    db: Database | None = None,
    redis: RedisClient | None = None,
    config=None,
) -> FastAPI:
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

        yield

        if getattr(app.state, "_own_resources", False):
            await app.state.redis.close()
            app.state.db.close()

    app = FastAPI(title="AI Knowledge Base", lifespan=lifespan)

    if db is not None:
        app.state.db = db
        app.state.redis = redis
        app.state.config = config
        app.state.storage = create_storage(config.jobs_dir) if config is not None else None

    from api.routes import jobs, notes, workers, ws, auth, admin, profiles, runner
    app.include_router(jobs.router)
    app.include_router(notes.router)
    app.include_router(workers.router)
    app.include_router(ws.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(profiles.router)
    app.include_router(runner.router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
