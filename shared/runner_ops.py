"""认领/上报编排：RedisTransport 与 gateway 服务端共用的唯一实现(避免漂移)。

把"从队列认领一步 / 上报完成 / 上报失败 / 释放"这套 redis+db 编排从
RedisTransport 抽出来,做成 (redis, db, ...) 上的纯函数。RedisTransport 退化成
薄包装调用本模块;gateway 的 /api/runner/jobs/* 端点也调本模块——同一份调用序列、
同一份 payload、同一份 DB 写,两端不会各写一份导致行为分叉。

调用序列/payload/DB 写与原 RedisTransport 方法体逐字等价(Verify 会对照回归)。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from shared.db import Database
from shared.models import AIUsage
from shared.redis_client import RedisClient


# ── 内部小工具 ──


async def _set_status(
    redis: RedisClient, db: Database, worker_id: str, status: str,
    current_job: str = "", current_step: str = "",
) -> None:
    # 设 worker 状态:Redis 字段 + DB 心跳双写(等价 RedisTransport.update_status)。
    await redis.set_worker_field(worker_id, "status", status)
    await redis.set_worker_field(worker_id, "current_job", current_job)
    await redis.set_worker_field(worker_id, "current_step", current_step)
    await asyncio.to_thread(
        db.update_worker_heartbeat, worker_id,
        status=status, current_job=current_job, current_step=current_step,
    )


async def _update_step_result(
    redis: RedisClient, db: Database, job_id: str, step: str, *,
    status: str, worker_id: str,
    started_at: datetime, finished_at: datetime, duration_sec: float,
    error: str | None = None,
) -> None:
    kwargs = dict(status=status, worker_id=worker_id,
                  started_at=started_at, finished_at=finished_at,
                  duration_sec=duration_sec)
    if error is not None:
        kwargs["error"] = error
    await asyncio.to_thread(db.update_step, job_id, step, **kwargs)


async def _increment_worker_stats(
    redis: RedisClient, db: Database, worker_id: str, *,
    completed: int = 0, failed: int = 0, duration: float = 0.0,
) -> None:
    await asyncio.to_thread(
        db.increment_worker_stats, worker_id,
        completed=completed, failed=failed, duration=duration,
    )
    # 也累计进 Redis hash：远程(仅 Redis)worker 的统计才不会在 /api/workers 显示 0。
    if completed:
        await redis.incr_worker_stat(worker_id, "tasks_completed", completed)
    if failed:
        await redis.incr_worker_stat(worker_id, "tasks_failed", failed)
    if duration:
        await redis.incr_worker_stat(worker_id, "total_duration_sec", duration)


async def pop_matching(redis: RedisClient, pool, tags, reject_tags, max_tries=5):
    # 从池队列取出首个标签匹配的任务,不匹配则放回,最多重试 max_tries 次。
    for _ in range(max_tries):
        result = await redis.dequeue_step_raw(pool)
        if result is None:
            return None
        raw_json, task, score = result
        require_tags = set(task.get("require_tags", []))
        all_tags = set(task.get("tags", []))
        if require_tags.issubset(tags) and not all_tags.intersection(reject_tags):
            return task, raw_json, score
        await redis.return_step(pool, raw_json, score)
    return None


# ── 粗粒度编排 ──


async def claim_step(
    redis: RedisClient, db: Database, worker_id: str,
    pools, pool_limits, tags, reject_tags,
) -> dict | None:
    """从池队列认领一步,返回最小 claim {job_id, step, pool, exec_id} 或 None。"""
    # draining 的 worker 不再认领新任务。
    info = await redis.get_worker_info(worker_id)
    if (info.get("status") if info else None) == "draining":
        return None

    for pool in pools:
        if await redis.is_pool_frozen(pool):
            continue
        # 限额来自 worker 传入的 pool_limits(等价旧 fetch_task 读 self.config.pools 的 limit,缺省 999)。
        limit = pool_limits.get(pool, 999)
        if not await redis.try_acquire_slot(pool, limit):
            continue

        matched = await pop_matching(redis, pool, tags, reject_tags)
        if matched is None:
            await redis.release_slot(pool)
            continue

        task, raw_json, score = matched
        job_id = task["job_id"]
        step = task["step"]
        if pool == "scene":
            await redis.freeze_pool("cpu")

        exec_id = f"{worker_id}:{int(time.time() * 1000)}"
        try:
            acquired = await redis.cas_step_status(job_id, step, "ready", "running")
            if not acquired:
                # CAS 失败(被他人抢先):释放槽 + 解冻 cpu,跳过本认领继续看其他池(净效果与旧 execute 一致)。
                await redis.release_slot(pool)
                if pool == "scene":
                    await redis.unfreeze_pool("cpu")
                continue

            await redis.set_step_worker(job_id, step, worker_id)
            await _set_status(redis, db, worker_id, "busy", job_id, step)
            await redis.publish("step_started", {
                "job_id": job_id, "step": step, "status": "running",
                "worker": worker_id, "exec_id": exec_id,
            })
            await redis.publish(f"events:{job_id}", {
                "event": "step_start", "step": step, "worker": worker_id,
            })
        except Exception:
            # dequeue 成功但随后 CAS/publish 抛错时,把 raw 放回队列(尽力而为),
            # 否则这条任务被永久吞掉。释放槽/解冻 cpu 让占用不泄漏。
            try:
                await redis.return_step(pool, raw_json, score)
            except Exception:
                pass
            try:
                await redis.release_slot(pool)
                if pool == "scene":
                    await redis.unfreeze_pool("cpu")
            except Exception:
                pass
            raise

        # pipeline/domain/style_tags 不在认领时读:直连模式留给 worker 在 execute 内解析;
        # gateway 模式由端点 enrich 后塞进 claim,worker 直接用、无需回读 redis。
        return {"job_id": job_id, "step": step, "pool": pool, "exec_id": exec_id}

    return None


async def report_step_done(
    redis: RedisClient, db: Database, worker_id: str,
    claim: dict, duration: float, started_at: float,
) -> None:
    job_id = claim["job_id"]
    step = claim["step"]
    await redis.publish("step_completed", {
        "job_id": job_id, "step": step, "status": "done",
        "duration": round(duration, 1),
        "worker": worker_id, "exec_id": claim["exec_id"],
    })
    await redis.publish(f"events:{job_id}", {
        "event": "step_done", "step": step,
        "duration_sec": round(duration, 1),
    })
    await _update_step_result(
        redis, db, job_id, step, status="done", worker_id=worker_id,
        started_at=datetime.fromtimestamp(started_at, timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_sec=round(duration, 1),
    )
    await _increment_worker_stats(
        redis, db, worker_id, completed=1, duration=round(duration, 1),
    )


async def report_step_failed(
    redis: RedisClient, db: Database, worker_id: str,
    claim: dict, error: str, error_type: str,
    duration: float, started_at: float, count_stats: bool,
) -> None:
    job_id = claim["job_id"]
    step = claim["step"]
    # rc!=0 分支带 exec_id 且 events 用 error[:200];timeout/异常分支不带 exec_id(逐字保持旧 payload)。
    topic_payload = {
        "job_id": job_id, "step": step, "status": "failed",
        "error": error, "error_type": error_type, "worker": worker_id,
    }
    if count_stats:
        topic_payload["exec_id"] = claim["exec_id"]
        events_error = error[:200]
    else:
        events_error = error
    await redis.publish("step_failed", topic_payload)
    await redis.publish(f"events:{job_id}", {
        "event": "step_failed", "step": step, "error": events_error,
    })
    await _update_step_result(
        redis, db, job_id, step, status="failed", error=error, worker_id=worker_id,
        started_at=datetime.fromtimestamp(started_at, timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_sec=round(duration, 1),
    )
    # 统计怪癖:仅 rc!=0(count_stats=True)累加 failed;timeout/异常分支不计(与旧 execute 一致)。
    if count_stats:
        await _increment_worker_stats(redis, db, worker_id, failed=1)


async def release_step(
    redis: RedisClient, db: Database, worker_id: str, claim: dict,
) -> None:
    pool = claim["pool"]
    await redis.release_slot(pool)
    if pool == "scene":
        await redis.unfreeze_pool("cpu")
    await _set_status(redis, db, worker_id, "idle")
