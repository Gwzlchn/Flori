"""调度器入口。"""

from __future__ import annotations

import asyncio
import os
import signal

import structlog

from shared.config import load_config
from shared.content_maintenance import acquire_service_lease
from shared.db import Database
from shared.redis_client import RedisClient
from shared.storage import create_storage

from .scheduler import Scheduler

logger = structlog.get_logger(component="scheduler")


async def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    data_dir = os.environ.get("DATA_DIR", "/data")
    config_dir = os.environ.get("CONFIG_DIR", "/data/configs")

    config = load_config(config_dir=config_dir, data_dir=data_dir)
    maintenance_lease = acquire_service_lease(
        db_path=config.db_path,
        jobs_dir=config.jobs_dir,
        object_bucket=os.environ.get("MINIO_BUCKET"),
        config_root=config.prompts_dir,
        owner="scheduler",
    )

    try:
        redis = RedisClient(redis_url)
        await redis.connect()
        await redis.ping()
        logger.info("redis_connected", url=redis_url)

        db = Database(config.db_path)
        db.init_schema()
        logger.info("db_ready", path=str(config.db_path))

        # storage 供 on_step_done 读笔记/评审产物(本地或 MinIO,由 env 决定)。
        storage = create_storage(config.jobs_dir)

        scheduler = Scheduler(redis, db, config, storage=storage)
    except BaseException:
        maintenance_lease.close()
        raise

    loop = asyncio.get_running_loop()
    shutdown_task: asyncio.Task | None = None

    def _on_signal() -> None:
        # add_signal_handler 只接受同步回调,故用 create_task 包裹 async shutdown。
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(scheduler.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    try:
        await scheduler.run()
    finally:
        try:
            if shutdown_task is not None:
                await shutdown_task  # 等 graceful shutdown(取消延迟任务)完成
        finally:
            try:
                db.close()
            finally:
                try:
                    await redis.close()
                finally:
                    maintenance_lease.close()
                    logger.info("scheduler_exit")


if __name__ == "__main__":
    from shared.logging_setup import setup_logging
    setup_logging()
    asyncio.run(main())
