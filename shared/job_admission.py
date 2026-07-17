"""Job 入口把流水线静态需求投影为可执行 Worker 门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared.ai_routing import (
    step_required_route_tags,
    step_task_tags,
    worker_satisfies_requirements,
)
from shared.status import ONLINE_BUSY, ONLINE_IDLE, compute_worker_status


@dataclass(frozen=True)
class StepRequirement:
    name: str
    pool: str
    required_tags: frozenset[str]
    task_tags: frozenset[str]


def _may_run(step: dict, flags: dict[str, bool]) -> bool:
    """仅排除 flags 已确定跳过的分支;产物条件尚未知时仍视为可达。"""
    if flags.get("mechanical_only", False) and step.get("pool") == "ai":
        return False
    if step.get("condition"):
        return True
    rules = step.get("rules")
    if not rules:
        return True
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if "exists" in rule:
            return True
        flag = rule.get("if_flag")
        if flag is not None and not flags.get(str(flag), False):
            continue
        when = rule.get("when", "on")
        return when not in {False, "skip"}
    return True


def pipeline_requirements(
    config: Any,
    pipeline: str,
    *,
    source: str,
    url: str | None,
    domain: str,
    style_tags: list[str],
    flags: dict[str, bool],
) -> list[StepRequirement]:
    """返回入口时可能运行的步骤;仅 flags 确定跳过的分支可排除。"""
    body = config.pipelines.get(pipeline)
    if not isinstance(body, dict):
        raise ValueError("pipeline must be an object")
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("pipeline steps must be a non-empty list")
    pools = (config.pools or {}).get("pools")
    if not isinstance(pools, dict) or not pools:
        raise ValueError("worker pools must be configured")
    net_steps = set((config.net_routing or {}).get("net_steps") or {"01_download", "07_danmaku"})
    out: list[StepRequirement] = []
    seen_names: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("pipeline step must be an object")
        name = step.get("name")
        pool = step.get("pool")
        if not isinstance(name, str) or not name or name in seen_names:
            raise ValueError("pipeline step name is missing or duplicate")
        seen_names.add(name)
        if not isinstance(pool, str) or pool not in pools:
            raise ValueError("pipeline step pool is invalid")
        if not _may_run(step, flags):
            continue
        required = set(step_required_route_tags(
            step, config.providers, source=source, url=url or "", net_steps=net_steps,
        ))
        task_tags = set(step_task_tags(
            step, domain=domain, style_tags=style_tags, required_tags=required,
        ))
        out.append(StepRequirement(
            name=name,
            pool=pool,
            required_tags=frozenset(required),
            task_tags=frozenset(task_tags),
        ))
    if not out:
        raise ValueError("pipeline has no reachable steps")
    return out


def worker_can_run(
    worker: dict,
    requirement: StepRequirement,
    *,
    online_window_sec: int,
    stale_window_sec: int,
) -> bool:
    raw_heartbeat = worker.get("last_heartbeat")
    try:
        heartbeat = datetime.fromisoformat(raw_heartbeat) if raw_heartbeat else None
    except (TypeError, ValueError):
        heartbeat = None
    status = compute_worker_status(
        heartbeat,
        worker.get("current_job") or None,
        worker.get("admin_status"),
        online_window_sec=online_window_sec,
        stale_window_sec=stale_window_sec,
    )
    if status not in {ONLINE_IDLE, ONLINE_BUSY}:
        return False
    if not worker_satisfies_requirements(
        worker, requirement.pool, requirement.required_tags,
    ):
        return False
    reject_raw = worker.get("reject_tags", "")
    if not isinstance(reject_raw, str):
        return False
    rejected = {part.strip() for part in reject_raw.split(",") if part.strip()}
    return not rejected.intersection(requirement.task_tags)


def workers_cover_pipeline(
    workers: list[dict], requirements: list[StepRequirement], config: Any,
) -> bool:
    status_cfg = (config.pools or {}).get("worker_status") or {}
    online = int(status_cfg.get("online_window_sec", 30))
    stale = int(status_cfg.get("stale_window_sec", 900))
    return all(any(
        worker_can_run(
            worker, requirement,
            online_window_sec=online,
            stale_window_sec=stale,
        )
        for worker in workers
    ) for requirement in requirements)
