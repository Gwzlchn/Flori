"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
import time
from datetime import datetime, timezone

import structlog

from shared.ai_routing import InvalidAIOverrideError
from shared.models import JobStatus, Step, StepStatus
from shared.notify import notify
from shared.study_suggestions import StudySuggestionConflictError

if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

class RecoveryCoordinator:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def _recover(self) -> None:
        """启动恢复:补推满足依赖的步骤,回收无主 running 步骤。"""
        await self.owner.reconcile_study_suggestion_batches()
        await self.owner._recover_pending_jobs()
        active_jobs = await self.owner.redis.get_active_jobs()
        logger.info("recover_start", active_jobs=len(active_jobs))

        for job_id in active_jobs:
            statuses = await self.owner.redis.get_all_step_statuses(job_id)
            if not statuses:
                await self.owner.redis.remove_active_job(job_id)
                continue

            # §2.7 启动顺序:先按 manifest 修复投影/重放副作用,再恢复入队。
            try:
                await self.owner.reconcile_step_manifests(job_id)
            except Exception:
                logger.warning(
                    "manifest_reconcile_failed", job_id=job_id, exc_info=True,
                )
            statuses = await self.owner.redis.get_all_step_statuses(job_id)

            for step, status in statuses.items():
                if status == "running":
                    worker_id = await self.owner.redis.get_step_worker(job_id, step)
                    if not worker_id or not await self.owner.redis.worker_exists(worker_id):
                        await self.owner._reclaim_step(
                            job_id, step, f"recover: worker {worker_id or 'none'} lost"
                        )
                elif status == "ready":
                    # ready-but-not-queued 孤儿补投:调度器/redis 在置 ready 后,入队前重启,或队列
                    # 消息丢失时,步永远停在 ready 无人认领(线上:部署窗口把 03_scene 卡死)。
                    # enqueue 的 ZADD 同成员幂等,task json 相同会去重,已在队的重复补投无害.
                    if await self.owner.enqueue_step(job_id, step):
                        logger.info("recover_requeue_ready", job_id=job_id, step=step)

            await self.owner._check_downstream(job_id)

        logger.info("recover_done", active_jobs=len(active_jobs))

    async def _recover_pending_jobs(self) -> None:
        """补偿 Scheduler 离线时落库但未消费 new_job 的普通 pending job。"""
        _, pending = await asyncio.to_thread(
            self.owner.db.list_jobs,
            status="pending",
            limit=10_000,
            current_only=False,
        )
        book_collections: set[str] = set()
        for job in pending:
            if job.collection_id:
                collection = await asyncio.to_thread(
                    self.owner.db.get_collection, job.collection_id,
                )
                if collection and getattr(collection, "source_type", None) == "book_toc":
                    book_collections.add(job.collection_id)
                    continue
            await self.owner.submit_job(job)

        if book_collections:
            from shared.book_chain import next_chapter_job
            by_id = {job.id: job for job in pending}
            for collection_id in sorted(book_collections):
                next_id = await next_chapter_job(self.owner.db, self.owner.redis, collection_id)
                if next_id and next_id in by_id:
                    await self.owner.submit_job(by_id[next_id])

    async def _fail_study_suggestion_batch(
        self, batch: dict, code: str, message: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.owner.db.fail_study_suggestion_batch,
                str(batch["batch_id"]),
                task_id=str(batch["task_id"]),
                expected_revision=int(batch["revision"]),
                error_code=code,
                error_message=(message or code)[:2_000],
            )
        except StudySuggestionConflictError:
            # 另一 Scheduler 副本已推进同一 CAS,当前拍按幂等竞争结束。
            return

    async def reconcile_study_suggestion_batches(
        self, *, now: datetime | None = None,
    ) -> int:
        """以 DB 为真相幂等投递并收割学习建议 AI task."""
        current_time = now or datetime.now(timezone.utc)
        await self.owner.redis.reconcile_ai_task_claims(
            now_epoch=current_time.timestamp(),
        )
        batches = await asyncio.to_thread(
            self.owner.db.list_study_suggestion_batches_for_reconcile,
        )
        progressed = 0
        for snapshot in batches:
            batch = snapshot
            try:
                if batch["status"] == "pending_enqueue":
                    batch = await asyncio.to_thread(
                        self.owner.db.mark_study_suggestion_batch_queued,
                        str(batch["batch_id"]),
                        task_id=str(batch["task_id"]),
                        expected_revision=int(batch["revision"]),
                    )
                    progressed += 1
                payload = self.owner._study_suggestion_ai_payload(batch)
                await self.owner.redis.enqueue_ai_task_once(payload, priority=-10)

                claim = await self.owner.redis.get_ai_task_claim(str(batch["task_id"]))
                claim_state = claim.get("state") if claim else None
                if claim_state == "ambiguous":
                    await self.owner._fail_study_suggestion_batch(
                        batch,
                        "ai_task_ambiguous",
                        "AI task execution expired after provider start; manual retry required",
                    )
                    progressed += 1
                    continue

                result = await self.owner.redis.get_ai_result(str(batch["task_id"]))
                if result is None:
                    log = await asyncio.to_thread(
                        self.owner.db.get_latest_ai_task_log, str(batch["task_id"]),
                    )
                    if log is not None:
                        record = log["record"]
                        result = (
                            {"content": record.get("output")}
                            if bool(log.get("ok"))
                            else {"error": log.get("error") or record.get("error")}
                        )

                # Worker 先写结果/审计再 CAS 终态.有 claim 时只消费终态,隔离迟到旧执行。
                if result is not None and (
                    claim is None or claim_state in {"succeeded", "failed"}
                ):
                    if not isinstance(result, dict):
                        await self.owner._fail_study_suggestion_batch(
                            batch, "ai_task_result_invalid", "AI task result is not an object",
                        )
                    elif result.get("error"):
                        await self.owner._fail_study_suggestion_batch(
                            batch, "ai_task_failed", str(result["error"]),
                        )
                    else:
                        content = result.get("content")
                        try:
                            parsed = json.loads(content) if isinstance(content, str) else content
                            if not isinstance(parsed, dict):
                                raise ValueError("AI task content is not an object")
                            await asyncio.to_thread(
                                self.owner.db.materialize_study_suggestions,
                                str(batch["batch_id"]),
                                task_id=str(batch["task_id"]),
                                result=parsed,
                            )
                        except (json.JSONDecodeError, ValueError) as exc:
                            await self.owner._fail_study_suggestion_batch(
                                batch, "ai_task_result_invalid", str(exc),
                            )
                    progressed += 1
                    continue

                deadline = datetime.fromisoformat(str(batch["deadline_at"]))
                if deadline.tzinfo is None or deadline.utcoffset() is None:
                    raise ValueError("study suggestion deadline has no timezone")
                if current_time >= deadline.astimezone(timezone.utc):
                    if claim_state == "executing":
                        continue
                    if claim_state in {None, "claimed", "requeued", "canceled"}:
                        cancel_state = await self.owner.redis.cancel_ai_task_before_execution(
                            payload,
                        )
                        if cancel_state != "canceled":
                            continue
                        await self.owner._fail_study_suggestion_batch(
                            batch, "ai_task_timeout", "AI task exceeded persistent deadline",
                        )
                        progressed += 1
            except StudySuggestionConflictError:
                continue
            except Exception:
                logger.exception(
                    "study_suggestion_reconcile_error",
                    batch_id=batch.get("batch_id"), task_id=batch.get("task_id"),
                )
        return progressed

    async def orphan_scan(self) -> None:
        active_jobs = await self.owner.redis.get_active_jobs()
        live_mismatch: set[tuple[str, str]] = set()
        for job_id in active_jobs:
            statuses = await self.owner.redis.get_all_step_statuses(job_id)
            for step, status in statuses.items():
                if status != "running":
                    continue
                worker_id = await self.owner.redis.get_step_worker(job_id, step)
                if not worker_id:
                    await self.owner._reclaim_step(job_id, step, "no worker assigned")
                    continue
                if not await self.owner.redis.worker_exists(worker_id):
                    await self.owner._reclaim_step(job_id, step, f"worker {worker_id} lost")
                    continue
                # worker 存活,但这步没有近期进度心跳;可能是认领响应丢失或未真正运行,实际没人在跑.
                # 判活用每步独立的进度心跳(job:*:step_progress,worker on_tick 每 10s 刷一步),
                # 而非 worker 的单个 current_step;后者在 concurrency>1 时只能反映 N 个并发步中的 1 个,
                # 会把其余并发步全误判为 claim lost 反复回收(并发越高越严重,实测会致失败雪崩)。
                # 持续超宽限期(容忍认领后首拍心跳延迟)才回收。
                progress_at = await self.owner.redis.get_step_progress_at(job_id, step)
                if progress_at is not None and time.time() - progress_at < self.owner._STEP_PROGRESS_FRESH_SEC:
                    self.owner._claim_mismatch_since.pop((job_id, step), None)
                    continue
                key = (job_id, step)
                live_mismatch.add(key)
                first = self.owner._claim_mismatch_since.setdefault(key, time.time())
                if time.time() - first >= self.owner._CLAIM_MISMATCH_GRACE_SEC:
                    self.owner._claim_mismatch_since.pop(key, None)
                    await self.owner._reclaim_step(
                        job_id, step,
                        f"worker {worker_id} not running this step (claim lost?)",
                    )
        # 清理不再 mismatch 的计时,避免泄漏。
        for k in [k for k in self.owner._claim_mismatch_since if k not in live_mismatch]:
            self.owner._claim_mismatch_since.pop(k, None)

    async def _revoke_step_execution(
        self, job_id: str, step: str,
    ) -> tuple[str | None, int | None]:
        """统一撤销 task lease 与所有 holder;重复调用安全。"""
        holder = await self.owner.redis.get_step_exec_id(job_id, step)
        generation = await self.owner.redis.get_step_generation(job_id, step)
        if holder:
            await self.owner.redis.release_holders({holder})
            await self.owner.redis.revoke_task_lease(holder)
        await self.owner.redis.clear_step_resources(job_id, step)
        return holder, generation

    async def _reclaim_step(
        self, job_id: str, step: str, reason: str,
        error_type: str = "processing",
    ) -> None:
        logger.warning("reclaim_step", job_id=job_id, step=step, reason=reason)
        await self.owner.redis.push_event("orphan_reclaimed", job_id=job_id, step=step, reason=reason)

        holder, generation = await self.owner._revoke_step_execution(job_id, step)

        await self.owner.redis.append_lifecycle_event("step_failed", {
            "job_id": job_id, "step": step, "status": "failed",
            "error": f"orphan reclaimed: {reason}",
            "error_type": error_type,
            "exec_id": holder,
            "generation": generation,
        })

    async def reconcile_slots(self) -> None:
        """周期对账并发槽:持有 holder(=exec_id)但不属于任何 running 步的就是泄漏.
        常见原因是 worker 突死没 release_step,删 running job 漏放,占槽后死在写状态前.
        SCARD 是真实占用,但这些陈旧 holder 仍占名额,需要清掉收敛.
        宽限:仅连续两拍(2×30s)都陈旧才 SREM,避开"刚占槽、还没写 running 状态"的认领窗口被误清。"""
        try:
            held = await self.owner.redis.get_all_holders()
            if not held:
                self.owner._slot_reconcile_suspect = set()
                return
            # live = 当前所有 running 步的 exec_id(= 合法持有者)。
            live: set[str] = set()
            for job_id in await self.owner.redis.get_active_jobs():
                statuses = await self.owner.redis.get_all_step_statuses(job_id)
                for step, status in statuses.items():
                    if status == "running":
                        ex = await self.owner.redis.get_step_exec_id(job_id, step)
                        if ex:
                            live.add(ex)
            live |= await self.owner.redis.get_live_ai_claim_holders()
            suspects = held - live
            confirmed = suspects & self.owner._slot_reconcile_suspect   # 连续两拍都陈旧才按真泄漏处理
            if confirmed:
                n = await self.owner.redis.release_holders(confirmed)
                logger.info("slots_reconciled", removed=n, holders=sorted(confirmed)[:10])
            self.owner._slot_reconcile_suspect = suspects
        except Exception:
            logger.exception("reconcile_slots_error")

    async def check_stuck(self) -> None:
        # 进度停滞检测:本地 job 读 jobs_dir/.{step}.progress(worker _progress_monitor 写其
        # work_dir;单机 LocalStorage 下 work_dir==jobs_dir 才可见)。远程 job(Gateway/Remote
        # 存储,work_dir 是 worker 本地 tmp,不落调度器盘)退回读 redis 步进度心跳;由 worker
        # on_tick 每 10s(仅子进程存活时)经 set_step_progress_at 刷新。
        active_jobs = await self.owner.redis.get_active_jobs()
        for job_id in active_jobs:
            statuses = await self.owner.redis.get_all_step_statuses(job_id)
            for step, status in statuses.items():
                if status != "running":
                    continue
                progress_file = self.owner.jobs_dir / job_id / f".{step}.progress"
                if progress_file.exists():
                    try:
                        data = json.loads(await asyncio.to_thread(progress_file.read_text))
                    except (json.JSONDecodeError, OSError):
                        continue
                    latest = max(
                        filter(None, [data.get("updated_at"), data.get("worker_heartbeat_at")]),
                        default=None,
                    )
                else:
                    # 远程 job:退回 redis 步进度心跳(无文件且无心跳=刚起步/未上报,跳过)。
                    latest = await self.owner.redis.get_step_progress_at(job_id, step)
                if latest is None:
                    continue
                age = time.time() - latest
                # 180s:worker 心跳每 10s(best-effort 走 gateway),但 api recreate/网络抖动可断 1-2 分钟,
                # 60s 一次部署就误杀在跑的步(线上 04_translate 被 "stale 71s" 杀过);真卡死 180s 内回收仍可接受。
                if age > 180:
                    logger.warning(
                        "step_stuck", job_id=job_id, step=step, age_sec=round(age),
                    )
                    await self.owner.redis.push_event("step_stuck", job_id=job_id, step=step, stalled_sec=round(age))
                    # 主动告警(设了 ALERT_WEBHOOK_URL 才外发;best-effort,不阻塞调度循环)。
                    await asyncio.to_thread(
                        notify, "step_stuck",
                        f"job {job_id} 的 {step} 进度停滞 {age:.0f}s,worker 可能卡死,已触发重试",
                        job_id=job_id, step=step, age_sec=round(age),
                    )
                    await self.owner._reclaim_step(
                        job_id,
                        step,
                        f"progress stale ({age:.0f}s, worker process may be stuck)",
                        error_type="timeout",
                    )

    async def check_no_worker(self) -> None:
        """无法推进的 job 持续超宽限期则 fail-fast,避免永久卡住。

        判定:无 running 步,且所有 ready 步所在 pool 都无在线 worker.
        典型是未部署 gpu worker 时 audio 的 02_whisper 卡在 queue:gpu。
        给出明确错误而非静默挂起;宽限期容忍 worker 短暂重启。
        """
        active_jobs = await self.owner.redis.get_active_jobs()
        # Worker 可用性在单轮扫描内是全局事实。active jobs 多时跨 job 复用,
        # 避免每个 ready step 都重复扫 worker registry。
        pool_ok: dict[tuple[str, frozenset[str]], bool] = {}
        for job_id in active_jobs:
            statuses = await self.owner.redis.get_all_step_statuses(job_id)
            if not statuses or any(v == "running" for v in statuses.values()):
                self.owner._no_worker_since.pop(job_id, None)
                continue
            ready = [s for s, v in statuses.items() if v == "ready"]
            if not ready:
                self.owner._no_worker_since.pop(job_id, None)
                continue

            pipeline = await self.owner.redis.get_job_pipeline(job_id)
            steps_cfg = await self.owner._get_job_pipeline_steps(job_id) if pipeline else {}
            steps_cfg = steps_cfg or {}
            stuck: list[tuple[str, str]] = []
            progressable = False
            for step in ready:
                cfg = steps_cfg.get(step, {})
                pool = cfg.get("pool", "")
                try:
                    req = await self.owner._required_tags_for_step(job_id, step, cfg)
                except InvalidAIOverrideError as exc:
                    await self.owner._fail_invalid_ai_override(job_id, step, str(exc))
                    stuck = []
                    progressable = True
                    break
                key = (pool, frozenset(req))
                if key not in pool_ok:
                    pool_ok[key] = await self.owner._pool_has_workers_for(pool, req)
                if pool_ok[key]:
                    progressable = True
                    break
                stuck.append((step, pool))
            if progressable or not stuck:
                self.owner._no_worker_since.pop(job_id, None)
                continue

            first = self.owner._no_worker_since.setdefault(job_id, time.time())
            if time.time() - first < self.owner._NO_WORKER_GRACE_SEC:
                continue
            waited = round(time.time() - first)
            self.owner._no_worker_since[job_id] = time.time()
            pairs = ", ".join(f"{s}(pool '{p}')" for s, p in stuck)
            logger.warning("job_waiting_for_worker", job_id=job_id, stuck=pairs)
            await self.owner.redis.push_event(
                "no_worker", job_id=job_id, step=stuck[0][0], pool=stuck[0][1], waited_sec=waited)

        # 清理已离开 active 集合的计时,避免泄漏。
        active_set = set(active_jobs)
        for jid in [j for j in self.owner._no_worker_since if j not in active_set]:
            self.owner._no_worker_since.pop(jid, None)

    async def _retry_failed(
        self, job_id: str, idempotency_key: str | None = None,
    ) -> None:
        """重试失败Job:每个相互独立的失败根各重跑一次。"""
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
        if not statuses:
            job = await asyncio.to_thread(self.owner.db.get_job, job_id)
            if job is None or job.status != JobStatus.FAILED:
                return
            await self.owner.submit_job(job)
            statuses = await self.owner.redis.get_all_step_statuses(job_id)
        failed_steps = [s for s, st in statuses.items() if st == "failed"]
        if not failed_steps:
            return
        steps = await self.owner._get_job_pipeline_steps(job_id) or {}
        failed = set(failed_steps)
        failed_descendants = {
            descendant
            for step in failed
            for descendant in self.owner._get_downstream(steps, step)
            if descendant in failed
        }
        roots = [step for step in steps if step in failed - failed_descendants]
        for index, root in enumerate(roots):
            operation_key = (
                f"{idempotency_key}:root:{index}:{root}"
                if idempotency_key is not None else None
            )
            await self.owner.rerun(
                job_id, root, idempotency_key=operation_key,
            )
        logger.info("job_retry", job_id=job_id, from_steps=roots)

    async def rerun(
        self, job_id: str, from_step: str,
        idempotency_key: str | None = None,
    ) -> list[str]:
        """从指定步骤开始重跑,清除该步骤及所有下游的 .done 标记。返回被重置的步骤列表。"""
        pipeline = await self.owner.redis.get_job_pipeline(job_id)
        if not pipeline:
            return []
        steps = await self.owner._get_job_pipeline_steps(job_id) or {}
        if from_step not in steps:
            logger.warning(
                "job_rerun_invalid_step",
                job_id=job_id,
                pipeline=pipeline,
                from_step=from_step,
            )
            return []
        self.owner._cancel_delayed_tasks(job_id)  # 取消在途延迟重试,防与新一轮状态串台
        downstream = self.owner._get_downstream(steps, from_step)
        reset_steps = [from_step] + downstream

        generation, should_apply = await self.owner.redis.begin_job_generation(
            job_id, idempotency_key,
        )
        if not should_apply:
            return reset_steps

        for step in reset_steps:
            await self.owner._revoke_step_execution(job_id, step)
            from shared.step_scope import parse_execution_step, part_id_from_scope
            scope_key, template_step = parse_execution_step(step)
            part_id = part_id_from_scope(scope_key)
            prefix = f"parts/{part_id}/" if part_id else ""
            # §2.6-5:遇在途 commit 有界等待;generation 已换代,旧 token 的每次 promote
            # 前后校验都会被 CAS 拒绝,等待只为收敛残留,超时即撤销 token。
            commit = await self.owner.redis.get_step_commit(job_id, step)
            if commit:
                deadline = time.time() + 3
                while commit and time.time() < deadline:
                    await asyncio.sleep(0.2)
                    commit = await self.owner.redis.get_step_commit(job_id, step)
                await self.owner.redis.clear_step_commit(job_id, step)
            # §2.10-3:删目标及下游 final manifest,并按各自旧 manifest 精确删除其输出。
            # Part rerun 的失效边界由展开 DAG 决定(fan-in 边已展开进 depends_on),
            # 只波及该 Part 下游与 Job reduce,其余 Part 保持。
            if self.owner.storage is not None:
                from shared.step_completion import read_valid_manifest
                from shared.step_manifest import manifest_relative_path

                manifest = await read_valid_manifest(
                    self.owner.storage, job_id, scope_key, template_step,
                )
                # 删除失败不吞:异常让整个 lifecycle 命令失败,PEL 重投重试整个 rerun
                # (begin_job_generation 的 idempotency token 在 complete 前保持 applying,
                # 重放会重新执行删除;delete_file 幂等)。旧代 manifest 的清除责任在此,
                # 对账不做 generation 裁决(backfill generation=0 与 clone 保留父代因此合法)。
                if manifest is not None:
                    for entry in manifest["outputs"]:
                        await self.owner.storage.delete_file(
                            job_id, f"{prefix}{entry['path']}",
                        )
                await self.owner.storage.delete_file(
                    job_id, manifest_relative_path(scope_key, template_step),
                )
            done_file = self.owner.jobs_dir / job_id / prefix / f".{template_step}.done"
            await asyncio.to_thread(done_file.unlink, True)
            # 中心存储的 .done 必须一并删:MinIO 部署下 .done 在 bucket,只删本地是 no-op,
            # worker pull 回旧 .done 指纹命中直接跳过,rerun/「重跑该步」整体失效。
            # best-effort:删失败只告警不挡主流程(兜底=改步 version 失效指纹)。
            if self.owner.storage is not None:
                try:
                    await self.owner.storage.delete_file(
                        job_id, f"{prefix}.{template_step}.done",
                    )
                except Exception:
                    logger.warning("rerun_central_done_delete_failed",
                                   job_id=job_id, step=step, exc_info=True)
            await self.owner.redis.set_step_status(job_id, step, "waiting")
            # 清重试计数,否则重跑曾耗尽重试的步骤会零重试预算、首次失败即终止。
            await self.owner.redis.reset_step_retries(job_id, step)
            await asyncio.to_thread(
                self.owner.db.update_step, job_id, step,
                # 清掉上一轮的起止/耗时,否则重置成 waiting 的步骤会显示旧时间(诡异)。
                status="waiting", error=None,
                started_at=None, finished_at=None, duration_sec=None,
            )

        # 刷新术语快照:P3 修复路径 = 人工定准 glossary.zh_name 后 rerun 04,必须让新表生效。
        job = await asyncio.to_thread(self.owner.db.get_job, job_id)
        if job:
            await self.owner._export_term_map(job)

        await asyncio.to_thread(
            self.owner.db.update_job, job_id, status=JobStatus.PROCESSING,
        )
        await self.owner.redis.add_active_job(job_id)
        await self.owner.redis.complete_job_generation(
            job_id, idempotency_key, generation,
        )
        await self.owner._check_downstream(job_id)

        logger.info("job_rerun", job_id=job_id, from_step=from_step, reset=reset_steps)
        return reset_steps

    async def resubmit(
        self, job_id: str, idempotency_key: str | None = None,
    ) -> None:
        """按当前 pipelines.yaml 重新初始化步骤,保留已有步骤的状态。

        以当前 pipeline 为准对齐 redis 与 DB 两侧:删去 pipeline 不再有的步(两侧都删)、
        补齐新步,并把每个步在 redis/DB 写到同一状态;不变量:redis 与 DB 步集一致.
        删旧步若只删 redis 不删 DB,或用 redis existing 当判据跳过 DB 回填,renumber/改
        pipeline 后流水线读 DB 会显示旧步、与实际执行的 redis 分叉。"""
        self.owner.reload_config()

        pipeline = await self.owner.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        generation, should_apply = await self.owner.redis.begin_job_generation(
            job_id, idempotency_key,
        )
        if not should_apply:
            return
        steps = await self.owner._get_job_pipeline_steps(job_id) or {}
        # 状态真源:redis(运行态)优先,redis 无则用 DB,都无则 waiting,并保留已完成/已跑步骤状态.
        existing = await self.owner.redis.get_all_step_statuses(job_id)
        from shared.step_scope import execution_step_key
        db_status = {
            execution_step_key(s.scope_key, s.name): (
                s.status.value if isinstance(s.status, StepStatus) else s.status
            )
            for s in await asyncio.to_thread(self.owner.db.get_steps, job_id)
        }

        # 删去当前 pipeline 不再有的步:redis 与 DB 都删,否则 DB 残留旧步。
        for name in (set(existing) | set(db_status)) - set(steps):
            await self.owner.redis.delete_step_status(job_id, name)
            await asyncio.to_thread(self.owner.db.delete_step, job_id, name)

        # 当前 pipeline 的每个步:取已有状态(缺则 waiting),redis 与 DB 都对齐到该状态。
        # DB 侧:已有行只在状态变化时 update_step(status=),不能 upsert_step 整行替换,
        # 否则会抹掉已完成步的 started_at/finished_at/duration/input_hash(流水线显示无时间);
        # 仅 DB 缺该步(分叉)时才 upsert_step 新建。
        for name, cfg in steps.items():
            status = existing.get(name) or db_status.get(name) or "waiting"
            await self.owner.redis.set_step_status(job_id, name, status)
            if name in db_status:
                if db_status[name] != status:
                    await asyncio.to_thread(
                        self.owner.db.update_step, job_id, name, status=StepStatus(status),
                    )
            else:
                await asyncio.to_thread(
                    self.owner.db.upsert_step,
                    Step(
                        job_id=job_id,
                        name=cfg["template_step"],
                        scope_key=cfg["scope_key"],
                        status=StepStatus(status),
                        pool=cfg["pool"],
                    ),
                )

        await asyncio.to_thread(
            self.owner.db.update_job, job_id, status=JobStatus.PROCESSING,
        )
        await self.owner.redis.add_active_job(job_id)
        await self.owner.redis.complete_job_generation(
            job_id, idempotency_key, generation,
        )
        await self.owner._check_downstream(job_id)

        logger.info("job_resubmit", job_id=job_id, pipeline=pipeline)

    async def continue_ai(
        self, job_id: str, idempotency_key: str | None = None,
    ) -> list[str]:
        """旧命令不再原地改快照;API 现以不可变 full snapshot 实现继续 AI。"""
        logger.warning("legacy_continue_ai_ignored", job_id=job_id)
        return []

    async def _demote_step_and_downstream(
        self, job_id: str, step: str, steps: dict, statuses: dict, reason: str,
    ) -> None:
        """§2.1 对账表行 3/4:done/skipped 降 waiting 并失效 DAG 下游(连带删下游 manifest,
        防下一次对账用仍有效的下游 manifest 把投影翻回 done)。"""
        from shared.step_manifest import manifest_relative_path
        from shared.step_scope import parse_execution_step

        # 下游闭包不按当前状态过滤:failed/running 等状态的下游 manifest 若不删,
        # 同轮对账稍后会据其把投影翻回 done(方向唯一性被破坏)。
        targets = [step] + self.owner._get_downstream(steps, step)
        for item in targets:
            scope_key, template_step = parse_execution_step(item)
            if self.owner.storage is not None:
                try:
                    await self.owner.storage.delete_file(
                        job_id, manifest_relative_path(scope_key, template_step),
                    )
                except Exception:
                    pass
            if item != step and statuses.get(item) not in ("done", "skipped", "waiting"):
                continue  # 状态仅对 done/skipped 置 waiting(waiting 幂等);running/failed 由各自机制收敛
            await self.owner.redis.set_step_status(job_id, item, "waiting")
            await self.owner.redis.reset_step_retries(job_id, item)
            await asyncio.to_thread(
                self.owner.db.update_step, job_id, item,
                status="waiting", error=None,
                started_at=None, finished_at=None, duration_sec=None,
            )
            statuses[item] = "waiting"
        logger.warning(
            "manifest_reconcile_demoted", job_id=job_id, step=step,
            reason=reason, downstream=len(targets) - 1,
        )

    async def reconcile_step_manifests(self, job_id: str) -> None:
        """启动对账(§2.1 五行表 + §2.7 启动顺序):manifest 是完成事实,DB/Redis 只是投影。

        dual 模式下 manifest 缺失时 .done 仍是权威(不降级);manifest-only 下缺失/损坏
        即降 waiting 并失效下游。修复投影后由 _recover 既有流程重放副作用与入队。
        """
        if self.owner.storage is None:
            return
        from shared.step_completion import (
            MODE_MANIFEST_ONLY,
            completion_mode,
            read_valid_manifest,
            verify_manifest_outputs_metadata,
        )
        from shared.step_scope import parse_execution_step

        mode = completion_mode()
        steps = await self.owner._get_job_pipeline_steps(job_id) or {}
        if not steps:
            return
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
        repaired = False
        for name, cfg in steps.items():
            status = statuses.get(name)
            if status == "running":
                continue  # 在途执行由 orphan/commit fence 收敛,不在对账内翻转
            scope_key, template_step = parse_execution_step(name)
            manifest = await read_valid_manifest(
                self.owner.storage, job_id, scope_key, template_step,
            )
            if manifest is not None:
                outputs_ok = await verify_manifest_outputs_metadata(
                    self.owner.storage, job_id, manifest,
                )
                if not outputs_ok:
                    # 行 3:输出与声明不符。dual 阶段只告警(.done 仍在,避免启动风暴);
                    # manifest-only 即降级失效下游。
                    if mode == MODE_MANIFEST_ONLY and status in ("done", "skipped"):
                        await self._demote_step_and_downstream(
                            job_id, name, steps, statuses, "manifest_outputs_invalid",
                        )
                    else:
                        logger.warning(
                            "manifest_outputs_mismatch", job_id=job_id, step=name,
                        )
                    continue
                desired = "done" if manifest["outcome"] == "done" else "skipped"
                if status not in ("done", "skipped"):
                    # 行 2:manifest 有效而投影落后(崩溃窗口 §2.7 行 3)→ 修复投影。
                    await self.owner.redis.set_step_status(job_id, name, desired)
                    await asyncio.to_thread(
                        self.owner.db.update_step, job_id, name, status=desired,
                    )
                    statuses[name] = desired
                    repaired = True
                    logger.info(
                        "manifest_reconcile_repaired", job_id=job_id,
                        step=name, status=desired,
                    )
                continue
            # manifest 缺失
            if status == "done" and mode != MODE_MANIFEST_ONLY:
                # dual 遥测:切 manifest-only 前据此计数评估 backfill 覆盖度。
                logger.warning(
                    "manifest_missing_dual", job_id=job_id, step=name,
                )
            if status == "done" and mode == MODE_MANIFEST_ONLY:
                await self._demote_step_and_downstream(
                    job_id, name, steps, statuses, "manifest_missing",
                )
                continue
            if status == "skipped":
                # §2.8:无 skipped manifest 的 skip 重新求值;可确定性重推导才保留,
                # 否则(典型 no_worker)转 waiting,由调度按当前环境重新决定。
                keep = False
                info = await self.owner.redis.get_job_info(job_id)
                try:
                    flags = json.loads(info.get("flags") or "{}")
                except (TypeError, ValueError):
                    flags = {}
                if flags.get("mechanical_only") is True and cfg.get("pool") == "ai":
                    keep = True
                elif self.owner._step_is_conditional(cfg):
                    try:
                        keep = not await self.owner._eval_step_condition(job_id, cfg)
                    except Exception:
                        keep = True  # 条件求值失败保持现状,不放大故障
                if not keep:
                    await self.owner.redis.set_step_status(job_id, name, "waiting")
                    await asyncio.to_thread(
                        self.owner.db.update_step, job_id, name, status="waiting",
                    )
                    statuses[name] = "waiting"
                    repaired = True
                    logger.info(
                        "environmental_skip_reset", job_id=job_id, step=name,
                    )
        if repaired:
            # 行 2 修复后幂等重放完成副作用(§2.7 步骤 4);重复副作用由 effects 自身幂等门挡。
            try:
                await self.owner._reconcile_completed_effects(job_id)
            except Exception:
                logger.warning(
                    "manifest_reconcile_effects_failed", job_id=job_id, exc_info=True,
                )

    _STAGING_CLEANUP_INTERVAL_SEC = 600
    _STAGING_STALE_GRACE_SEC = 1800  # 3x commit token TTL:崩溃残留才回收,在途绝不清

    async def cleanup_execution_staging_orphans(self) -> None:
        """周期回收孤儿执行 staging(§2.6-9 的 TTL 兜底);活跃执行经 exec_id 集合保护。"""
        storage = self.owner.storage
        if storage is None or not hasattr(storage, "cleanup_stale_execution_staging"):
            return
        now = time.time()
        last = getattr(self.owner, "_staging_cleanup_at", 0.0)
        if now - last < self._STAGING_CLEANUP_INTERVAL_SEC:
            return
        self.owner._staging_cleanup_at = now
        live: set[str] = set()
        try:
            for job_id in await self.owner.redis.get_active_jobs():
                statuses = await self.owner.redis.get_all_step_statuses(job_id)
                for step, status in statuses.items():
                    if status == "running":
                        exec_id = await self.owner.redis.get_step_exec_id(job_id, step)
                        if exec_id:
                            live.add(exec_id)
            removed = await storage.cleanup_stale_execution_staging(
                active_exec_ids=live,
                stale_before_epoch=now - self._STAGING_STALE_GRACE_SEC,
            )
            if removed:
                logger.info("execution_staging_cleaned", removed=removed)
        except Exception:
            logger.warning("execution_staging_cleanup_failed", exc_info=True)
