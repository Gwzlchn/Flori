"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import fnmatch
import json
from datetime import datetime, timezone

import structlog

from shared.ai_routing import (
    InvalidAIOverrideError,
    parse_ai_override,
    step_required_route_tags,
    step_required_capability_tags,
    step_task_tags,
    worker_satisfies_requirements,
)
from shared.runner_ops import parse_style_tags
from shared.source_detect import detect_source
from shared.storage import read_file_bounded

if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

# 只有需要外网的步骤才附加网络区域硬标签。
_NET_STEPS = {"01_download", "07_danmaku"}

# 步骤静态优先级加权(分数 -= boost;zpopmin 越小越先)。02_whisper 防饿死(出稿硬依赖它)。
_PRIORITY_BOOST = {"02_whisper": 100}

class TaskRouter:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def enqueue_step(self, job_id: str, step_name: str) -> bool:
        pipeline_steps = await self.owner._get_job_pipeline_steps(job_id)
        if not pipeline_steps:
            return False
        step_cfg = pipeline_steps.get(step_name)
        if not step_cfg:
            return False

        pool = step_cfg["pool"]

        job_info = await self.owner.redis.get_job_info(job_id) if pool == "ai" else None

        # 网络区域路由(net-zone):任务分发时按 URL 判区域,require 对应 net-cn / net-global tag.
        # 只有自报覆盖该区域的 worker 才能认领;境外内容走香港或带代理 worker,大陆内容走大陆 worker.
        # 代理 HOW 全在 worker。区域判定与 tag 语义见文件头 _NET_STEPS 注释与 shared.net_zone。
        nr = self.owner.config.net_routing or {}
        net_steps = set(nr.get("net_steps") or _NET_STEPS)
        info = job_info if pool == "ai" else None
        try:
            require_tags = await self.owner._required_tags_for_step(
                job_id, step_name, step_cfg, info,
            )
        except InvalidAIOverrideError as exc:
            await self.owner._fail_invalid_ai_override(job_id, step_name, str(exc))
            return False
        merged_tags = step_task_tags(
            step_cfg,
            domain=(job_info or {}).get("domain", ""),
            style_tags=parse_style_tags((job_info or {}).get("style_tags", "[]")),
            required_tags=require_tags,
        )

        await self.owner.redis.set_step_status(job_id, step_name, "ready")
        statuses = await self.owner.redis.get_all_step_statuses(job_id)
        done_count = sum(1 for v in statuses.values() if v in ("done", "skipped"))
        # zpopmin:分数越小越先出。priority=-done_count 让晚到步骤优先,但 02_whisper 处在链路早段
        # 会被各 job 的视觉步(04/05/06,同 cpu 池)长期抢占而饿死。给它静态加权(更小分数)抢先转写;
        # 出稿硬依赖转写,不加权会让出稿步空等。
        priority = -done_count - _PRIORITY_BOOST.get(
            step_cfg.get("template_step", step_name), 0,
        )

        await self.owner.redis.enqueue_step(
            pool, job_id, step_name, merged_tags, priority,
            require_tags=require_tags,
            resources=step_cfg.get("resources") or [],
        )

        await asyncio.to_thread(
            self.owner.db.update_step, job_id, step_name, status="ready",
        )
        await self.owner.redis.publish(f"events:{job_id}", {
            "event": "step_ready", "step": step_name,
        })

        logger.info("step_enqueued", job_id=job_id, step=step_name, pool=pool, priority=priority)
        return True

    async def _fail_invalid_ai_override(
        self, job_id: str, step_name: str, reason: str,
    ) -> None:
        """非法 override 在入队前终止步骤与 job,不允许回退到 pipeline provider。"""
        error = (
            reason if reason.startswith("invalid AI override:")
            else f"invalid AI override: {reason}"
        )
        await self.owner.redis.set_step_status(job_id, step_name, "failed")
        await asyncio.to_thread(
            self.owner.db.update_step, job_id, step_name,
            status="failed", error=error[:500],
            finished_at=datetime.now(timezone.utc),
        )
        await self.owner.mark_job_failed(job_id, f"{step_name}: {error}")

    async def _required_tags_for_step(
        self, job_id: str, step_name: str, step_cfg: dict,
        job_info: dict | None = None,
    ) -> list[str]:
        """计算 enqueue 与 no-worker 共用的硬标签,含 provider override。"""
        template_step = step_cfg.get("template_step", step_name)
        part_id = step_cfg.get("part_id")
        artifact_prefix = f"parts/{part_id}/" if part_id else ""
        required = set(step_cfg.get("tags") or [])
        override = None
        capability_tags: set[str] = set()
        if step_cfg.get("pool") == "ai":
            async def has_nonempty_artifact(rel: str) -> bool:
                if self.owner.storage is not None:
                    data = await read_file_bounded(
                        self.owner.storage, job_id, f"{artifact_prefix}{rel}", 0,
                    )
                    return bool(data)
                path = self.owner.jobs_dir / job_id / artifact_prefix / rel
                try:
                    return await asyncio.to_thread(
                        lambda: path.is_file() and path.stat().st_size > 0,
                    )
                except OSError as exc:
                    raise InvalidAIOverrideError(
                        "invalid AI capability input: artifact_unreadable",
                    ) from exc

            try:
                capability_tags = set(await step_required_capability_tags(
                    step_cfg, has_nonempty_artifact,
                ))
            except InvalidAIOverrideError:
                raise
            except (OSError, ValueError, TypeError) as exc:
                raise InvalidAIOverrideError(
                    "invalid AI capability input",
                ) from exc
            raw = None
            if self.owner.storage is not None:
                try:
                    raw = await self.owner.storage.read_file(job_id, "job.json")
                except (OSError, ValueError, TypeError) as exc:
                    logger.warning("ai_override_read_failed", job_id=job_id, step=step_name)
                    raise InvalidAIOverrideError(
                        "invalid AI override: job_json_unreadable",
                    ) from exc
            else:
                path = self.owner.jobs_dir / job_id / "job.json"
                try:
                    raw = await asyncio.to_thread(path.read_bytes)
                except FileNotFoundError:
                    raw = None
                except OSError as exc:
                    raise InvalidAIOverrideError(
                        "invalid AI override: job_json_unreadable",
                    ) from exc
            if raw is not None:
                try:
                    doc = json.loads(raw)
                    override, shape_error = parse_ai_override(
                        doc, template_step, self.owner.config.providers,
                    )
                    if shape_error:
                        logger.warning(
                            "ai_override_invalid", job_id=job_id,
                            step=step_name, reason=shape_error,
                        )
                        raise InvalidAIOverrideError(f"invalid AI override: {shape_error}")
                except InvalidAIOverrideError:
                    raise
                except (ValueError, json.JSONDecodeError, TypeError) as exc:
                    logger.warning("ai_override_read_failed", job_id=job_id, step=step_name)
                    raise InvalidAIOverrideError(
                        "invalid AI override: job_json_invalid",
                    ) from exc
        nr = self.owner.config.net_routing or {}
        net_steps = set(nr.get("net_steps") or _NET_STEPS)
        info = job_info
        if template_step in net_steps:
            info = info or await self.owner.redis.get_job_info(job_id)
        info = info or {}
        if part_id:
            parts = await asyncio.to_thread(self.owner.db.get_parts, job_id)
            part = next((item for item in parts if item.id == part_id), None)
            if part is None:
                raise InvalidAIOverrideError("invalid part scope")
            part_url = part.source_url or ""
            info = {
                **info,
                "url": part_url,
                "source": str((part.meta or {}).get("source") or "")
                or detect_source(part_url),
            }
        source = (info.get("source") or "").strip() or detect_source(info.get("url", ""))
        try:
            return step_required_route_tags(
                {**step_cfg, "name": template_step}, self.owner.config.providers,
                source=source, url=info.get("url", ""),
                net_steps=net_steps,
                override=override, capability_tags=capability_tags,
            )
        except ValueError as exc:
            raise InvalidAIOverrideError(
                f"invalid AI capability: {exc}",
            ) from exc

    async def _list_job_files(
        self, job_id: str, part_id: str | None = None,
    ) -> list[str]:
        """列出 job 现有产物的相对路径。分布式部署产物在对象存储(MinIO)、不在调度器本地盘,
        故优先走 storage;无 storage(单机/测试)回退本地 jobs_dir。条件/规则据此判存在。"""
        if self.owner.storage is not None:
            try:
                files = await self.owner.storage.list_files(job_id)
                if part_id is None:
                    return files
                prefix = f"parts/{part_id}/"
                return [item[len(prefix):] for item in files if item.startswith(prefix)]
            except Exception:
                logger.warning("list_job_files_failed", job_id=job_id)
                return []
        job_dir = self.owner.jobs_dir / job_id
        if part_id:
            job_dir = job_dir / "parts" / part_id

        def _local() -> list[str]:
            if not job_dir.exists():
                return []
            return [p.relative_to(job_dir).as_posix() for p in job_dir.rglob("*") if p.is_file()]

        return await asyncio.to_thread(_local)

    async def check_condition(
        self, job_id: str, condition: str, part_id: str | None = None,
    ) -> bool:
        files = await self.owner._list_job_files(job_id, part_id)
        has_srt = any(fnmatch.fnmatch(f, "input/*.srt") for f in files)
        has_ass = any(fnmatch.fnmatch(f, "input/*.ass") for f in files)
        if condition == "no_subtitle":
            return not has_srt
        if condition == "has_subtitle":
            return has_srt
        if condition == "has_danmaku":
            return has_ass
        return True

    def _step_is_conditional(self, cfg: dict) -> bool:
        """step 是否带跳过条件:condition 字符串或声明式 rules 均算。"""
        return bool(cfg.get("condition") or cfg.get("rules"))

    async def _eval_step_condition(self, job_id: str, cfg: dict) -> bool:
        """求值 step 是否应运行:优先 condition 字符串,否则用声明式 rules。"""
        condition = cfg.get("condition")
        part_id = cfg.get("part_id")
        if condition:
            if part_id:
                return await self.owner.check_condition(job_id, condition, part_id)
            return await self.owner.check_condition(job_id, condition)
        rules = cfg.get("rules")
        if rules:
            if part_id:
                return await self.owner._eval_rules(job_id, rules, part_id)
            return await self.owner._eval_rules(job_id, rules)
        return True

    async def _eval_rules(
        self, job_id: str, rules: list, part_id: str | None = None,
    ) -> bool:
        """声明式 rules 求值器:自上而下首条命中生效,命中 when=skip 则跳过,
        支持 exists(相对 job 根的 glob)与 if_flag(投递开关),无命中默认运行。
        存在性查 storage(产物在 MinIO,不在调度器本地盘);if_flag 查 redis job info。"""
        files = await self.owner._list_job_files(job_id, part_id)
        _flags_cache: dict | None = None

        async def _flags() -> dict:
            nonlocal _flags_cache
            if _flags_cache is None:
                info = await self.owner.redis.get_job_info(job_id)
                try:
                    _flags_cache = json.loads(info.get("flags") or "{}")
                except (json.JSONDecodeError, ValueError, AttributeError):
                    _flags_cache = {}
            return _flags_cache

        def _when(rule: dict) -> str:
            when = rule.get("when", "on")
            if when is True:
                return "on"
            if when is False:
                return "skip"
            return str(when)

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            glob = rule.get("exists")
            if glob is not None:
                hit = any(fnmatch.fnmatch(f, glob) for f in files)
                if not hit:
                    continue
            # if_flag:投递开关为真才命中(假则本条不生效,落到后续兜底规则)。
            flag = rule.get("if_flag")
            if flag is not None:
                if not (await _flags()).get(flag):
                    continue
            # exists/if_flag 命中、或无条件的兜底规则:本条生效。
            return _when(rule) != "skip"
        return True

    async def _pool_has_workers(self, pool: str) -> bool:
        """检查某个 pool 是否有可认领新任务的 worker;排除 paused/offline.
        claim_step 对 paused 直接拒认领;若 pool 只剩 paused,no-worker 判定/死锁打破器会误判为可推进,
        ready 步既无人认领又不被 fail-fast/skip,永久卡 ready。
        暂停态算"无可用 worker":暂停期下载好的 job 进到该池会等候,超 NO_WORKER_GRACE_SEC 才 fail。"""
        workers = await self.owner.redis.list_worker_ids()
        for wid in workers:
            info = await self.owner.redis.get_worker_info(wid)
            if not info:
                continue
            if info.get("admin_status") == "paused" or info.get("status") == "offline":
                continue
            if pool in info.get("pools", "").split(","):
                return True
        return False

    async def _pool_has_workers_for(self, pool: str, require_tags: list[str]) -> bool:
        """同 _pool_has_workers,但额外要求在线 worker 的 tags 满足 require_tags(硬门控)。
        require_tags 为空时等价 _pool_has_workers;check_no_worker 若只看池不看 tag,
        池有 worker 但无人满足 require_tags 时(如境外内容 require net-global 却无覆盖全球的
        worker)会躲过 fail-fast、永久卡 ready 且无报错;用本函数后超 NO_WORKER_GRACE_SEC
        给明确失败。"""
        req = {t for t in (require_tags or []) if t}
        workers = await self.owner.redis.list_worker_ids()
        for wid in workers:
            info = await self.owner.redis.get_worker_info(wid)
            if worker_satisfies_requirements(info, pool, req):
                return True
        return False
