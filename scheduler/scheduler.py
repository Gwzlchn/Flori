"""调度器：监听步骤完成/失败事件，推进 DAG，管理 Job 生命周期。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import structlog

from shared.config import AppConfig, load_config
from shared.db import Database
from shared.errors import RETRY_POLICY, get_retry_delay
from shared.models import Job, JobStatus, Step, StepStatus
from shared.redis_client import RedisClient

logger = structlog.get_logger(component="scheduler")

# 延迟重试任务的 name 前缀，跟踪/按 job 取消时复用，避免格式漂移。
_DELAYED_PREFIX = "delayed_enqueue:"


class Scheduler:
    def __init__(self, redis: RedisClient, db: Database, config: AppConfig):
        self.redis = redis
        self.db = db
        self.config = config
        self.jobs_dir = config.jobs_dir
        self._shutdown = False
        self._pubsub_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None
        # 跟踪所有 _delayed_enqueue fire-and-forget 任务，供 shutdown / rerun /
        # job 失败时取消，避免泄漏或旧重试与新状态串台。
        self._delayed_tasks: set[asyncio.Task] = set()

    # ── 生命周期 ──

    async def run(self) -> None:
        logger.info("scheduler_start")
        await self._recover()
        self._pubsub_task = asyncio.create_task(self._event_loop())
        self._periodic_task = asyncio.create_task(self._periodic_loop())
        try:
            await asyncio.gather(
                self._pubsub_task,
                self._periodic_task,
            )
        except asyncio.CancelledError:
            logger.info("scheduler_cancelled")

    async def shutdown(self) -> None:
        logger.info("scheduler_shutdown")
        self._shutdown = True
        if self._pubsub_task and not self._pubsub_task.done():
            self._pubsub_task.cancel()
        if self._periodic_task and not self._periodic_task.done():
            self._periodic_task.cancel()
        pending = [t for t in self._delayed_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _on_delayed_done(self, task: asyncio.Task) -> None:
        """延迟任务完成回调：从跟踪集合移除；非取消的真异常上报。"""
        self._delayed_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "delayed_enqueue_failed",
                task=task.get_name(), exc_info=task.exception(),
            )

    def _cancel_delayed_tasks(self, job_id: str) -> None:
        """取消某 job 在途的延迟重试任务（rerun / job 失败时调用）。"""
        prefix = f"{_DELAYED_PREFIX}{job_id}:"
        for t in list(self._delayed_tasks):
            if t.get_name().startswith(prefix) and not t.done():
                t.cancel()

    # ── 主循环 ──

    async def _event_loop(self) -> None:
        """订阅事件并分发。连接级异常（redis 超时/断连）不再让进程崩溃，
        而是指数退避后重连重订阅；启动恢复也会补推漏掉的步骤。"""
        backoff = 1
        while not self._shutdown:
            try:
                async for msg in self.redis.subscribe(
                    "step_completed", "step_failed", "job_command",
                ):
                    if self._shutdown:
                        break
                    backoff = 1  # 收到任何消息说明连接健康，重置退避
                    try:
                        await self._dispatch(msg)
                    except Exception:
                        logger.exception("event_handler_error", msg=msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._shutdown:
                    break
                logger.exception("event_loop_reconnect", backoff=backoff)
                # 重连前先尝试重建底层连接 + 补推可能漏掉的事件
                try:
                    await self.redis.reconnect()
                    await self._recover()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("event_loop_recover_failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _dispatch(self, msg: dict) -> None:
        status = msg.get("status")
        command = msg.get("command") or msg.get("action")

        if status == "done":
            await self.on_step_done(
                msg["job_id"], msg["step"],
                duration=msg.get("duration"),
                worker=msg.get("worker"),
                exec_id=msg.get("exec_id"),
            )
        elif status == "failed":
            await self.on_step_failed(
                msg["job_id"], msg["step"],
                msg.get("error", ""),
                msg.get("error_type", "unknown"),
            )
        elif command == "new_job":
            job = await asyncio.to_thread(self.db.get_job, msg["job_id"])
            if job:
                await self.submit_job(job)
        elif command == "rerun":
            await self.rerun(msg["job_id"], msg["from_step"])
        elif command == "resubmit":
            await self.resubmit(msg["job_id"])
        elif command == "retry":
            await self._retry_failed(msg["job_id"])

    async def _periodic_loop(self) -> None:
        while not self._shutdown:
            try:
                await self.orphan_scan()
                await self.check_stuck()
                await self.cleanup_stale_workers()
            except Exception:
                logger.exception("periodic_error")
            await asyncio.sleep(30)

    async def cleanup_stale_workers(self, timeout_sec: int = 60) -> None:
        """清理僵尸 worker：DB 中 last_heartbeat 超时且 Redis 注册已过期（worker 真没了）
        的记录删除；仅 DB 过期但 Redis 仍在的标 offline（容器可能刚重启换 id）。"""
        from datetime import timedelta

        workers = await asyncio.to_thread(self.db.list_workers)
        now = datetime.now()
        for w in workers:
            hb = w.last_heartbeat
            stale = hb is None or (now - hb) > timedelta(seconds=timeout_sec)
            if not stale:
                continue
            alive = await self.redis.worker_exists(w.id)
            if alive:
                # 注意：list_workers 已把超时的 w.status 衍生成 "offline"，
                # 故此处直接持久化（幂等），不能用 w.status 判断是否需要写。
                await asyncio.to_thread(
                    self.db.set_worker_status, w.id, "offline",
                )
            else:
                await asyncio.to_thread(self.db.delete_worker, w.id)
                logger.info("worker_cleaned", worker_id=w.id)

    async def _recover(self) -> None:
        """启动恢复：补推满足依赖的步骤，回收无主 running 步骤。"""
        active_jobs = await self.redis.get_active_jobs()
        logger.info("recover_start", active_jobs=len(active_jobs))

        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            if not statuses:
                await self.redis.remove_active_job(job_id)
                continue

            for step, status in statuses.items():
                if status == "running":
                    worker_id = await self.redis.get_step_worker(job_id, step)
                    if not worker_id or not await self.redis.worker_exists(worker_id):
                        await self._reclaim_step(
                            job_id, step, f"recover: worker {worker_id or 'none'} lost"
                        )

            await self._check_downstream(job_id)

        logger.info("recover_done", active_jobs=len(active_jobs))

    # ── Job 提交 ──

    async def submit_job(self, job: Job) -> None:
        """API 调用：提交新任务，初始化步骤状态，入队无依赖步骤。"""
        pipeline_steps = self._get_pipeline_steps(job.pipeline)
        if not pipeline_steps:
            logger.warning("empty_pipeline", job_id=job.id, pipeline=job.pipeline)
            await asyncio.to_thread(
                self.db.update_job, job.id,
                status=JobStatus.FAILED, error=f"unknown pipeline: {job.pipeline}",
            )
            return

        await self.redis.init_job(job.id, job.pipeline, {
            "domain": job.domain,
            "style_tags": job.style_tags,
        })

        for name, cfg in pipeline_steps.items():
            await self.redis.set_step_status(job.id, name, "waiting")
            await asyncio.to_thread(
                self.db.upsert_step,
                Step(job_id=job.id, name=name, status=StepStatus.WAITING, pool=cfg["pool"]),
            )

        await self.redis.add_active_job(job.id)
        await self._check_downstream(job.id)

        logger.info("job_submitted", job_id=job.id, pipeline=job.pipeline)

    # ── 事件处理 ──

    async def on_step_done(
        self,
        job_id: str,
        step: str,
        duration: float | None = None,
        worker: str | None = None,
        exec_id: str | None = None,
    ) -> None:
        ok = await self.redis.cas_step_status(job_id, step, "running", "done")
        if not ok:
            return

        await asyncio.to_thread(
            self.db.update_step, job_id, step,
            status="done",
            worker_id=worker,
            finished_at=datetime.now(),
            duration_sec=duration,
        )

        progress = await self._update_progress(job_id)
        await self.redis.publish(f"events:{job_id}", {
            "event": "step_done", "step": step,
            "duration_sec": duration, "progress_pct": progress,
        })

        logger.info("step_done", job_id=job_id, step=step, duration=duration)
        await self._check_downstream(job_id)

    async def on_step_failed(
        self,
        job_id: str,
        step: str,
        error: str,
        error_type: str = "unknown",
    ) -> None:
        ok = await self.redis.cas_step_status(job_id, step, "running", "failed")
        if not ok:
            return

        logger.warning(
            "step_failed", job_id=job_id, step=step,
            error_type=error_type, error=error[:200],
        )

        pipeline_steps = await self._get_job_pipeline_steps(job_id)
        if not pipeline_steps:
            return
        cfg = pipeline_steps.get(step, {})
        pipeline_retries = cfg.get("retries", 0)

        policy = RETRY_POLICY.get(error_type, {})
        policy_max = policy.get("max", 0)
        max_retries = min(policy_max, pipeline_retries)

        current_retries = await self.redis.get_step_retries(job_id, step)

        if current_retries < max_retries:
            await self.redis.incr_step_retries(job_id, step)
            delay = get_retry_delay(error_type, current_retries) or 0
            logger.info(
                "step_retry", job_id=job_id, step=step,
                attempt=current_retries + 1, max=max_retries, delay=delay,
            )
            # enqueue_step will set status to "ready" (from current "failed")
            if delay > 0:
                task = asyncio.create_task(
                    self._delayed_enqueue(delay, job_id, step),
                    name=f"{_DELAYED_PREFIX}{job_id}:{step}",
                )
                self._delayed_tasks.add(task)
                task.add_done_callback(self._on_delayed_done)
            else:
                await self.enqueue_step(job_id, step)

            await self.redis.publish(f"events:{job_id}", {
                "event": "step_failed", "step": step,
                "error": error[:200], "retries": current_retries + 1,
            })
        else:
            # CAS already set it to "failed", just update DB
            await asyncio.to_thread(
                self.db.update_step, job_id, step,
                status="failed", error=error[:500],
                finished_at=datetime.now(),
                retries=current_retries,
            )
            await self.mark_job_failed(job_id, f"{step}: {error[:200]}")

    async def _delayed_enqueue(self, delay: int, job_id: str, step: str) -> None:
        await asyncio.sleep(delay)
        await self.enqueue_step(job_id, step)

    # ── DAG 推进 ──

    async def _check_downstream(self, job_id: str) -> None:
        """检查所有 waiting/skipped 步骤是否可推进。on_step_done 和 mark_skipped 共用。"""
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        steps = self._get_pipeline_steps(pipeline)
        statuses = await self.redis.get_all_step_statuses(job_id)

        for name, cfg in steps.items():
            status = statuses.get(name)
            if status not in ("waiting", "skipped"):
                continue

            deps = cfg.get("depends_on", [])
            if not all(statuses.get(d) in ("done", "skipped") for d in deps):
                continue

            condition = cfg.get("condition")
            if condition and not await self.check_condition(job_id, condition):
                if status == "waiting":
                    await self.redis.set_step_status(job_id, name, "skipped")
                    await asyncio.to_thread(
                        self.db.update_step, job_id, name, status="skipped",
                    )
                    await self.redis.publish(f"events:{job_id}", {
                        "event": "step_skipped", "step": name,
                    })
                    statuses[name] = "skipped"
                continue

            if status == "skipped":
                if not condition:
                    continue
                ok = await self.redis.cas_step_status(job_id, name, "skipped", "ready")
                if not ok:
                    continue
            await self.enqueue_step(job_id, name)
            statuses[name] = "ready"

        fresh = await self.redis.get_all_step_statuses(job_id)
        if fresh and all(v in ("done", "skipped") for v in fresh.values()):
            await self.mark_job_done(job_id)
        elif fresh:
            # 死锁打破器：仅当剩余未完成步骤全部为 ready（无 running、无 waiting）才介入。
            not_done = {k: v for k, v in fresh.items() if v not in ("done", "skipped")}
            all_remaining_ready = bool(not_done) and all(
                v == "ready" for v in not_done.values()
            )
            if all_remaining_ready:
                pipeline = await self.redis.get_job_pipeline(job_id)
                if pipeline:
                    steps_cfg = self._get_pipeline_steps(pipeline)
                    for step_name in not_done:
                        pool = steps_cfg.get(step_name, {}).get("pool", "")
                        if await self._pool_has_workers(pool):
                            continue
                        # CAS 保护 ready→skipped：若该步骤刚被 worker 抢成 running，
                        # CAS 失败 → 放弃 skip，避免覆盖在途执行。
                        if not await self.redis.cas_step_status(
                            job_id, step_name, "ready", "skipped"
                        ):
                            continue
                        logger.info(
                            "skip_no_worker", job_id=job_id,
                            step=step_name, pool=pool,
                        )
                        await asyncio.to_thread(
                            self.db.update_step, job_id, step_name, status="skipped",
                        )
                        await self.redis.publish(f"events:{job_id}", {
                            "event": "step_skipped", "step": step_name,
                            "reason": f"no workers in pool '{pool}'",
                        })
                    fresh2 = await self.redis.get_all_step_statuses(job_id)
                    if fresh2 and all(v in ("done", "skipped") for v in fresh2.values()):
                        await self.mark_job_done(job_id)

    async def enqueue_step(self, job_id: str, step_name: str) -> None:
        pipeline_steps = await self._get_job_pipeline_steps(job_id)
        if not pipeline_steps:
            return
        step_cfg = pipeline_steps.get(step_name)
        if not step_cfg:
            return

        await self.redis.set_step_status(job_id, step_name, "ready")
        pool = step_cfg["pool"]

        static_tags = step_cfg.get("tags", [])
        if pool == "ai":
            job_info = await self.redis.get_job_info(job_id)
            domain = job_info.get("domain", "")
            style_tags_raw = job_info.get("style_tags", "[]")
            try:
                style_tags = json.loads(style_tags_raw) if isinstance(style_tags_raw, str) else style_tags_raw
            except (json.JSONDecodeError, TypeError):
                style_tags = []
            dynamic_tags = [domain] + (style_tags if isinstance(style_tags, list) else [])
            merged_tags = sorted(set(static_tags + [t for t in dynamic_tags if t]))
        else:
            merged_tags = list(static_tags)

        statuses = await self.redis.get_all_step_statuses(job_id)
        done_count = sum(1 for v in statuses.values() if v in ("done", "skipped"))
        priority = -done_count

        await self.redis.enqueue_step(
            pool, job_id, step_name, merged_tags, priority,
            require_tags=list(static_tags),
        )

        await asyncio.to_thread(
            self.db.update_step, job_id, step_name, status="ready",
        )
        await self.redis.publish(f"events:{job_id}", {
            "event": "step_ready", "step": step_name,
        })

        logger.info("step_enqueued", job_id=job_id, step=step_name, pool=pool, priority=priority)

    async def check_condition(self, job_id: str, condition: str) -> bool:
        job_dir = self.jobs_dir / job_id
        input_dir = job_dir / "input"

        def _check() -> bool:
            if condition == "no_subtitle":
                return not list(input_dir.glob("*.srt")) if input_dir.exists() else True
            if condition == "has_subtitle":
                return bool(list(input_dir.glob("*.srt"))) if input_dir.exists() else False
            if condition == "has_danmaku":
                return bool(list(input_dir.glob("*.ass"))) if input_dir.exists() else False
            return True

        return await asyncio.to_thread(_check)

    async def mark_skipped(self, job_id: str, step: str) -> None:
        await self.redis.set_step_status(job_id, step, "skipped")
        await asyncio.to_thread(self.db.update_step, job_id, step, status="skipped")
        await self.redis.publish(f"events:{job_id}", {
            "event": "step_skipped", "step": step,
        })
        await self._check_downstream(job_id)

    async def _pool_has_workers(self, pool: str) -> bool:
        """检查某个 pool 是否有在线 worker。"""
        workers = await self.redis.list_worker_ids()
        for wid in workers:
            info = await self.redis.get_worker_info(wid)
            if info and pool in info.get("pools", "").split(","):
                return True
        return False

    # ── Job 状态 ──

    async def mark_job_done(self, job_id: str) -> None:
        await asyncio.to_thread(
            self.db.update_job, job_id,
            status=JobStatus.DONE, progress_pct=100,
        )
        await self.redis.publish(f"events:{job_id}", {
            "event": "job_done", "progress_pct": 100,
        })
        await self.redis.remove_active_job(job_id)
        logger.info("job_done", job_id=job_id)

    async def mark_job_failed(self, job_id: str, error: str) -> None:
        self._cancel_delayed_tasks(job_id)
        progress = await self._update_progress(job_id)
        await asyncio.to_thread(
            self.db.update_job, job_id,
            status=JobStatus.FAILED, error=error[:500],
        )
        await self.redis.publish(f"events:{job_id}", {
            "event": "job_failed", "error": error[:200], "progress_pct": progress,
        })
        await self.redis.remove_active_job(job_id)
        logger.info("job_failed", job_id=job_id, error=error[:200])

    # ── 孤儿回收 + 卡住检测 ──

    async def orphan_scan(self) -> None:
        active_jobs = await self.redis.get_active_jobs()
        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            for step, status in statuses.items():
                if status != "running":
                    continue
                worker_id = await self.redis.get_step_worker(job_id, step)
                if not worker_id:
                    await self._reclaim_step(job_id, step, "no worker assigned")
                elif not await self.redis.worker_exists(worker_id):
                    await self._reclaim_step(job_id, step, f"worker {worker_id} lost")

    async def _reclaim_step(self, job_id: str, step: str, reason: str) -> None:
        logger.warning("reclaim_step", job_id=job_id, step=step, reason=reason)

        pipeline_steps = await self._get_job_pipeline_steps(job_id)
        if pipeline_steps:
            pool = pipeline_steps.get(step, {}).get("pool")
            if pool:
                await self.redis.release_slot(pool)
                if pool == "scene":
                    await self.redis.unfreeze_pool("cpu")

        await self.redis.publish("step_failed", {
            "job_id": job_id, "step": step, "status": "failed",
            "error": f"orphan reclaimed: {reason}",
            "error_type": "processing",
        })

    async def check_stuck(self) -> None:
        active_jobs = await self.redis.get_active_jobs()
        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            for step, status in statuses.items():
                if status != "running":
                    continue
                progress_file = self.jobs_dir / job_id / f".{step}.progress"
                if not progress_file.exists():
                    continue
                try:
                    raw = await asyncio.to_thread(progress_file.read_text)
                    data = json.loads(raw)
                except (json.JSONDecodeError, OSError):
                    continue

                step_updated = data.get("updated_at")
                worker_hb = data.get("worker_heartbeat_at")
                latest = max(filter(None, [step_updated, worker_hb]), default=None)
                if latest is None:
                    continue
                age = time.time() - latest
                if age > 60:
                    logger.warning(
                        "step_stuck", job_id=job_id, step=step, age_sec=round(age),
                    )
                    await self.redis.publish("step_failed", {
                        "job_id": job_id, "step": step, "status": "failed",
                        "error": f"progress stale ({age:.0f}s, worker process may be stuck)",
                        "error_type": "timeout",
                    })

    # ── 重跑 / 重提交 ──

    async def _retry_failed(self, job_id: str) -> None:
        """重试失败 Job：从第一个 failed 步骤开始重跑。"""
        statuses = await self.redis.get_all_step_statuses(job_id)
        failed_steps = [s for s, st in statuses.items() if st == "failed"]
        if not failed_steps:
            return
        first_failed = sorted(failed_steps)[0]
        await self.rerun(job_id, first_failed)
        logger.info("job_retry", job_id=job_id, from_step=first_failed)

    async def rerun(self, job_id: str, from_step: str) -> list[str]:
        """从指定步骤开始重跑，清除该步骤及所有下游的 .done 标记。返回被重置的步骤列表。"""
        self._cancel_delayed_tasks(job_id)  # 取消旧重试，防与新一轮状态串台
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return []
        steps = self._get_pipeline_steps(pipeline)
        downstream = self._get_downstream(steps, from_step)
        reset_steps = [from_step] + downstream

        for step in reset_steps:
            done_file = self.jobs_dir / job_id / f".{step}.done"
            await asyncio.to_thread(done_file.unlink, True)
            await self.redis.set_step_status(job_id, step, "waiting")
            await asyncio.to_thread(
                self.db.update_step, job_id, step,
                status="waiting", error=None,
            )

        await asyncio.to_thread(
            self.db.update_job, job_id, status=JobStatus.PROCESSING,
        )
        await self.redis.add_active_job(job_id)
        await self._check_downstream(job_id)

        logger.info("job_rerun", job_id=job_id, from_step=from_step, reset=reset_steps)
        return reset_steps

    async def resubmit(self, job_id: str) -> None:
        """按当前 pipelines.yaml 重新初始化步骤，保留已完成步骤状态。"""
        self.reload_config()

        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        steps = self._get_pipeline_steps(pipeline)
        existing = await self.redis.get_all_step_statuses(job_id)

        for name in existing:
            if name not in steps:
                await self.redis.delete_step_status(job_id, name)

        for name, cfg in steps.items():
            if name not in existing:
                await self.redis.set_step_status(job_id, name, "waiting")
                await asyncio.to_thread(
                    self.db.upsert_step,
                    Step(job_id=job_id, name=name, status=StepStatus.WAITING, pool=cfg["pool"]),
                )

        await asyncio.to_thread(
            self.db.update_job, job_id, status=JobStatus.PROCESSING,
        )
        await self.redis.add_active_job(job_id)
        await self._check_downstream(job_id)

        logger.info("job_resubmit", job_id=job_id, pipeline=pipeline)

    def reload_config(self) -> None:
        self.config = load_config(
            config_dir=self.config.config_dir,
            data_dir=self.config.data_dir,
        )
        logger.info("config_reloaded")

    # ── 内部工具 ──

    def _get_pipeline_steps(self, pipeline: str) -> dict[str, dict]:
        steps_list = self.config.pipelines.get(pipeline, {}).get("steps", [])
        return {s["name"]: s for s in steps_list}

    async def _get_job_pipeline_steps(self, job_id: str) -> dict[str, dict] | None:
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return None
        return self._get_pipeline_steps(pipeline)

    def _get_downstream(self, steps: dict[str, dict], from_step: str) -> list[str]:
        """递归获取 from_step 的所有下游步骤。"""
        dependents: dict[str, list[str]] = {}
        for name, cfg in steps.items():
            for dep in cfg.get("depends_on", []):
                dependents.setdefault(dep, []).append(name)

        result = []
        q = deque(dependents.get(from_step, []))
        visited = set()
        while q:
            s = q.popleft()
            if s in visited:
                continue
            visited.add(s)
            result.append(s)
            q.extend(dependents.get(s, []))
        return result

    def _calc_progress(self, steps_config: list[dict], statuses: dict[str, str]) -> int:
        done_weight = sum(
            s.get("weight", 1) for s in steps_config
            if statuses.get(s["name"]) in ("done", "skipped")
        )
        total_weight = sum(s.get("weight", 1) for s in steps_config)
        return round(100 * done_weight / max(total_weight, 1))

    async def _update_progress(self, job_id: str) -> int:
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return 0
        steps_config = self.config.pipelines.get(pipeline, {}).get("steps", [])
        statuses = await self.redis.get_all_step_statuses(job_id)
        progress = self._calc_progress(steps_config, statuses)
        await asyncio.to_thread(
            self.db.update_job, job_id, progress_pct=progress,
        )
        return progress
