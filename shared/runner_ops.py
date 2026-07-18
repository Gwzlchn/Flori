"""认领/上报编排:RedisTransport 与 gateway 服务端共用的唯一实现,避免两端漂移。

"从队列认领一步 / 上报完成 / 上报失败 / 释放"这套 redis+db 编排做成
(redis, db, ...) 上的纯函数。RedisTransport 是调用本模块的薄包装;gateway 的
/api/runner/jobs/* 端点也调本模块.同一份调用序列,同一份 payload,同一份 DB 写,
两端不会各写一份导致行为分叉。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass
import json
import secrets
import time

from shared.db import Database
from shared.redis_client import RedisClient


@dataclass(frozen=True)
class TaskLease:
    """当前异步 worker 槽持有的任务租约."""

    worker_id: str
    job_id: str
    step: str
    exec_id: str


_CURRENT_TASK_LEASE: ContextVar[TaskLease | None] = ContextVar(
    "flori_current_task_lease", default=None,
)


def bind_task_lease(lease: TaskLease) -> Token:
    """把 claim 四元组绑定到当前异步执行上下文."""
    return _CURRENT_TASK_LEASE.set(lease)


def current_task_lease() -> TaskLease | None:
    return _CURRENT_TASK_LEASE.get()


def clear_task_lease() -> None:
    _CURRENT_TASK_LEASE.set(None)


def parse_style_tags(raw) -> list:
    """解析 job_info.style_tags 原始值(JSON 字符串或已是 list);失败/非 list 兜空 list。
    供 scheduler.enqueue_step / worker.claim / api.runner.request_job 三处共用,避免各写一份漂移。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


# 内部小工具


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


async def pop_matching(
    redis: RedisClient, pool, tags, reject_tags, max_tries=5, *, exclude_kind=None,
):
    # 不匹配项先暂存,否则同分数成员每次放回后可能连续弹到自己,永远看不到后续任务。
    skipped: list[tuple[str, float]] = []
    try:
        for _ in range(max_tries):
            result = await redis.dequeue_step_raw(pool)
            if result is None:
                return None
            raw_json, task, score = result
            require_tags = set(task.get("require_tags", []))
            all_tags = set(task.get("tags", []))
            if (
                (exclude_kind is None or task.get("kind") != exclude_kind)
                and require_tags.issubset(tags)
                and not all_tags.intersection(reject_tags)
            ):
                return task, raw_json, score
            skipped.append((raw_json, score))
        return None
    finally:
        for raw_json, score in skipped:
            await redis.return_step(pool, raw_json, score)


# 粗粒度编排


async def claim_step(
    redis: RedisClient, db: Database, worker_id: str,
    pools, pool_limits, tags, reject_tags,
) -> dict | None:
    """从池队列认领一步,返回最小 claim {job_id, step, pool, exec_id} 或 None。"""
    info = await redis.get_worker_info(worker_id)
    if (info.get("admin_status") if info else None) == "paused":
        return None
    holder = f"{worker_id}:{int(time.time() * 1000)}:{secrets.token_hex(3)}"

    for pool in pools:
        if await redis.is_pool_frozen(pool):
            continue
        limit = pool_limits.get(pool, 999)
        if pool == "ai":
            override = await redis.get_pool_limit_override(pool)
            if not await redis.try_acquire_slot(
                pool, override if override is not None else limit, holder,
            ):
                continue
            ai_claim = await redis.claim_ai_task(
                worker_id=worker_id,
                claim_id=holder,
                tags=tags,
                reject_tags=reject_tags,
            )
            if ai_claim is not None:
                task_id = ai_claim["task_id"]
                step_name = ai_claim.get("step", "ai")
                try:
                    await _set_status(redis, db, worker_id, "busy", task_id, step_name)
                    await redis.publish(f"events:{task_id}", {
                        "event": "ai_task_start", "task_id": task_id,
                        "step": step_name, "worker": worker_id,
                    })
                except Exception:
                    try:
                        await redis.abandon_ai_task_claim(
                            task_id=task_id,
                            batch_id=ai_claim.get("batch_id", ""),
                            attempt=int(ai_claim.get("attempt", 0)),
                            revision=int(ai_claim.get("revision", 0)),
                            worker_id=worker_id,
                            claim_id=ai_claim["claim_id"],
                        )
                    finally:
                        await redis.release_slot(pool, holder)
                        try:
                            await _set_status(redis, db, worker_id, "idle")
                        except Exception:
                            pass
                    raise
                return {
                    **ai_claim,
                    "kind": "ai",
                    "pool": pool,
                    "exec_id": holder,
                }
            await redis.release_slot(pool, holder)

        claim = await redis.claim_pipeline_step_atomic(
            pool=pool,
            worker_id=worker_id,
            exec_id=holder,
            default_limit=limit,
            tags=set(tags),
            reject_tags=set(reject_tags),
        )
        if claim is None:
            continue
        await _set_status(
            redis, db, worker_id, "busy", claim["job_id"], claim["step"],
        )
        await redis.publish("step_started", {
            "job_id": claim["job_id"], "step": claim["step"],
            "status": "running", "worker": worker_id,
            "exec_id": claim["exec_id"], "generation": claim["generation"],
        })
        await redis.publish(f"events:{claim['job_id']}", {
            "event": "step_start", "step": claim["step"], "worker": worker_id,
        })
        from shared.source_detect import detect_source
        from shared.step_scope import parse_execution_step, part_id_from_scope
        scope_key, step = parse_execution_step(claim["step"])
        part_id = part_id_from_scope(scope_key)
        if part_id is not None:
            from shared.source_library import SOURCE_MEDIA_STEPS
            parts = await asyncio.to_thread(db.get_parts, claim["job_id"])
            part = next((item for item in parts if item.id == part_id), None)
            if part is None:
                await redis.release_holders({claim["exec_id"]})
                raise RuntimeError("claimed step references a missing part")
            source = str((part.meta or {}).get("source") or "") or detect_source(
                part.source_url or "",
            )
            claim = {
                **claim,
                "source": source,
            }
            if step in SOURCE_MEDIA_STEPS and part.source_ref:
                claim.update({
                    "source_ref": part.source_ref,
                    "source_digest": part.source_digest,
                    "source_size_bytes": part.size_bytes,
                })
        return claim

    return None


