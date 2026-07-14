"""验证调度职责拆分保持显式 façade 与纯规划语义。"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduler.background import BackgroundServices
from scheduler.dag_planner import DagPlanner
from scheduler.effects import EffectDispatcher
from scheduler.job_finalizer import JobFinalizer
from scheduler.lifecycle import LifecycleCoordinator
from scheduler.recovery import RecoveryCoordinator
from scheduler.scheduler import Scheduler
from scheduler.task_router import TaskRouter


def _reference_downstream(steps: dict[str, dict], root: str) -> set[str]:
    found: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        for name, cfg in steps.items():
            if name not in found and current in cfg.get("depends_on", []):
                found.add(name)
                pending.append(name)
    return found


def test_dag_planner_matches_reference_for_random_acyclic_graphs():
    planner = DagPlanner(SimpleNamespace())
    rng = random.Random(20260715)
    for size in range(2, 30):
        names = [f"s{i}" for i in range(size)]
        steps = {
            name: {
                "depends_on": [
                    candidate for candidate in names[:index]
                    if rng.random() < 0.18
                ],
            }
            for index, name in enumerate(names)
        }
        for root in names:
            assert set(planner._get_downstream(steps, root)) == _reference_downstream(
                steps, root,
            )


def test_components_are_explicit_and_do_not_use_dynamic_forwarding():
    source = Path("scheduler/scheduler.py").read_text()
    assert "__getattr__" not in source
    for component in (
        DagPlanner,
        TaskRouter,
        LifecycleCoordinator,
        RecoveryCoordinator,
        EffectDispatcher,
        JobFinalizer,
        BackgroundServices,
    ):
        assert component.__name__ in source


def test_new_scheduler_submodules_do_not_import_api_package():
    for name in (
        "background.py",
        "dag_planner.py",
        "effects.py",
        "job_finalizer.py",
        "lifecycle.py",
        "recovery.py",
        "task_router.py",
    ):
        source = (Path("scheduler") / name).read_text()
        assert "from api" not in source
        assert "import api" not in source


@pytest.mark.asyncio
async def test_effect_dispatcher_unknown_action_fails_closed():
    owner = SimpleNamespace(storage=object())
    dispatcher = EffectDispatcher(owner)
    assert await dispatcher._run_completion_effects(
        "j1", "s1", [{"action": "unknown"}],
    ) is False


def test_scheduler_keeps_all_compatibility_entrypoints():
    expected = {
        "run", "shutdown", "submit_job", "on_step_started", "on_step_done",
        "on_step_failed", "enqueue_step", "mark_job_done", "mark_job_failed",
        "orphan_scan", "reconcile_slots", "check_stuck", "check_no_worker",
        "rerun", "resubmit", "_check_downstream", "_dispatch",
    }
    assert expected <= set(vars(Scheduler))
