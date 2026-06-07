"""WorkerTransport：worker 与协调/状态后端之间的唯一接口。

P0-A：RedisTransport 包现有 redis_client + db(直连,零行为变化)。
P1+:GatewayTransport 实现同一 Protocol,全部换成出站 HTTPS,worker.py 不动。
worker.py 只依赖此 Protocol,不再 import redis_client / Database。

把 register/heartbeat/update_status/update_step_result 的 "Redis+DB 双写" 封在
transport 内部,worker.py 不再出现 asyncio.to_thread(self.db.xxx),双写顺序集中一处。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
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

    # ── 粗粒度认领/上报(P3a)：编排封装在 transport 内,worker.execute 不再直接调细粒度方法 ──
    # pool_limits:每池槽位上限(由 worker 从 config 算好传入,transport 保持不持有 config)。
    async def request_step(
        self, worker_id: str, pools: list[str], pool_limits: dict[str, int],
        tags: set[str], reject_tags: set[str],
    ) -> dict | None: ...

    async def report_done(self, claim: dict, duration: float, started_at: float) -> None: ...

    async def report_failed(
        self, claim: dict, error: str, error_type: str,
        duration: float, started_at: float, count_stats: bool,
    ) -> None: ...

    async def release(self, claim: dict) -> None: ...

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
        # 粗粒度上报需要 worker_id,注册/认领时记下,report_*/release 据此回写。
        self._worker_id = ""

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
        self._worker_id = worker_id
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

    # ── 粗粒度认领/上报(P3a:包细粒度方法,调用与顺序与旧 worker.execute 逐字等价) ──

    async def _pop_matching(self, pool, tags, reject_tags, max_tries=5):
        # 从池队列取出首个标签匹配的任务,不匹配则放回,最多重试 max_tries 次。
        for _ in range(max_tries):
            result = await self._redis.dequeue_step_raw(pool)
            if result is None:
                return None
            raw_json, task, score = result
            require_tags = set(task.get("require_tags", []))
            all_tags = set(task.get("tags", []))
            if require_tags.issubset(tags) and not all_tags.intersection(reject_tags):
                return task, raw_json, score
            await self._redis.return_step(pool, raw_json, score)
        return None

    async def request_step(self, worker_id, pools, pool_limits, tags, reject_tags):
        self._worker_id = worker_id
        if await self.get_worker_status(worker_id) == "draining":
            return None

        for pool in pools:
            if await self._redis.is_pool_frozen(pool):
                continue
            # 限额来自 worker 传入的 pool_limits(等价旧 fetch_task 读 self.config.pools 的 limit,缺省 999)。
            limit = pool_limits.get(pool, 999)
            if not await self._redis.try_acquire_slot(pool, limit):
                continue

            matched = await self._pop_matching(pool, tags, reject_tags)
            if matched is None:
                await self._redis.release_slot(pool)
                continue

            task, _raw_json, _score = matched
            job_id = task["job_id"]
            step = task["step"]
            if pool == "scene":
                await self._redis.freeze_pool("cpu")

            exec_id = f"{worker_id}:{int(time.time() * 1000)}"
            acquired = await self._redis.cas_step_status(job_id, step, "ready", "running")
            if not acquired:
                # CAS 失败(被他人抢先):释放槽 + 解冻 cpu,跳过本认领继续看其他池(净效果与旧 execute 一致)。
                await self._redis.release_slot(pool)
                if pool == "scene":
                    await self._redis.unfreeze_pool("cpu")
                continue

            await self._redis.set_step_worker(job_id, step, worker_id)
            await self.update_status(worker_id, "busy", job_id, step)
            await self._redis.publish("step_started", {
                "job_id": job_id, "step": step, "status": "running",
                "worker": worker_id, "exec_id": exec_id,
            })
            await self._redis.publish(f"events:{job_id}", {
                "event": "step_start", "step": step, "worker": worker_id,
            })

            # pipeline/domain/style_tags 不在认领时读:留给 worker 在 execute 的 try 内解析,
            # 任意读失败都由 execute 接住转 report_failed,不会冲垮主循环(保持旧的故障隔离)。
            # gateway 模式(P3b)服务端会把这几项塞进 claim,worker 直接用、无需回读 redis。
            return {"job_id": job_id, "step": step, "pool": pool, "exec_id": exec_id}

        return None

    async def report_done(self, claim, duration, started_at):
        job_id = claim["job_id"]
        step = claim["step"]
        await self._redis.publish("step_completed", {
            "job_id": job_id, "step": step, "status": "done",
            "duration": round(duration, 1),
            "worker": self._worker_id, "exec_id": claim["exec_id"],
        })
        await self._redis.publish(f"events:{job_id}", {
            "event": "step_done", "step": step,
            "duration_sec": round(duration, 1),
        })
        await self.update_step_result(
            job_id, step, status="done", worker_id=self._worker_id,
            started_at=datetime.fromtimestamp(started_at, timezone.utc),
            finished_at=datetime.now(timezone.utc),
            duration_sec=round(duration, 1),
        )
        await self.increment_worker_stats(
            self._worker_id, completed=1, duration=round(duration, 1),
        )

    async def report_failed(self, claim, error, error_type, duration,
                            started_at, count_stats):
        job_id = claim["job_id"]
        step = claim["step"]
        # rc!=0 分支带 exec_id 且 events 用 error[:200];timeout/异常分支不带 exec_id(逐字保持旧 payload)。
        topic_payload = {
            "job_id": job_id, "step": step, "status": "failed",
            "error": error, "error_type": error_type, "worker": self._worker_id,
        }
        if count_stats:
            topic_payload["exec_id"] = claim["exec_id"]
            events_error = error[:200]
        else:
            events_error = error
        await self._redis.publish("step_failed", topic_payload)
        await self._redis.publish(f"events:{job_id}", {
            "event": "step_failed", "step": step, "error": events_error,
        })
        await self.update_step_result(
            job_id, step, status="failed", error=error, worker_id=self._worker_id,
            started_at=datetime.fromtimestamp(started_at, timezone.utc),
            finished_at=datetime.now(timezone.utc),
            duration_sec=round(duration, 1),
        )
        # 统计怪癖:仅 rc!=0(count_stats=True)累加 failed;timeout/异常分支不计(与旧 execute 一致)。
        if count_stats:
            await self.increment_worker_stats(self._worker_id, failed=1)

    async def release(self, claim):
        pool = claim["pool"]
        await self._redis.release_slot(pool)
        if pool == "scene":
            await self._redis.unfreeze_pool("cpu")
        await self.update_status(self._worker_id, "idle")

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
