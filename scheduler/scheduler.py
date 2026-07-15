"""调度器:监听步骤完成/失败事件,推进 DAG,管理 Job 生命周期。"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import structlog

from shared.config import AppConfig, load_config
from shared.db import Database
from shared.models import AITask, Job, LLMRequest
from shared.note_text import markdown_to_index_text
from shared.redis_client import RedisClient
from shared.study_suggestions import canonical_json, prefixed_sha256, validate_study_suggestion_prompt_snapshot
from shared.storage import StorageBackend

from scheduler.background import BackgroundServices
from scheduler.dag_planner import DagPlanner
from scheduler.effects import EffectDispatcher
from scheduler.job_finalizer import JobFinalizer
from scheduler.lifecycle import LifecycleCoordinator
from scheduler.recovery import RecoveryCoordinator
from scheduler.task_router import TaskRouter

logger = structlog.get_logger(component="scheduler")

# 命中来源站点、需按网络可达区域(net-zone)路由的步骤;其余步骤本地/AI,不分区域。
# 区域判定见 shared.net_zone(按 URL + 构建时烤入的 CN 域名表);worker 启动自动探测自报覆盖区域。
# 网络路由 tag 只有 net-cn / net-global;B站 SESSDATA 等凭证是 worker 本地的事(下载步自读),非路由 tag。

def _markdown_to_text(md: str) -> str:
    """兼容旧调用点;归一化实现只保留在 shared.note_text。"""
    return markdown_to_index_text(md)


class Scheduler:
    def __init__(
        self,
        redis: RedisClient,
        db: Database,
        config: AppConfig,
        storage: StorageBackend | None = None,
    ):
        self.redis = redis
        self.db = db
        self.config = config
        # storage 在 NAS 侧(调度器有 DB)读笔记/评审产物做索引与术语采集;
        # worker 可能远程无 DB,故索引必须落在这里。未注入则跳过(向后兼容)。
        self.storage = storage
        self.jobs_dir = config.jobs_dir
        self._shutdown = False
        self._pubsub_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # 心跳 payload 源:启动时刻(算 uptime)+ 上一拍 periodic 循环的实测延迟(loop_lag)。
        self._started_at_iso = datetime.now(timezone.utc).isoformat()
        self._last_tick: float | None = None       # 上一拍 periodic 循环的 monotonic 时刻
        self._last_loop_lag: float = 0.0            # 实测间隔 - 期望(30s)的超出量,≥5s 叠加 degraded
        # 跟踪所有 _delayed_enqueue fire-and-forget 任务,供 shutdown / rerun /
        # job 失败时取消,避免泄漏或旧重试与新状态串台。
        self._delayed_tasks: set[asyncio.Task] = set()
        # 自动周报结果收割任务(fire-and-forget,done 自摘,防 GC 早收)。
        self._digest_harvest_tasks: set[asyncio.Task] = set()
        # 概念重综合不阻塞 job 终态；同一概念只保留一个在途调用，避免 concepts/review
        # 连续完成时重复消耗 provider。
        self._concept_synthesis_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # 在途期间的新 completion 合并为一次 latest rerun；review 可能让原先
        # no_quorum 的概念变得可综合，不能静默吞掉这次状态变化。
        self._concept_synthesis_pending: dict[
            tuple[str, str], tuple[str, int]
        ] = {}
        # job_id -> 首次被判定"无 worker 可推进"的时刻,超宽限期才 fail-fast(容忍 worker 重启)。
        self._no_worker_since: dict[str, float] = {}
        # (job_id, step) -> 首次发现"在跑步骤的 worker 上报的 current_step 不是本步"的时刻,
        # 超宽限期才回收(容忍认领后首拍心跳延迟),防 gateway 认领响应丢失导致的永久卡 running。
        self._claim_mismatch_since: dict[tuple[str, str], float] = {}
        # 上一拍判定为"陈旧"(持有槽但不属于任何 running 步)的 holder 集合。仅连续两拍都陈旧才 SREM,
        # 避开"刚占槽、尚未写 running 状态"的认领窗口被周期对账误清(同 _claim_mismatch_since 的宽限思路)。
        self._slot_reconcile_suspect: set[str] = set()
        self._dag_planner = DagPlanner(self)
        self._task_router = TaskRouter(self)
        self._lifecycle = LifecycleCoordinator(self)
        self._recovery = RecoveryCoordinator(self)
        self._effects = EffectDispatcher(self)
        self._job_finalizer = JobFinalizer(self)
        self._background = BackgroundServices(self)

    # 生命周期

    async def run(self) -> None:
        return await self._background.run()

    async def _publish_resource_limits(self) -> None:
        return await self._background._publish_resource_limits()

    async def shutdown(self) -> None:
        return await self._background.shutdown()

    def _on_delayed_done(self, task: asyncio.Task) -> None:
        return self._background._on_delayed_done(task)

    def _cancel_delayed_tasks(self, job_id: str) -> None:
        return self._background._cancel_delayed_tasks(job_id)

    # 主循环

    async def _event_loop(self) -> None:
        return await self._background._event_loop()

    async def _notification_loop(self) -> None:
        return await self._background._notification_loop()

    async def _dispatch(self, msg: dict) -> None:
        return await self._background._dispatch(msg)

    _PERIODIC_INTERVAL_SEC = 30

    async def _periodic_loop(self) -> None:
        return await self._background._periodic_loop()

    async def _heartbeat_loop(self) -> None:
        return await self._background._heartbeat_loop()

    async def cleanup_stale_workers(self, timeout_sec: int | None = None) -> None:
        return await self._background.cleanup_stale_workers(timeout_sec)

    async def check_radar_digest(self, today=None) -> int:
        """每周自动周报:当天 UTC 星期命中 RADAR_DIGEST_CRON_DOW 时给每个 domain 投 digest AI task.
        periodic 每 30s 进来,幂等靠 redis SET NX 当日锁;锁先置,雷达无动静的库当日也不再重算.
        本周没新内容或概念变化的库不出空周报;结果经 airesult:{task_id} 取,最新指针落 radar:digest:latest.
        today 参数仅测试注入。返回本拍投递数。"""
        try:
            dow = int(os.environ.get("RADAR_DIGEST_CRON_DOW", "0"))
        except ValueError:
            dow = 0
        d = today or datetime.now(timezone.utc).date()
        if d.weekday() != dow % 7:
            return 0
        # api.services.radar 是纯函数服务层(只依赖 shared.db),调度器可安全复用;
        # 惰性导入避免 scheduler 启动期背上 api 包。
        from api.services import radar as radar_service
        import uuid

        queued = 0
        for dom in await asyncio.to_thread(self.db.list_domains):
            domain = dom.get("domain")
            if not domain:
                continue
            if not await self.redis.try_mark_auto_digest(domain, d.isoformat()):
                continue
            data = await asyncio.to_thread(radar_service.radar, self.db, domain, 7)
            if not (data["recent_jobs"] or data["rising_concepts"] or data["new_concepts"]):
                continue
            task_id = f"at_{uuid.uuid4().hex}"
            source_manifest = await asyncio.to_thread(
                radar_service.build_digest_source_manifest,
                self.db,
                task_id=task_id,
                radar_data=data,
            )
            queued_at = datetime.now(timezone.utc).isoformat()
            if not source_manifest["sources"]:
                await self.redis.set_latest_auto_digest(domain, {
                    "task_id": None,
                    "queued_at": queued_at,
                    "error": "canonical evidence unavailable",
                    "citation_validation": {
                        "kind": "digest_citations", "status": "unverified",
                        "reliable": False,
                        "issues": ["canonical_evidence_unavailable"],
                        "items": [], "checked_claims": 0,
                        "supported_claims": 0,
                        "manifest_sha256": source_manifest["manifest_sha256"],
                    },
                })
                continue
            system, user = radar_service.build_digest_prompt(data, source_manifest)
            payload = AITask(
                task_id=task_id,
                request=LLMRequest(
                    messages=[{"role": "user", "content": user}], system=system,
                    max_tokens=2048, temperature=0,
                ),
                step_name="digest",
                domain=domain,
                audit_context={"digest_source_manifest": source_manifest},
            ).to_task_payload()
            await self.redis.enqueue_ai_task(payload)
            await self.redis.set_latest_auto_digest(domain, {
                "task_id": task_id, "queued_at": queued_at,
            })
            await self.redis.push_event("radar_digest_queued", domain=domain, task_id=task_id)
            t = asyncio.create_task(self._harvest_digest_result(domain, task_id, queued_at))
            self._digest_harvest_tasks.add(t)
            t.add_done_callback(self._digest_harvest_tasks.discard)
            queued += 1
            logger.info("radar_digest_queued", domain=domain, task_id=task_id)
        return queued

    async def _harvest_digest_result(
        self, domain: str, task_id: str, queued_at: str,
        timeout_sec: int = 900, poll_sec: float = 10,
    ) -> None:
        """把自动周报结果从 airesult:{task_id}(TTL≈600s)搬进 radar:digest:latest(长存)。
        没人守屏轮询,ai-worker 完成后由这里收割;超时/失败落 error 供前端提示。"""
        deadline = time.monotonic() + timeout_sec
        info = {"task_id": task_id, "queued_at": queued_at}
        while time.monotonic() < deadline:
            res = await self.redis.get_ai_result(task_id)
            if res is not None:
                info["generated_at"] = datetime.now(timezone.utc).isoformat()
                if isinstance(res, dict) and res.get("error"):
                    info["error"] = str(res["error"])[:300]
                else:
                    from api.services.radar import validate_digest_citations

                    original = await self.redis.get_ai_task_original_payload(task_id)
                    audit_context = (
                        original.get("audit_context")
                        if type(original) is dict else None
                    )
                    manifest = (
                        audit_context.get("digest_source_manifest")
                        if type(audit_context) is dict else None
                    )
                    content = str((res or {}).get("content") or "")
                    validation = validate_digest_citations(task_id, content, manifest)
                    info["citation_validation"] = validation
                    if validation["reliable"]:
                        info["markdown"] = content
                    else:
                        info["error"] = "digest citation validation failed"
                await self.redis.set_latest_auto_digest(domain, info)
                await self.redis.push_event("radar_digest_ready", domain=domain, task_id=task_id)
                return
            await asyncio.sleep(poll_sec)
        info["error"] = "digest result timeout"
        await self.redis.set_latest_auto_digest(domain, info)

    async def _recover(self) -> None:
        return await self._recovery._recover()

    async def _recover_pending_jobs(self) -> None:
        return await self._recovery._recover_pending_jobs()

    @staticmethod
    def _study_suggestion_ai_payload(batch: dict) -> dict:
        """只从持久批次快照构造任务,重启和 retry 不再读取当前 Prompt 文件."""
        persisted = batch.get("llm_request")
        if not isinstance(persisted, dict):
            raise ValueError("study suggestion llm_request is invalid")
        prompt = persisted.get("prompt_snapshot")
        raw = validate_study_suggestion_prompt_snapshot(prompt)
        request_input = {
            key: value for key, value in persisted.items() if key != "prompt_snapshot"
        }
        request = LLMRequest(
            messages=[{"role": "user", "content": canonical_json(request_input)}],
            system=raw.decode("utf-8"),
            max_tokens=8_192,
            temperature=0.2,
            response_format="json_object",
        )
        payload = AITask(
            task_id=str(batch["task_id"]),
            request=request,
            step_name="study_suggestions",
            domain=str(batch["domain"]),
            provider=str(batch["provider"]),
            model=str(batch["model"]),
        ).to_task_payload()
        payload.update({
            "batch_id": str(batch["batch_id"]),
            "attempt": int(batch["attempt"]),
            "revision": int(batch["revision"]),
            "generator_fingerprint": str(batch["generator_fingerprint"]),
            "input_fingerprint": str(batch["input_fingerprint"]),
            "prompt_snapshot": prompt,
        })
        payload["task_payload_sha256"] = prefixed_sha256(
            canonical_json(payload).encode("utf-8")
        )
        return payload

    async def _fail_study_suggestion_batch(self, batch: dict, code: str, message: str) -> None:
        return await self._recovery._fail_study_suggestion_batch(batch, code, message)

    async def reconcile_study_suggestion_batches(self, *, now: datetime | None = None) -> int:
        return await self._recovery.reconcile_study_suggestion_batches(now=now)

    # Job 提交

    async def submit_job(self, job: Job) -> None:
        return await self._lifecycle.submit_job(job)

    # 事件处理

    async def _exec_is_current(self, job_id: str, step: str, exec_id: str | None, generation: int | None) -> bool:
        return await self._lifecycle._exec_is_current(job_id, step, exec_id, generation)

    async def on_step_started(self, job_id: str, step: str, worker: str | None = None) -> None:
        return await self._lifecycle.on_step_started(job_id, step, worker)

    async def on_step_done(self, job_id: str, step: str, duration: float | None = None, worker: str | None = None, exec_id: str | None = None, generation: int | None = None, started_at: float | None = None) -> None:
        return await self._lifecycle.on_step_done(job_id, step, duration, worker, exec_id, generation, started_at)

    async def _run_step_completion_effects(self, job_id: str, step: str) -> bool:
        return await self._effects._run_step_completion_effects(job_id, step)

    async def _run_completion_effects(self, job_id: str, step: str, effects: list) -> bool:
        return await self._effects._run_completion_effects(job_id, step, effects)

    async def _index_first_available_note(self, job_id: str, candidates: list[dict]) -> None:
        return await self._effects._index_first_available_note(job_id, candidates)

    async def _index_job_notes(
        self, job_id: str, note_type: str, rel: str, data: bytes, *,
        candidate_types: list[str] | None = None,
        source_manifest_path: str | None = None,
        provenance_path: str | None = None,
        provenance_step: str | None = None,
        provenance_since_version: str | None = None,
        legacy_provenance_step: str | None = None,
        legacy_provenance_since_version: str | None = None,
    ) -> None:
        return await self._effects._index_job_notes(
            job_id, note_type, rel, data,
            candidate_types=candidate_types,
            source_manifest_path=source_manifest_path,
            provenance_path=provenance_path,
            provenance_step=provenance_step,
            provenance_since_version=provenance_since_version,
            legacy_provenance_step=legacy_provenance_step,
            legacy_provenance_since_version=legacy_provenance_since_version,
        )

    async def _is_legacy_provenance_completion(
        self, job: Job | None, provenance_path: str, *,
        provenance_step: str | None,
        provenance_since_version: str | None,
        legacy_provenance_step: str | None = None,
        legacy_provenance_since_version: str | None = None,
    ) -> bool:
        return await self._effects._is_legacy_provenance_completion(
            job, provenance_path,
            provenance_step=provenance_step,
            provenance_since_version=provenance_since_version,
            legacy_provenance_step=legacy_provenance_step,
            legacy_provenance_since_version=legacy_provenance_since_version,
        )

    async def _reconcile_completed_effects(self, job_id: str) -> bool:
        return await self._effects._reconcile_completed_effects(job_id)

    async def _export_term_map(self, job: Job) -> None:
        return await self._effects._export_term_map(job)

    async def _collect_term_pairs(self, job_id: str) -> None:
        return await self._effects._collect_term_pairs(job_id)

    async def _collect_glossary(self, job_id: str) -> None:
        return await self._effects._collect_glossary(job_id)

    async def _read_verification_artifact(self, job_id: str, rel: str) -> bytes | None:
        return await self._effects._read_verification_artifact(job_id, rel)

    def _write_concept_relations(self, domain: str, items: list[tuple[str, str, list]]) -> int:
        return self._effects._write_concept_relations(domain, items)

    def _replace_concept_occurrences(self, domain: str, job_id: str, key_terms: list, evidence_note_type: object) -> tuple[int, list[tuple[str, str, int]]]:
        return self._effects._replace_concept_occurrences(domain, job_id, key_terms, evidence_note_type)

    def _schedule_concept_resynthesis(self, domain: str, candidates: list[tuple[str, str, int]]) -> None:
        return self._effects._schedule_concept_resynthesis(domain, candidates)

    def _on_concept_synthesis_done(self, key: tuple[str, str], task: asyncio.Task) -> None:
        return self._effects._on_concept_synthesis_done(key, task)

    async def _auto_resynthesize_concept(
        self,
        domain: str,
        term: str,
        current_id: str,
        lock_revision: int,
    ) -> None:
        """用对账时的 CAS 快照尝试重综合；并发编辑由服务层拒绝。"""
        try:
            from api.services.concepts import maybe_resynthesize_concept

            result = await maybe_resynthesize_concept(
                self.db,
                self.storage,
                self.config,
                domain,
                term,
                expected_current_version_id=current_id,
                expected_lock_revision=lock_revision,
                actor="scheduler:auto",
                strategy="automatic_resynthesis",
            )
            logger.info(
                "concept_resynthesis_finished",
                domain=domain,
                term=term,
                created=bool(result.get("created")),
                reason=result.get("reason"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "concept_resynthesis_failed",
                domain=domain,
                term=term,
                exc_info=True,
            )

    async def on_step_failed(self, job_id: str, step: str, error: str, error_type: str = 'unknown', worker: str | None = None, exec_id: str | None = None, generation: int | None = None, duration: float | None = None, started_at: float | None = None, count_stats: bool = False) -> None:
        return await self._lifecycle.on_step_failed(job_id, step, error, error_type, worker, exec_id, generation, duration, started_at, count_stats)

    async def _delayed_enqueue(self, delay: int, job_id: str, step: str) -> None:
        return await self._lifecycle._delayed_enqueue(delay, job_id, step)

    async def _record_worker_terminal_stats(self, worker_id: str | None, *, completed: int = 0, failed: int = 0, duration: float = 0.0) -> None:
        return await self._lifecycle._record_worker_terminal_stats(worker_id, completed=completed, failed=failed, duration=duration)

    # DAG 推进

    async def _check_downstream(self, job_id: str) -> None:
        return await self._dag_planner._check_downstream(job_id)

    async def enqueue_step(self, job_id: str, step_name: str) -> bool:
        return await self._task_router.enqueue_step(job_id, step_name)

    async def _fail_invalid_ai_override(self, job_id: str, step_name: str, reason: str) -> None:
        return await self._task_router._fail_invalid_ai_override(job_id, step_name, reason)

    async def _required_tags_for_step(self, job_id: str, step_name: str, step_cfg: dict, job_info: dict | None = None) -> list[str]:
        return await self._task_router._required_tags_for_step(job_id, step_name, step_cfg, job_info)

    async def _list_job_files(self, job_id: str) -> list[str]:
        return await self._task_router._list_job_files(job_id)

    async def check_condition(self, job_id: str, condition: str) -> bool:
        return await self._task_router.check_condition(job_id, condition)

    def _step_is_conditional(self, cfg: dict) -> bool:
        return self._task_router._step_is_conditional(cfg)

    async def _eval_step_condition(self, job_id: str, cfg: dict) -> bool:
        return await self._task_router._eval_step_condition(job_id, cfg)

    async def _eval_rules(self, job_id: str, rules: list) -> bool:
        return await self._task_router._eval_rules(job_id, rules)

    async def _pool_has_workers(self, pool: str) -> bool:
        return await self._task_router._pool_has_workers(pool)

    async def _pool_has_workers_for(self, pool: str, require_tags: list[str]) -> bool:
        return await self._task_router._pool_has_workers_for(pool, require_tags)

    # Job 状态

    async def mark_job_done(self, job_id: str) -> bool:
        return await self._job_finalizer.mark_job_done(job_id)

    async def reconcile_completion_effects(self) -> None:
        return await self._job_finalizer.reconcile_completion_effects()

    async def _advance_book_chain(self, job_id: str) -> None:
        return await self._job_finalizer._advance_book_chain(job_id)

    async def _sync_published_at(self, job_id: str) -> None:
        return await self._job_finalizer._sync_published_at(job_id)

    async def mark_job_failed(self, job_id: str, error: str) -> None:
        return await self._job_finalizer.mark_job_failed(job_id, error)

    # 孤儿回收 + 卡住检测

    # 认领后到首个进度心跳之间允许的无心跳窗口。尤其 gateway worker 拉大源文件(source.mp4)
    # 的 pull 阶段:子进程未起、on_tick 未触发,progress_at 为 None。取 120s 覆盖慢链路拉取,
    # 避免 pull 中的步被误判 claim lost 回收(实测误判会让 03_scene 等步雪崩)。真丢认领最迟
    # 120s 回收(罕见,可接受)。开 STORAGE_WORKDIR_REUSE 后 pull 近乎瞬时、基本不触发。env 可调。
    _CLAIM_MISMATCH_GRACE_SEC = int(os.environ.get("CLAIM_MISMATCH_GRACE_SEC", "120"))
    # 判"这步是否有人在跑"用每步独立的进度心跳新鲜度(worker on_tick 每 10s 刷一步)。
    # 30s(≈3 拍)留余量,避免扫描时序抖动误判正在跑的步。
    _STEP_PROGRESS_FRESH_SEC = 30

    async def orphan_scan(self) -> None:
        return await self._recovery.orphan_scan()

    async def _revoke_step_execution(self, job_id: str, step: str) -> tuple[str | None, int | None]:
        return await self._recovery._revoke_step_execution(job_id, step)

    async def _reclaim_step(self, job_id: str, step: str, reason: str, error_type: str = 'processing') -> None:
        return await self._recovery._reclaim_step(job_id, step, reason, error_type)

    async def reconcile_slots(self) -> None:
        return await self._recovery.reconcile_slots()

    async def check_stuck(self) -> None:
        return await self._recovery.check_stuck()

    # 默认 90s 宽限即 fail-fast(无可用 worker 的 job)。可经 env 调大,用于
    # "只跑部分 worker"的运维窗口(如夜间仅 ECS download worker,其余步骤明天再跑),
    # 避免下载完的 job 因缺 scene/cpu/ai worker 被误判失败。
    _NO_WORKER_GRACE_SEC = int(os.environ.get("NO_WORKER_GRACE_SEC", "90"))

    async def check_no_worker(self) -> None:
        return await self._recovery.check_no_worker()

    # 重跑 / 重提交

    async def _retry_failed(self, job_id: str, idempotency_key: str | None = None) -> None:
        return await self._recovery._retry_failed(job_id, idempotency_key)

    async def rerun(self, job_id: str, from_step: str, idempotency_key: str | None = None) -> list[str]:
        return await self._recovery.rerun(job_id, from_step, idempotency_key)

    async def resubmit(self, job_id: str, idempotency_key: str | None = None) -> None:
        return await self._recovery.resubmit(job_id, idempotency_key)

    def reload_config(self) -> None:
        self.config = load_config(
            config_dir=self.config.config_dir,
            data_dir=self.config.data_dir,
        )
        logger.info("config_reloaded")

    # 内部工具

    def _get_pipeline_steps(self, pipeline: str) -> dict[str, dict]:
        steps_list = self.config.pipelines.get(pipeline, {}).get("steps", [])
        return {s["name"]: s for s in steps_list}

    async def _get_job_pipeline_steps(self, job_id: str) -> dict[str, dict] | None:
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return None
        return self._get_pipeline_steps(pipeline)

    def _get_downstream(self, steps: dict[str, dict], from_step: str) -> list[str]:
        return self._dag_planner._get_downstream(steps, from_step)

    def _calc_progress(self, steps_config: list[dict], statuses: dict[str, str]) -> int:
        return self._dag_planner._calc_progress(steps_config, statuses)

    async def _update_progress(self, job_id: str) -> int:
        return await self._dag_planner._update_progress(job_id)
