"""Worker 入口。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path

import structlog

from shared.config import load_config
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
        help="本 worker 订阅的资源池(能力集合,可多个),如 --pools cpu gpu。",
    )
    parser.add_argument("--tags", nargs="*", default=None, help="Capability tags")
    parser.add_argument("--reject-tags", nargs="*", default=None, help="Reject tags")
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="同时执行的 step 数(本机容量;默认 1,或 env WORKER_CONCURRENCY)。"
             "全局每池上限仍是系统级天花板。",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    data_dir = os.environ.get("DATA_DIR", "/data")
    # 默认从镜像烤入的 /app/configs 读(无状态 worker 不必显式传 CONFIG_DIR);
    # docker/base.Dockerfile 把 configs/ 复制到 /app/configs。挂 /data 卷的部署可显式覆盖。
    config_dir = os.environ.get("CONFIG_DIR", "/app/configs")
    config = load_config(config_dir=config_dir, data_dir=data_dir)

    gateway_url = os.environ.get("GATEWAY_URL")
    redis_url = os.environ.get("REDIS_URL")

    # 三种模式:
    #  1) GATEWAY_URL 未设(本地/单机):redis+db 直连,RedisTransport,本地/远端存储。
    #  2) GATEWAY_URL 设 + REDIS_URL 设(混合):redis+db 作内层兜底,产物走 gateway。
    #  3) GATEWAY_URL 设 + REDIS_URL 未设(真零隧道):跳过 redis+db,只出站 HTTPS。
    redis: RedisClient | None = None
    db: Database | None = None
    if gateway_url is None or redis_url:
        # 未设 GATEWAY_URL 时用默认本机地址;混合模式用显式 REDIS_URL。
        effective_redis_url = redis_url or "redis://localhost:6379/0"
        redis = RedisClient(effective_redis_url)
        await redis.connect()
        await redis.ping()
        logger.info("redis_connected", url=effective_redis_url)
        db = Database(config.db_path)
        db.init_schema()

    transport = create_transport(redis, db)

    if gateway_url:
        # 产物经网关中转:token_getter 绑定 transport,用 register/resume 后的 worker token。
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
    # worker_type 仅作显示标签,路由一律按 pools;从 pools 集合派生:单池="cpu",多池="cpu+gpu"。
    worker_type = "+".join(sorted(set(pools)))
    tags = set(args.tags) if args.tags else auto_discover_tags()
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

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.shutdown)

    try:
        await worker.run()
    finally:
        # 先优雅关 transport,再关 db/redis。gateway 模式才有 httpx AsyncClient 要释放;
        # 直连 RedisTransport.close 为 no-op、不触碰 redis/db,故无双关。
        await transport.close()
        if db is not None:
            db.close()
        if redis is not None:
            await redis.close()


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
