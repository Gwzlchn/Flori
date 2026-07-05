"""认领/上报编排:RedisTransport 与 gateway 服务端共用的唯一实现,避免两端漂移。

"从队列认领一步 / 上报完成 / 上报失败 / 释放"这套 redis+db 编排做成
(redis, db, ...) 上的纯函数。RedisTransport 是调用本模块的薄包装;gateway 的
/api/runner/jobs/* 端点也调本模块——同一份调用序列、同一份 payload、同一份 DB 写,
两端不会各写一份导致行为分叉。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timezone

from shared.db import Database
from shared.models import AIUsage
from shared.redis_client import RedisClient


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


async def _update_step_result(
    db: Database, job_id: str, step: str, *,
    status: str, worker_id: str,
    started_at: datetime, finished_at: datetime, duration_sec: float,
    error: str | None = None, only_if_active: bool = False,
) -> None:
    kwargs = dict(status=status, worker_id=worker_id,
                  started_at=started_at, finished_at=finished_at,
                  duration_sec=duration_sec)
    if error is not None:
        kwargs["error"] = error
    await asyncio.to_thread(
        db.update_step, job_id, step, only_if_active=only_if_active, **kwargs
    )


async def _increment_worker_stats(
    redis: RedisClient, db: Database, worker_id: str, *,
    completed: int = 0, failed: int = 0, duration: float = 0.0,
) -> None:
    await asyncio.to_thread(
        db.increment_worker_stats, worker_id,
        completed=completed, failed=failed, duration=duration,
    )
    # 也累计进 Redis hash:远程(仅 Redis)worker 的统计才不会在 /api/workers 显示 0。
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


# 粗粒度编排


async def claim_step(
    redis: RedisClient, db: Database, worker_id: str,
    pools, pool_limits, tags, reject_tags,
) -> dict | None:
    """从池队列认领一步,返回最小 claim {job_id, step, pool, exec_id} 或 None。"""
    # 暂停(paused)的 worker 不再认领新任务。读独立的 admin_status 叠加位,
    # 与运行时 status(idle/busy) 解耦——claim/release 写 status 不会覆盖暂停态。
    info = await redis.get_worker_info(worker_id)
    if (info.get("admin_status") if info else None) == "paused":
        return None

    # 本次认领的唯一 holder(= exec_id),先于占槽生成。并发槽用 holder 集合 pool/res:*:holders 记账,
    # 占/放/reclaim/删除均按此 holder SADD/SREM;SREM 幂等不双减,worker 突死的槽可被 reclaim/对账精准释放。
    # 加短随机,防同 worker 同毫秒并发认领时 holder 相撞:相撞则两个 claim 共用一个 holder,SCARD 少计导致超额。
    holder = f"{worker_id}:{int(time.time() * 1000)}:{secrets.token_hex(3)}"

    for pool in pools:
        if await redis.is_pool_frozen(pool):
            continue
        # 限额来自 worker 传入的 pool_limits,缺省 999。
        limit = pool_limits.get(pool, 999)
        override = await redis.get_pool_limit_override(pool)
        if override is not None:
            limit = override  # 运行时覆盖即最终上限:直连+网关两路都过本函数,前端调小即时生效
        if not await redis.try_acquire_slot(pool, limit, holder):
            continue

        matched = await pop_matching(redis, pool, tags, reject_tags)
        if matched is None:
            await redis.release_slot(pool, holder)
            continue

        task, raw_json, score = matched

        # 独立 AI task(kind='ai')分流
        # 没有 job_id / job:{id}:steps hash,绝不喂进下方 job-step 状态机(cas_step_status/set_step_*),
        # 也不占资源槽。已占的池槽(上方 try_acquire_slot)持槽执行,done 由 release_step 的 ai 分支释放。
        if task.get("kind") == "ai":
            task_id = task.get("task_id")
            step_name = task.get("step", "ai")
            exec_id = holder
            try:
                await _set_status(redis, db, worker_id, "busy", task_id, step_name)
                await redis.publish(f"events:{task_id}", {
                    "event": "ai_task_start", "task_id": task_id,
                    "step": step_name, "worker": worker_id,
                })
            except Exception:
                # dequeue 成功但随后写状态/publish 抛错:放回任务 + 释放槽,避免吞任务/泄漏槽。
                try:
                    await redis.return_step(pool, raw_json, score)
                except Exception:
                    pass
                try:
                    await redis.release_slot(pool, holder)
                except Exception:
                    pass
                raise
            return {
                "kind": "ai", "task_id": task_id, "step": step_name, "pool": pool,
                "exec_id": exec_id, "request": task.get("request", {}), "domain": task.get("domain"),
            }

        job_id = task["job_id"]
        step = task["step"]

        # 资源槽:单账号/单出口IP 等细粒度并发。任务在 enqueue 时带 resources;对每个有配置上限的资源
        # 占一个槽,上限存 redis resource_limits,由 scheduler 从 configs/resources.yaml 推送。
        # 任一占不到 → 回滚已占资源 + 释放池槽 + 把任务放回队列,继续看下一个池,不绑定本 worker。
        # 未配上限的资源跳过:声明了但 resources.yaml 没配 = 不限,安全降级;无声明则整段零开销。
        acquired_resources: list[str] = []
        resource_blocked = False
        for res in task.get("resources", []):
            limit = await redis.get_resource_limit(res)
            if limit is None:
                continue
            if await redis.try_acquire_resource(res, limit, holder):
                acquired_resources.append(res)
            else:
                resource_blocked = True
                break
        if resource_blocked:
            for res in acquired_resources:
                await redis.release_resource(res, holder)
            await redis.release_slot(pool, holder)
            await redis.return_step(pool, raw_json, score)
            continue

        exec_id = holder
        try:
            acquired = await redis.cas_step_status(job_id, step, "ready", "running")
            if not acquired:
                # CAS 失败(被他人抢先):释放槽 + 归还资源槽,继续看其他池。
                await redis.release_slot(pool, holder)
                for res in acquired_resources:
                    await redis.release_resource(res, holder)
                continue

            await redis.set_step_worker(job_id, step, worker_id)
            await redis.set_step_exec_id(job_id, step, exec_id)
            # 认领即刷进度心跳:覆盖上次执行(被杀/重跑)残留的旧 progress_at,否则 check_stuck 在
            # "认领→worker 首拍 on_tick"窗口读到旧值,按 now-旧值(可达小时/天级)误杀刚认领的步
            # (线上踩过:rerun 后 9 步被 "progress stale 250689s" 秒杀)。
            await redis.set_step_progress_at(job_id, step)
            if acquired_resources:
                # 存 redis 供 release_step / orphan 回收据此释放(gateway release 请求不回传资源)。
                await redis.set_step_resources(job_id, step, acquired_resources)
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
            # 否则这条任务被永久吞掉。释放槽/归还资源让占用不泄漏。
            try:
                await redis.return_step(pool, raw_json, score)
            except Exception:
                pass
            try:
                await redis.release_slot(pool, holder)
            except Exception:
                pass
            for res in acquired_resources:
                try:
                    await redis.release_resource(res, holder)
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
        db, job_id, step, status="done", worker_id=worker_id,
        started_at=datetime.fromtimestamp(started_at, timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_sec=round(duration, 1),
        # 与失败侧对称:不覆盖已终态(done/skipped)的步——挡迟到的成功上报把已被 skip 的步倒回 done。
        # waiting/rerun-reset 不在终态集,本守卫不拦,属可接受残留。
        only_if_active=True,
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
    # rc!=0 分支带 exec_id 且 events 用 error[:200];timeout/异常分支不带 exec_id——两分支 payload 刻意不同,勿顺手统一。
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
        db, job_id, step, status="failed", error=error, worker_id=worker_id,
        started_at=datetime.fromtimestamp(started_at, timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_sec=round(duration, 1),
        # 不覆盖已终态成功的步:成功上报响应丢失被改报 failed 时,DB 仍保 done。
        only_if_active=True,
    )
    # 统计怪癖:仅 rc!=0(count_stats=True)累加 failed;timeout/异常分支刻意不计。
    if count_stats:
        await _increment_worker_stats(redis, db, worker_id, failed=1)


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
