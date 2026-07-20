"""调度器入口。"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timezone

import structlog

from shared.config import load_config
from shared.content_maintenance import acquire_service_lease
from shared.db import Database
from shared.redis_client import RedisClient
from shared.storage import create_storage
from shared.exact_dr_maintenance import (
    PHASE_SNAPSHOTTING,
    barrier_phase,
    read_barrier,
    write_scheduler_quiesced,
)

from .scheduler import Scheduler

logger = structlog.get_logger(component="scheduler")


async def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    data_dir = os.environ.get("DATA_DIR", "/data")
    config_dir = os.environ.get("CONFIG_DIR", "/data/configs")

    config = load_config(config_dir=config_dir, data_dir=data_dir)
    while barrier_phase(config.data_dir) == PHASE_SNAPSHOTTING:
        logger.warning("exact_dr_start_wait", phase=PHASE_SNAPSHOTTING)
        await asyncio.sleep(1)
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
    quiesced_operation: str | None = None

    def _on_signal() -> None:
        # add_signal_handler 只接受同步回调,故用 create_task 包裹 async shutdown。
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(scheduler.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    async def _watch_exact_dr() -> None:
        nonlocal quiesced_operation
        while not scheduler._shutdown:
            barrier = await asyncio.to_thread(read_barrier, config.data_dir)
            if barrier and barrier["phase"] == PHASE_SNAPSHOTTING:
                quiesced_operation = str(barrier["operation_id"])
                await scheduler.shutdown(drain=True)
                return
            await asyncio.sleep(0.5)

    exact_dr_watch_task = asyncio.create_task(_watch_exact_dr())

    try:
        await scheduler.run()
    finally:
        try:
            if quiesced_operation is not None:
                await asyncio.gather(exact_dr_watch_task, return_exceptions=True)
            elif not exact_dr_watch_task.done():
                exact_dr_watch_task.cancel()
                await asyncio.gather(exact_dr_watch_task, return_exceptions=True)
            if shutdown_task is not None:
                await shutdown_task  # 等 graceful shutdown(取消延迟任务)完成
        finally:
            try:
                if quiesced_operation is not None:
                    # cancel(to_thread)只取消 await 包装;底层线程必须在 ack 前真实退出。
                    await loop.shutdown_default_executor()
                db.close()
            finally:
                try:
                    await redis.close()
                finally:
                    maintenance_lease.close()
                    if quiesced_operation is not None:
                        write_scheduler_quiesced(
                            config.data_dir,
                            operation_id=quiesced_operation,
                            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        )
                    logger.info("scheduler_exit")


if __name__ == "__main__":
    from shared.logging_setup import setup_logging
    setup_logging()
    asyncio.run(main())
