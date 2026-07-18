"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
from collections import deque

import structlog


if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

class DagPlanner:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def _check_downstream(self, job_id: str) -> None:
        """检查所有 waiting/skipped 步骤是否可推进。生产路径由 on_step_done 调用。"""
        pipeline = await self.owner.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        steps = await self.owner._get_job_pipeline_steps(job_id)
        if not steps:
            return
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
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
                        # 缺 worker 只 skip 条件步(可选步缺能力=合理跳过);必需步不 skip,留给
                        # check_no_worker 超宽限 fail-fast,避免末端必需步被静默 skip 后 job
                        # 不完整却显示完成(对齐 pools.yaml fail-fast 注释)。
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
        self, job_id: str, cfg: dict, reason: str,
    ) -> None:
        """scheduler 签发确定性 skipped manifest(§2.8);best-effort,失败不阻调度。

        no_worker 跳过绝不进入这里:环境性 skip 无 manifest,重启对账时重新求值。
        """
        if self.owner.storage is None:
            return
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
        except Exception:
            logger.warning(
                "skipped_manifest_publish_failed",
                job_id=job_id, step=cfg.get("name"), reason=reason, exc_info=True,
            )

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
