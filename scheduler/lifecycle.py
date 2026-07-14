"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
from datetime import datetime, timezone

import structlog

from shared.errors import RETRY_POLICY, get_retry_delay
from shared.models import Job, JobStatus, Step, StepStatus

if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

# 延迟重试任务的 name 前缀,跟踪/按 job 取消时复用,避免格式漂移。
_DELAYED_PREFIX = "delayed_enqueue:"

class LifecycleCoordinator:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def submit_job(self, job: Job) -> None:
        """API 调用:提交新任务,初始化步骤状态,入队无依赖步骤。"""
        pipeline_steps = self.owner._get_pipeline_steps(job.pipeline)
        if not pipeline_steps:
            logger.warning("empty_pipeline", job_id=job.id, pipeline=job.pipeline)
            await asyncio.to_thread(
                self.owner.db.update_job, job.id,
                status=JobStatus.FAILED, error=f"unknown pipeline: {job.pipeline}",
            )
            return

        await self.owner.redis.init_job(job.id, job.pipeline, {
            "domain": job.domain,
            "style_tags": job.style_tags,
            "url": job.url or "",
            "source": job.source or "",
            # 投递开关(如 smart_note),供 rules 的 if_flag 求值,条件跳步见 _eval_rules。
            "flags": (job.meta or {}).get("flags", {}),
        })

        redis_status = await self.owner.redis.get_all_step_statuses(job.id)
        db_steps = {
            item.name: item for item in await asyncio.to_thread(self.owner.db.get_steps, job.id)
        }
        for name, cfg in pipeline_steps.items():
            status = redis_status.get(name)
            if status is None and name in db_steps:
                stored = db_steps[name].status
                status = stored.value if isinstance(stored, StepStatus) else str(stored)
            if status is None:
                status = "waiting"
            if name not in redis_status:
                await self.owner.redis.set_step_status(job.id, name, status)
            if name not in db_steps:
                await asyncio.to_thread(
                    self.owner.db.upsert_step,
                    Step(
                        job_id=job.id,
                        name=name,
                        status=StepStatus(status),
                        pool=cfg["pool"],
                    ),
                )

        await self.owner._export_term_map(job)

        await self.owner.redis.add_active_job(job.id)
        await self.owner._check_downstream(job.id)

        logger.info("job_submitted", job_id=job.id, pipeline=job.pipeline)

    async def _exec_is_current(
        self, job_id: str, step: str, exec_id: str | None,
        generation: int | None,
    ) -> bool:
        """存在当前执行身份时 fail closed;旧库无身份记录才兼容无 envelope 事件。"""
        current = await self.owner.redis.get_step_exec_id(job_id, step)
        if current is not None and current != exec_id:
            return False
        current_generation = await self.owner.redis.get_step_generation(job_id, step)
        if current_generation is not None and current_generation != generation:
            return False
        if generation is not None:
            if await self.owner.redis.get_job_generation(job_id) != generation:
                return False
            if await self.owner.redis.job_generation_is_terminal(job_id, generation):
                return False
        return True

    async def on_step_started(
        self, job_id: str, step: str, worker: str | None = None,
    ) -> None:
        # 把"运行中"落 DB,让 REST(/api/jobs)也能显示 running,不只 WebSocket。
        # 仅当 Redis 仍为 running 时写:避免快步骤的 step_completed 先到、迟到的
        # step_started 把已完成步骤倒回 running(两条不同频道,跨频道顺序无保证)。
        if await self.owner.redis.get_step_status(job_id, step) != "running":
            return
        await asyncio.to_thread(
            self.owner.db.update_step, job_id, step,
            status="running", worker_id=worker, started_at=datetime.now(timezone.utc),
        )

    async def on_step_done(
        self,
        job_id: str,
        step: str,
        duration: float | None = None,
        worker: str | None = None,
        exec_id: str | None = None,
        generation: int | None = None,
        started_at: float | None = None,
    ) -> None:
        # 丢弃陈旧执行的完成事件:孤儿重排后旧 worker 迟到上报,其 exec_id 不再是当前在跑的实例.
        # 忽略旧上报,避免提前置 done 顶替仍在跑的新执行(双执行/读到不完整产物).
        if not await self.owner._exec_is_current(job_id, step, exec_id, generation):
            logger.warning("stale_exec_done_ignored", job_id=job_id, step=step, exec_id=exec_id)
            return
        ok = await self.owner.redis.cas_step_status(job_id, step, "running", "done")
        if not ok:
            return

        await asyncio.to_thread(
            self.owner.db.update_step, job_id, step,
            status="done",
            worker_id=worker,
            started_at=(
                datetime.fromtimestamp(started_at, timezone.utc)
                if isinstance(started_at, (int, float)) else None
            ),
            finished_at=datetime.now(timezone.utc),
            duration_sec=duration,
        )
        await self.owner._record_worker_terminal_stats(
            worker, completed=1, duration=float(duration or 0),
        )

        progress = await self.owner._update_progress(job_id)
        await self.owner.redis.publish(f"events:{job_id}", {
            "event": "step_done", "step": step,
            "duration_sec": duration, "progress_pct": progress,
        })

        # 完成副作用来自 pipeline step 的 on_complete 声明。失败不回滚已完成步骤;
        # job 终态门与周期对账会幂等重放,直到全部副作用成功。
        await self.owner._run_step_completion_effects(job_id, step)

        logger.info("step_done", job_id=job_id, step=step, duration=duration)
        await self.owner._check_downstream(job_id)

    async def on_step_failed(
        self,
        job_id: str,
        step: str,
        error: str,
        error_type: str = "unknown",
        worker: str | None = None,
        exec_id: str | None = None,
        generation: int | None = None,
        duration: float | None = None,
        started_at: float | None = None,
        count_stats: bool = False,
    ) -> None:
        # 同 on_step_done:丢弃陈旧执行的失败事件,不让旧实例顶替当前在跑的步骤。
        if not await self.owner._exec_is_current(job_id, step, exec_id, generation):
            logger.warning("stale_exec_failed_ignored", job_id=job_id, step=step, exec_id=exec_id)
            return
        ok = await self.owner.redis.cas_step_status(job_id, step, "running", "failed")
        if not ok:
            return

        await asyncio.to_thread(
            self.owner.db.update_step,
            job_id,
            step,
            status="failed",
            error=error[:500],
            worker_id=worker,
            started_at=(
                datetime.fromtimestamp(started_at, timezone.utc)
                if isinstance(started_at, (int, float)) else None
            ),
            finished_at=datetime.now(timezone.utc),
            duration_sec=duration,
        )
        if count_stats:
            await self.owner._record_worker_terminal_stats(worker, failed=1)

        logger.warning(
            "step_failed", job_id=job_id, step=step,
            error_type=error_type, error=error[:200],
        )

        pipeline_steps = await self.owner._get_job_pipeline_steps(job_id)
        if not pipeline_steps:
            return
        cfg = pipeline_steps.get(step, {})
        pipeline_retries = cfg.get("retries", 0)

        # 缺表项(如 unknown)按 max 0 处理:未归类失败默认 BUILD,不重试。
        # pipeline_retries 二次封顶 policy_max:用户不可放大 SYSTEM 类的上限。
        policy = RETRY_POLICY.get(error_type, {})
        policy_max = policy.get("max", 0)
        max_retries = min(policy_max, pipeline_retries)

        current_retries = await self.owner.redis.get_step_retries(job_id, step)

        if current_retries < max_retries:
            await self.owner.redis.incr_step_retries(job_id, step)
            # 同步 DB retries 列:重试计数权威在 redis(job:{id}:retries),但 DB 只在终态才写,
            # 重试中 UI/排查看到 retries=0 会误判"超时不计数、无限循环"(线上 GPT-3 翻译步实证误读)。
            await asyncio.to_thread(
                self.owner.db.update_step, job_id, step, retries=current_retries + 1,
            )
            delay = get_retry_delay(error_type, current_retries) or 0
            logger.info(
                "step_retry", job_id=job_id, step=step,
                attempt=current_retries + 1, max=max_retries, delay=delay,
            )
            # enqueue_step will set status to "ready" (from current "failed")
            if delay > 0:
                task = asyncio.create_task(
                    self.owner._delayed_enqueue(delay, job_id, step),
                    name=f"{_DELAYED_PREFIX}{job_id}:{step}",
                )
                self.owner._delayed_tasks.add(task)
                task.add_done_callback(self.owner._on_delayed_done)
            else:
                await self.owner.enqueue_step(job_id, step)

            await self.owner.redis.publish(f"events:{job_id}", {
                "event": "step_failed", "step": step,
                "error": error[:200], "retries": current_retries + 1,
            })
        else:
            # CAS already set it to "failed", just update DB
            await asyncio.to_thread(
                self.owner.db.update_step, job_id, step,
                status="failed", error=error[:500],
                finished_at=datetime.now(timezone.utc),
                retries=current_retries,
            )
            await self.owner.mark_job_failed(job_id, f"{step}: {error[:200]}")

    async def _delayed_enqueue(self, delay: int, job_id: str, step: str) -> None:
        await asyncio.sleep(delay)
        await self.owner.enqueue_step(job_id, step)

    async def _record_worker_terminal_stats(
        self,
        worker_id: str | None,
        *,
        completed: int = 0,
        failed: int = 0,
        duration: float = 0.0,
    ) -> None:
        if not worker_id:
            return
        await asyncio.to_thread(
            self.owner.db.increment_worker_stats,
            worker_id,
            completed=completed,
            failed=failed,
            duration=duration,
        )
        if completed:
            await self.owner.redis.incr_worker_stat(worker_id, "tasks_completed", completed)
        if failed:
            await self.owner.redis.incr_worker_stat(worker_id, "tasks_failed", failed)
        if duration:
            await self.owner.redis.incr_worker_stat(worker_id, "total_duration_sec", duration)
