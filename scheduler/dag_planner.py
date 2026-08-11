"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
from collections import deque

import structlog

from shared.models import Step, StepStatus
from shared.step_scope import execution_step_key


if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

class DagPlanner:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def _align_pipeline_step_set(
        self,
        job_id: str,
        steps: dict[str, dict],
        statuses: dict[str, str],
    ) -> dict[str, str]:
        """把活跃 Job 的持久步骤集收敛到当前 DAG,在途旧步完成前保留为终态门。"""
        pipeline = await self.owner.redis.get_job_pipeline(job_id)
        if not pipeline:
            return statuses
        info = await self.owner.redis.get_job_info(job_id)
        try:
            style_tags = json.loads(info.get("style_tags") or "[]")
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            style_tags = []
        domain = info.get("domain", "general")
        current_generation = await self.owner.redis.get_job_generation(job_id)
        manifest_cache: dict[str, tuple[str | None, dict | None]] = {}

        async def current_manifest(name: str, cfg: dict) -> tuple[str | None, dict | None]:
            cached = manifest_cache.get(name)
            if cached is not None:
                return cached
            if self.owner.storage is None:
                result = (None, None)
                manifest_cache[name] = result
                return result
            from shared.step_completion import (
                read_valid_manifest,
                step_definition_digest_for,
                verify_manifest_outputs_metadata,
            )

            manifest = await read_valid_manifest(
                self.owner.storage, job_id, cfg["scope_key"], cfg["template_step"],
            )
            try:
                definition_digest = step_definition_digest_for(
                    pipeline,
                    cfg,
                    config=self.owner.config,
                    domain=domain,
                    style_tags=style_tags,
                )
            except Exception:
                definition_digest = None
            if (
                manifest is None
                or manifest.get("job_id") != job_id
                or (manifest.get("scope") or {}).get("scope_key")
                != cfg["scope_key"]
                or manifest.get("step") != cfg["template_step"]
                or definition_digest is None
                or (manifest.get("compatibility") or {}).get("definition_digest")
                != definition_digest
                or not await verify_manifest_outputs_metadata(
                    self.owner.storage, job_id, manifest,
                )
                or (
                    manifest.get("outcome") == "skipped"
                    and (manifest.get("skip") or {}).get("reason_code")
                    == "allowed_failure"
                    and statuses.get(name) != "skipped"
                    and (manifest.get("execution") or {}).get("job_generation")
                    != current_generation
                )
            ):
                result = (None, manifest)
            else:
                result = (
                    "done" if manifest["outcome"] == "done" else "skipped",
                    manifest,
                )
            manifest_cache[name] = result
            return result

        db_steps = {
            execution_step_key(item.scope_key, item.name): item
            for item in await asyncio.to_thread(self.owner.db.get_steps, job_id)
        }
        original_names = set(statuses)
        changed = original_names != set(steps)
        for name, cfg in steps.items():
            if name in statuses:
                continue
            stored = db_steps.get(name)
            manifest_status, _manifest = await current_manifest(name, cfg)
            # Redis 缺字段时 DB 只是可能迟到的投影。只有当前 definition 的有效
            # manifest 能恢复 terminal;否则一律 waiting,避免幽灵 running/done。
            status = manifest_status or "waiting"
            initialized = await self.owner.redis.init_missing_step_waiting(job_id, name)
            if not initialized:
                current = await self.owner.redis.get_step_status(job_id, name)
                if current is not None:
                    status = current
            elif manifest_status is not None:
                if not await self.owner.redis.cas_step_status(
                    job_id, name, "waiting", manifest_status,
                ):
                    status = (
                        await self.owner.redis.get_step_status(job_id, name)
                        or "waiting"
                    )
            statuses[name] = status
            if stored is None:
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
            else:
                await asyncio.to_thread(
                    self.owner.db.update_step,
                    job_id,
                    name,
                    status=status,
                    error=None if status == "waiting" else stored.error,
                    started_at=None if status == "waiting" else stored.started_at,
                    finished_at=None if status == "waiting" else stored.finished_at,
                    duration_sec=None if status == "waiting" else stored.duration_sec,
                )
            changed = True

        for name in sorted(set(statuses) - set(steps)):
            if statuses[name] == "running":
                continue
            if statuses[name] == "ready":
                # 快照之后 Worker 可能已完成 ready -> running 的原子 claim。只有仍为
                # ready 才能撤回;CAS 失败时保留真实在途状态,不能删除其执行身份。
                if not await self.owner.redis.cas_step_status(
                    job_id, name, "ready", "waiting",
                ):
                    current = await self.owner.redis.get_step_status(job_id, name)
                    if current is not None:
                        statuses[name] = current
                    if current == "running":
                        continue
            await self.owner.redis.delete_step_status(job_id, name)
            await asyncio.to_thread(self.owner.db.delete_step, job_id, name)
            statuses.pop(name, None)
            changed = True

        job = await asyncio.to_thread(self.owner.db.get_job, job_id)
        from shared.step_base import pipeline_digest_for

        raw_steps = self.owner.config.pipelines.get(pipeline, {}).get("steps", [])
        current_pipeline_digest = pipeline_digest_for(raw_steps)
        definition_changed = bool(
            self.owner.storage is not None
            and (changed or job is None or job.pipeline_digest != current_pipeline_digest)
        )
        stale: set[str] = set()
        # pipeline_digest 可能已在上一轮升级对账写成 current,但旧 generation 的
        # 执行随后才发布 terminal manifest。活跃终态必须每次绑定当前 definition,
        # 不能把 Job 行的摘要当作各步完成事实。
        if self.owner.storage is not None:
            for name, cfg in steps.items():
                if statuses.get(name) not in ("done", "skipped"):
                    continue
                manifest_status, _manifest = await current_manifest(name, cfg)
                if manifest_status != statuses[name]:
                    stale.add(name)
        if stale:
            stale_roots = [
                name for name in stale
                if not any(
                    name in self._get_downstream(steps, other)
                    for other in stale if other != name
                )
            ]
            reset_steps: set[str] = set()
            for root in stale_roots:
                reset_steps.add(root)
                reset_steps.update(self._get_downstream(steps, root))
            await self._reset_stale_pipeline_steps(
                job_id, steps, statuses, sorted(reset_steps), manifest_cache,
            )
            changed = True

        if definition_changed:
            await asyncio.to_thread(
                self.owner.db.update_job,
                job_id,
                pipeline_digest=current_pipeline_digest,
            )

        if changed:
            # 先把当前 ready 降回 waiting,再删旧 member。本轮主循环按新依赖重新入队;
            # 若直接保留 ready,_check_downstream 不会重投,任务会从队列永久消失。
            for name in sorted(set(statuses) & set(steps)):
                if statuses[name] != "ready":
                    continue
                if not await self.owner.redis.cas_step_status(
                    job_id, name, "ready", "waiting",
                ):
                    current = await self.owner.redis.get_step_status(job_id, name)
                    if current is not None:
                        statuses[name] = current
                    continue
                await asyncio.to_thread(
                    self.owner.db.update_step, job_id, name, status="waiting",
                )
                statuses[name] = "waiting"
            await self.owner.redis.remove_job_tasks(job_id)
            logger.warning(
                "active_pipeline_step_set_aligned",
                job_id=job_id,
                current_steps=len(steps),
                retained_running_legacy=sorted(set(statuses) - set(steps)),
            )
        return statuses

    async def _reset_stale_pipeline_steps(
        self,
        job_id: str,
        steps: dict[str, dict],
        statuses: dict[str, str],
        reset_steps: list[str],
        manifest_cache: dict[str, tuple[str | None, dict | None]],
    ) -> None:
        """换代并清理旧 definition 的 owned 产物,下游统一回 waiting。"""
        from shared.step_manifest import manifest_relative_path
        from shared.step_scope import part_id_from_scope

        self.owner._cancel_delayed_tasks(job_id)
        for name, status in list(statuses.items()):
            if status == "running" and name in steps and name not in reset_steps:
                reset_steps.append(name)
        removed_running = [
            name for name, status in statuses.items()
            if status == "running" and name not in steps
        ]
        for name in [*reset_steps, *removed_running]:
            await self.owner._revoke_step_execution(job_id, name)
        generation = await self.owner.redis.advance_job_generation(job_id)
        fresh = await self.owner.redis.get_all_step_statuses(job_id)
        for name, status in fresh.items():
            if status not in ("running", "failed"):
                continue
            step_generation = await self.owner.redis.get_step_generation(job_id, name)
            if step_generation == generation:
                continue
            await self.owner._revoke_step_execution(job_id, name)
            if name in steps:
                if name not in reset_steps:
                    reset_steps.append(name)
            elif name not in removed_running:
                removed_running.append(name)
        for name in reset_steps:
            cfg = steps[name]
            prefix = (
                f"parts/{part_id_from_scope(cfg['scope_key'])}/"
                if part_id_from_scope(cfg["scope_key"])
                else ""
            )
            _status, manifest = manifest_cache.get(name, (None, None))
            owned = set(cfg.get("outputs") or [])
            if manifest is not None:
                owned.update(entry["path"] for entry in manifest.get("outputs") or [])
            if self.owner.storage is not None:
                for rel in sorted(owned):
                    await self.owner.storage.delete_file(job_id, f"{prefix}{rel}")
                await self.owner.storage.delete_file(
                    job_id,
                    manifest_relative_path(cfg["scope_key"], cfg["template_step"]),
                )
                await self.owner.storage.delete_file(
                    job_id, f"{prefix}.{cfg['template_step']}.done",
                )
            done_file = (
                self.owner.jobs_dir / job_id / prefix / f".{cfg['template_step']}.done"
            )
            await asyncio.to_thread(done_file.unlink, True)
            await self.owner.redis.set_step_status(job_id, name, "waiting")
            await self.owner.redis.reset_step_retries(job_id, name)
            await asyncio.to_thread(
                self.owner.db.update_step,
                job_id,
                name,
                status="waiting",
                error=None,
                started_at=None,
                finished_at=None,
                duration_sec=None,
            )
            statuses[name] = "waiting"
        for name in removed_running:
            await self.owner.redis.delete_step_status(job_id, name)
            await asyncio.to_thread(self.owner.db.delete_step, job_id, name)
            statuses.pop(name, None)
        await asyncio.to_thread(
            self.owner.db.update_job, job_id, status="processing", error=None,
        )
        logger.warning(
            "active_pipeline_definition_restarted",
            job_id=job_id,
            reset_steps=reset_steps,
        )

    async def _check_downstream(self, job_id: str) -> None:
        """检查所有 waiting/skipped 步骤是否可推进。生产路径由 on_step_done 调用。"""
        pipeline = await self.owner.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        steps = await self.owner._get_job_pipeline_steps(job_id)
        if not steps:
            return
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
        statuses = await self._align_pipeline_step_set(job_id, steps, statuses)
        info = await self.owner.redis.get_job_info(job_id)
        try:
            flags = json.loads(info.get("flags") or "{}")
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            flags = {}
        mechanical_only = flags.get("mechanical_only") is True

        # YAML 只表达依赖关系,不保证 key 按拓扑排序。跳过一个 AI/条件步骤后必须重新扫描,
        # 否则声明在依赖之前的下游会永久停在 waiting。每轮只在真实状态转换时继续。
        while True:
            progressed = False
            for name, cfg in steps.items():
                status = statuses.get(name)
                if status not in ("waiting", "skipped"):
                    continue

                deps = cfg.get("depends_on", [])
                if not all(statuses.get(d) in ("done", "skipped") for d in deps):
                    continue

                # 纯机械模式按执行池建硬边界。不能依赖步骤名清单,否则新增 AI 步会静默漏网。
                if mechanical_only and cfg.get("pool") == "ai":
                    if status == "waiting":
                        await self.owner.redis.set_step_status(job_id, name, "skipped")
                        await asyncio.to_thread(
                            self.owner.db.update_step, job_id, name, status="skipped",
                        )
                        await self.owner.redis.publish(f"events:{job_id}", {
                            "event": "step_skipped", "step": name,
                            "reason": "mechanical_only",
                        })
                        # 确定性 skip 是持久完成事实(§2.8),签发 skipped manifest。
                        await self._publish_skipped_manifest(
                            job_id, cfg, "mechanical_only",
                        )
                        statuses[name] = "skipped"
                        progressed = True
                    continue

                conditional = self.owner._step_is_conditional(cfg)
                if conditional and not await self.owner._eval_step_condition(job_id, cfg):
                    if status == "waiting":
                        await self.owner.redis.set_step_status(job_id, name, "skipped")
                        await asyncio.to_thread(
                            self.owner.db.update_step, job_id, name, status="skipped",
                        )
                        await self.owner.redis.publish(f"events:{job_id}", {
                            "event": "step_skipped", "step": name,
                        })
                        # rules/condition 对当前证据确定性求值为 false:同上签发。
                        await self._publish_skipped_manifest(job_id, cfg, "rule_false")
                        statuses[name] = "skipped"
                        progressed = True
                    continue

                if status == "skipped":
                    # allowed_failure 是本代际的持久终态。该步同时带 rules 时再次求值
                    # 仍可能为 true,但不得因此把失败分支复活并重复消耗外部资源。
                    if cfg.get("allow_failure") is True:
                        continue
                    if not conditional:
                        continue
                    ok = await self.owner.redis.cas_step_status(
                        job_id, name, "skipped", "ready",
                    )
                    if not ok:
                        continue
                if not await self.owner.enqueue_step(job_id, name):
                    statuses[name] = "failed"
                    return
                statuses[name] = "ready"
                progressed = True
            if not progressed:
                break

        fresh = await self.owner.redis.get_all_step_statuses(job_id)
        if fresh and all(v in ("done", "skipped") for v in fresh.values()):
            await self.owner.mark_job_done(job_id)
        elif fresh:
            # 死锁打破器:仅当剩余未完成步骤全部为 ready(无 running、无 waiting)才介入。
            not_done = {k: v for k, v in fresh.items() if v not in ("done", "skipped")}
            all_remaining_ready = bool(not_done) and all(
                v == "ready" for v in not_done.values()
            )
            if all_remaining_ready:
                pipeline = await self.owner.redis.get_job_pipeline(job_id)
                if pipeline:
                    steps_cfg = await self.owner._get_job_pipeline_steps(job_id) or {}
                    pool_ok: dict[str, bool] = {}  # 同 pool 只查一次,免逐步重复扫 worker
                    for step_name in not_done:
                        pool = steps_cfg.get(step_name, {}).get("pool", "")
                        if pool not in pool_ok:
                            pool_ok[pool] = await self.owner._pool_has_workers(pool)
                        if pool_ok[pool]:
                            continue
                        # 缺 Worker 只 skip 条件步(可选步缺能力=合理跳过);必需步保留 ready,
                        # 由 check_no_worker 周期告警并等待能力恢复,避免不完整却显示完成。
                        if not self.owner._step_is_conditional(steps_cfg.get(step_name, {})):
                            continue
                        # CAS 保护 ready 到 skipped 的转换:若该步骤刚被 worker 抢成 running,
                        # CAS 失败时放弃 skip,避免覆盖在途执行.
                        if not await self.owner.redis.cas_step_status(
                            job_id, step_name, "ready", "skipped"
                        ):
                            continue
                        logger.info(
                            "skip_no_worker", job_id=job_id,
                            step=step_name, pool=pool,
                        )
                        await asyncio.to_thread(
                            self.owner.db.update_step, job_id, step_name, status="skipped",
                        )
                        await self.owner.redis.publish(f"events:{job_id}", {
                            "event": "step_skipped", "step": step_name,
                            "reason": f"no workers in pool '{pool}'",
                        })
                    fresh2 = await self.owner.redis.get_all_step_statuses(job_id)
                    if fresh2 and all(v in ("done", "skipped") for v in fresh2.values()):
                        await self.owner.mark_job_done(job_id)

    async def _publish_skipped_manifest(
        self,
        job_id: str,
        cfg: dict,
        reason: str,
        *,
        condition_evidence: dict | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """scheduler 签发确定性 skipped manifest(§2.8);失败由调用方按策略收敛。

        no_worker 跳过绝不进入这里:环境性 skip 无 manifest,重启对账时重新求值。
        """
        if self.owner.storage is None:
            return False
        try:
            from shared.runner_ops import parse_style_tags
            from shared.step_completion import (
                build_skipped_manifest,
                step_definition_digest_for,
            )
            from shared.step_manifest import manifest_relative_path
            from shared.version import FLORI_VERSION

            from shared.step_manifest import canonical_digest

            pipeline = await self.owner.redis.get_job_pipeline(job_id)
            info = await self.owner.redis.get_job_info(job_id)
            definition_digest = step_definition_digest_for(
                pipeline, cfg, config=self.owner.config,
                domain=info.get("domain", "general"),
                style_tags=parse_style_tags(info.get("style_tags", "[]")),
            )
            generation = await self.owner.redis.get_job_generation(job_id)
            if expected_generation is not None and generation != expected_generation:
                return False
            # 证据摘要:rule_false 绑定规则语义块与求值证据(命中文件集+flags);
            # mechanical_only 以 flags 摘要充 condition 证据。可与重放求值交叉核对。
            try:
                flags = json.loads(info.get("flags") or "{}")
            except (TypeError, ValueError):
                flags = {}
            rule_digest = None
            condition_digest = None
            if reason == "rule_false":
                semantic_rules = {
                    "rules": cfg.get("rules"), "condition": cfg.get("condition"),
                }
                rule_digest = canonical_digest(semantic_rules)
                import fnmatch as _fnmatch

                files = await self.owner._list_job_files(
                    job_id, cfg.get("part_id"),
                )
                globs = [
                    rule.get("exists") for rule in (cfg.get("rules") or [])
                    if isinstance(rule, dict) and rule.get("exists")
                ]
                evidence = sorted({
                    item for item in files
                    for pattern in globs if _fnmatch.fnmatch(item, pattern)
                })
                condition_digest = canonical_digest(
                    {"flags": flags, "matched_files": evidence},
                )
            elif reason == "mechanical_only":
                condition_digest = canonical_digest({"flags": flags})
            elif reason == "allowed_failure":
                condition_digest = canonical_digest(condition_evidence or {})
            _manifest, encoded = build_skipped_manifest(
                job_id=job_id,
                scope_key=cfg["scope_key"],
                step=cfg["template_step"],
                part_index=cfg.get("part_index"),
                job_generation=generation,
                reason_code=reason,
                definition_digest=definition_digest,
                rule_digest=rule_digest,
                condition_digest=condition_digest,
                flori_version=FLORI_VERSION,
            )
            await self.owner.storage.write_file(
                job_id,
                manifest_relative_path(cfg["scope_key"], cfg["template_step"]),
                encoded,
            )
            # 对象存储与 Redis 无跨系统事务。发布后再验代际;若并发 rerun 已换代,
            # manifest 会被恢复端按 generation 忽略,旧失败不能跳过新一轮执行。
            return expected_generation is None or (
                await self.owner.redis.get_job_generation(job_id)
                == expected_generation
            )
        except Exception:
            logger.warning(
                "skipped_manifest_publish_failed",
                job_id=job_id, step=cfg.get("name"), reason=reason, exc_info=True,
            )
            return False

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
        pipeline = await self.owner.redis.get_job_pipeline(job_id)
        if not pipeline:
            return 0
        steps = await self.owner._get_job_pipeline_steps(job_id) or {}
        steps_config = list(steps.values())
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
        progress = self.owner._calc_progress(steps_config, statuses)
        await asyncio.to_thread(
            self.owner.db.update_job, job_id, progress_pct=progress,
        )
        return progress
