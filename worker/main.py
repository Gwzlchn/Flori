"""Worker 入口。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path

import structlog

from shared.config import load_config
from shared.content_maintenance import acquire_service_lease
from shared.source_library import source_roots_from_env
from shared.db import Database
from shared.errors import WorkerFatalError
from shared.redis_client import RedisClient
from shared.storage import GatewayStorage, create_storage

from .transport import create_transport
from .worker import Worker, auto_discover_tags

logger = structlog.get_logger(component="worker")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Worker process")
    # 能力用 --pools 显式表达,路由按 pool 走。一台机器会几种活就 --pools 几个,
    # 无主次,如 cpu+gpu 强机 --pools cpu gpu。
    parser.add_argument(
        "--pools", nargs="+", required=True,
        help="本 worker 声明的资源池(能力集合,可多个),如 --pools cpu gpu。",
    )
    parser.add_argument("--tags", nargs="*", default=None, help="Capability tags")
    parser.add_argument("--reject-tags", nargs="*", default=None, help="Reject tags")
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="同时执行的 step 数(本机容量;默认 1,或 env WORKER_CONCURRENCY)。"
             "全局每池上限仍是系统级天花板。",
    )
    return parser.parse_args()


async def _initialize_runtime(args, config, gateway_url: str | None, redis_url: str | None):
    """构造 worker 运行资源;调用方在异常时负责释放 maintenance lease。"""
    redis: RedisClient | None = None
    db: Database | None = None
    if gateway_url is None or redis_url:
        effective_redis_url = redis_url or "redis://localhost:6379/0"
        redis = RedisClient(effective_redis_url)
        await redis.connect()
        await redis.ping()
        logger.info("redis_connected", url=effective_redis_url)
        db = Database(config.db_path)
        db.init_schema()

    transport = create_transport(redis, db, data_dir=config.data_dir)
    if gateway_url:
        work_dir = Path(os.environ.get("WORK_DIR", "/tmp/flori-work"))
        storage = GatewayStorage(
            gateway_url,
            token_getter=lambda: transport.worker_token,
            work_dir=work_dir,
        )
        logger.info("storage_gateway_proxy", pure=redis is None)
    else:
        storage = create_storage(config.jobs_dir)

    pools = args.pools
    worker_type = "+".join(sorted(set(pools)))
    tags = auto_discover_tags() | (set(args.tags) if args.tags else set())
    reject_tags = set(args.reject_tags) if args.reject_tags else set()
    concurrency = (
        args.concurrency if args.concurrency is not None
        else int(os.environ.get("WORKER_CONCURRENCY", "1"))
    )
    worker = Worker(
        transport=transport, config=config, storage=storage,
        worker_type=worker_type, pools=pools,
        tags=tags, reject_tags=reject_tags, concurrency=concurrency,
    )
    return transport, db, redis, worker


async def main() -> None:
    args = parse_args()

    data_dir = os.environ.get("DATA_DIR", "/data")
    # 默认从镜像烤入的 /app/configs 读(无状态 worker 不必显式传 CONFIG_DIR);
    # docker/base.Dockerfile 把 configs/ 复制到 /app/configs。挂 /data 卷的部署可显式覆盖。
    config_dir = os.environ.get("CONFIG_DIR", "/app/configs")
    config = load_config(config_dir=config_dir, data_dir=data_dir)

    gateway_url = os.environ.get("GATEWAY_URL")
    redis_url = os.environ.get("REDIS_URL")
    maintenance_lease = None
    if gateway_url is None or redis_url:
        maintenance_lease = acquire_service_lease(
            db_path=config.db_path,
            jobs_dir=config.jobs_dir,
            object_bucket=os.environ.get("MINIO_BUCKET"),
            config_root=config.prompts_dir,
            source_roots=source_roots_from_env().values(),
            owner="worker",
        )

    # 三种模式的资源构造都在 lease 内。初始化失败也必须显式释放,不能等进程退出。
    try:
        transport, db, redis, worker = await _initialize_runtime(
            args, config, gateway_url, redis_url,
        )
    except BaseException:
        if maintenance_lease is not None:
            maintenance_lease.close()
        raise

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.shutdown)

    try:
        await worker.run()
    finally:
        # 先优雅关 transport,再关 db/redis。gateway 模式才有 httpx AsyncClient 要释放;
        # 直连 RedisTransport.close 为 no-op、不触碰 redis/db,故无双关。
        try:
            await transport.close()
        finally:
            try:
                if db is not None:
                    db.close()
            finally:
                try:
                    if redis is not None:
                        await redis.close()
                finally:
                    if maintenance_lease is not None:
                        maintenance_lease.close()


if __name__ == "__main__":
    from shared.logging_setup import setup_logging
    setup_logging()
    try:
        asyncio.run(main())
    except WorkerFatalError as e:
        logger.error(
            "worker_fatal_exit",
            reason=e.reason,
            status_code=e.status_code,
            endpoint=e.endpoint,
            error=str(e)[:200],
        )
        raise SystemExit(1)
