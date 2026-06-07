"""WorkerTransport：worker 与协调/状态后端之间的唯一接口。

P0-A：RedisTransport 包现有 redis_client + db(直连,零行为变化)。
P1+:GatewayTransport 实现同一 Protocol,全部换成出站 HTTPS,worker.py 不动。
worker.py 只依赖此 Protocol,不再 import redis_client / Database。

把 register/heartbeat/update_status/update_step_result 的 "Redis+DB 双写" 封在
transport 内部,worker.py 不再出现 asyncio.to_thread(self.db.xxx),双写顺序集中一处。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Protocol

from shared.db import Database
from shared.models import AIUsage, Worker as WorkerModel
from shared.redis_client import RedisClient


class WorkerTransport(Protocol):
    # ── 生命周期 / 心跳 ──
    async def register(
        self, worker_id: str, worker_type: str, pools: list[str],
        tags: set[str], reject_tags: set[str], hostname: str, now: datetime,
    ) -> str: ...

    async def heartbeat(self, worker_id: str) -> None: ...

    async def update_status(
        self, worker_id: str, status: str,
        current_job: str = "", current_step: str = "",
    ) -> None: ...

    async def get_worker_status(self, worker_id: str) -> str | None: ...

    # ── 资源池 / 队列认领 ──
    async def is_pool_frozen(self, pool: str) -> bool: ...
    async def try_acquire_slot(self, pool: str, limit: int) -> bool: ...
    async def release_slot(self, pool: str) -> None: ...
    async def freeze_pool(self, pool: str) -> None: ...
    async def unfreeze_pool(self, pool: str) -> None: ...
    async def dequeue_step_raw(self, pool: str) -> tuple[str, dict, float] | None: ...
    async def return_step(self, pool: str, raw_json: str, score: float) -> None: ...

    # ── 步骤状态机 ──
    async def cas_step_status(
        self, job_id: str, step: str, expected: str, new: str,
    ) -> bool: ...
    async def set_step_worker(self, job_id: str, step: str, worker_id: str) -> None: ...
    async def update_step_result(
        self, job_id: str, step: str, *,
        status: str, worker_id: str,
        started_at: datetime, finished_at: datetime, duration_sec: float,
        error: str | None = None,
    ) -> None: ...
    async def increment_worker_stats(
        self, worker_id: str, *,
        completed: int = 0, failed: int = 0, duration: float = 0.0,
    ) -> None: ...
    async def record_ai_usage(self, usage: AIUsage) -> None: ...

    # ── Job 上下文 ──
    async def get_job_pipeline(self, job_id: str) -> str | None: ...
    async def get_job_info(self, job_id: str) -> dict: ...

    # ── 事件 ──
    async def publish_step_event(self, channel: str, data: dict) -> None: ...

    async def close(self) -> None: ...


class RedisTransport:
    """P0-A:直连 redis_client + db,逐方法转调,零行为变化。"""

    def __init__(self, redis: RedisClient, db: Database):
        self._redis = redis
        self._db = db

    # ── 生命周期 / 心跳 ──
    async def register(self, worker_id, worker_type, pools, tags,
                       reject_tags, hostname, now):
        info = {
            "type": worker_type,
            "pools": ",".join(pools),
            "tags": ",".join(sorted(tags)),
            "reject_tags": ",".join(sorted(reject_tags)),
            "hostname": hostname,
            "status": "idle",
            "started_at": now.isoformat(),
            "last_heartbeat": now.isoformat(),
        }
        await self._redis.register_worker(worker_id, info, ttl=30)
        worker_model = WorkerModel(
            id=worker_id, type=worker_type, pools=pools,
            tags=tags, reject_tags=reject_tags, hostname=hostname,
            status="idle", started_at=now, first_seen=now, last_heartbeat=now,
        )
        await asyncio.to_thread(self._db.upsert_worker, worker_model)
        return worker_id

    async def heartbeat(self, worker_id):
        await self._redis.heartbeat(worker_id, ttl=30)
        await asyncio.to_thread(self._db.update_worker_heartbeat, worker_id)

    async def update_status(self, worker_id, status,
                            current_job="", current_step=""):
        await self._redis.set_worker_field(worker_id, "status", status)
        await self._redis.set_worker_field(worker_id, "current_job", current_job)
        await self._redis.set_worker_field(worker_id, "current_step", current_step)
        await asyncio.to_thread(
            self._db.update_worker_heartbeat, worker_id,
            status=status, current_job=current_job, current_step=current_step,
        )

    async def get_worker_status(self, worker_id):
        info = await self._redis.get_worker_info(worker_id)
        return info.get("status") if info else None

    # ── 资源池 / 队列(纯转调) ──
    async def is_pool_frozen(self, pool):
        return await self._redis.is_pool_frozen(pool)

    async def try_acquire_slot(self, pool, limit):
        return await self._redis.try_acquire_slot(pool, limit)

    async def release_slot(self, pool):
        await self._redis.release_slot(pool)

    async def freeze_pool(self, pool):
        await self._redis.freeze_pool(pool)

    async def unfreeze_pool(self, pool):
        await self._redis.unfreeze_pool(pool)

    async def dequeue_step_raw(self, pool):
        return await self._redis.dequeue_step_raw(pool)

    async def return_step(self, pool, raw_json, score):
        await self._redis.return_step(pool, raw_json, score)

    # ── 步骤状态机 ──
    async def cas_step_status(self, job_id, step, expected, new):
        return await self._redis.cas_step_status(job_id, step, expected, new)

    async def set_step_worker(self, job_id, step, worker_id):
        await self._redis.set_step_worker(job_id, step, worker_id)

    async def update_step_result(self, job_id, step, *, status, worker_id,
                                 started_at, finished_at, duration_sec,
                                 error=None):
        kwargs = dict(status=status, worker_id=worker_id,
                      started_at=started_at, finished_at=finished_at,
                      duration_sec=duration_sec)
        if error is not None:
            kwargs["error"] = error
        await asyncio.to_thread(self._db.update_step, job_id, step, **kwargs)

    async def increment_worker_stats(self, worker_id, *, completed=0,
                                     failed=0, duration=0.0):
        await asyncio.to_thread(
            self._db.increment_worker_stats, worker_id,
            completed=completed, failed=failed, duration=duration,
        )
        # 也累计进 Redis hash：远程(仅 Redis)worker 的统计才不会在 /api/workers 显示 0。
        if completed:
            await self._redis.incr_worker_stat(worker_id, "tasks_completed", completed)
        if failed:
            await self._redis.incr_worker_stat(worker_id, "tasks_failed", failed)
        if duration:
            await self._redis.incr_worker_stat(worker_id, "total_duration_sec", duration)

    async def record_ai_usage(self, usage):
        await asyncio.to_thread(self._db.record_ai_usage, usage)

    # ── Job 上下文 ──
    async def get_job_pipeline(self, job_id):
        return await self._redis.get_job_pipeline(job_id)

    async def get_job_info(self, job_id):
        return await self._redis.get_job_info(job_id)

    # ── 事件 ──
    async def publish_step_event(self, channel, data):
        await self._redis.publish(channel, data)

    async def close(self):
        # P0-A:redis/db 的关闭仍由 main.py 负责,此处 no-op。
        pass


def create_transport(redis: RedisClient, db: Database) -> WorkerTransport:
    """按 env 切换:GATEWAY_URL 有值→GatewayTransport(出站 HTTPS),否则 RedisTransport(直连)。"""
    base_url = os.environ.get("GATEWAY_URL")
    if base_url:
        from worker.gateway_transport import GatewayTransport

        return GatewayTransport(
            base_url,
            registration_token=os.environ.get("WORKER_REGISTRATION_TOKEN", ""),
            id_file=os.environ.get("WORKER_ID_FILE", "/data/.worker_id"),
            inner=RedisTransport(redis, db),
        )
    return RedisTransport(redis, db)