async def report_step_done(
    redis: RedisClient, db: Database, worker_id: str,
    claim: dict, duration: float, started_at: float,
) -> bool:
    job_id = claim["job_id"]
    step = claim["step"]
    result, _ = await redis.append_terminal_if_current("step_completed", {
        "job_id": job_id, "step": step, "status": "done",
        "duration": round(duration, 1),
        "worker": worker_id, "exec_id": claim["exec_id"],
        "generation": claim.get("generation"),
        "started_at": started_at,
    })
    return result == 1


async def report_step_failed(
    redis: RedisClient, db: Database, worker_id: str,
    claim: dict, error: str, error_type: str,
    duration: float, started_at: float, count_stats: bool,
) -> bool:
    job_id = claim["job_id"]
    step = claim["step"]
    topic_payload = {
        "job_id": job_id, "step": step, "status": "failed",
        "error": error, "error_type": error_type, "worker": worker_id,
        "exec_id": claim["exec_id"], "generation": claim.get("generation"),
        "duration": round(duration, 1), "started_at": started_at,
        "count_stats": bool(count_stats),
    }
    result, _ = await redis.append_terminal_if_current("step_failed", topic_payload)
    return result == 1


async def release_step(
    redis: RedisClient, db: Database, worker_id: str, claim: dict,
) -> None:
    pool = claim["pool"]
    holder = claim.get("exec_id")   # = 占槽时的 holder,本执行唯一;按它 SREM 释放自己的槽/资源,幂等。
    # 独立 AI task:无 job:steps,跳过 exec_id 守卫 / 资源回读;仅释放池槽 + 置 idle。
    if claim.get("kind") == "ai":
        await redis.release_slot(pool, holder)
        await _set_status(redis, db, worker_id, "idle")
        return
    job_id, step = claim["job_id"], claim["step"]
    # exec_id 守卫:若该步已被更新的执行接管(check_stuck 重排后 worker B 以新 exec_id 认领),
    # 本陈旧 worker 迟到的 release 不得动新执行的槽/资源。holder 集合下本 worker 只 SREM 自己的
    # holder,已被 reclaim SREM 则为 no-op,不会误删新执行的 holder;仍提前 return 省去无谓回读。
    current_exec = await redis.get_step_exec_id(job_id, step)
    if (current_exec is not None and holder is not None
            and current_exec != holder):
        await redis.release_slot(pool, holder)   # 幂等:释放自己(陈旧)的 holder,防其残留泄漏
        await redis.revoke_task_lease(holder)
        await _set_status(redis, db, worker_id, "idle")
        return
    await redis.release_slot(pool, holder)
    # 归还本步占用的资源槽(从 redis 读,gateway release 请求不回传资源列表);清记录防重复归还。
    resources = await redis.get_step_resources(job_id, step)
    if resources:
        for res in resources:
            await redis.release_resource(res, holder)
        await redis.clear_step_resources(job_id, step)
    await _set_status(redis, db, worker_id, "idle")
    await redis.mark_task_lease_released(
        worker_id, job_id, step, holder, pool,
    )
