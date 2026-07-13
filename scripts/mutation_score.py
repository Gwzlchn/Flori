#!/usr/bin/env python3
"""真实变异分数驱动,区分被测试杀死与基础设施失败.

mutmut 3.x 的自带 runner 在本仓库布局下不会从 mutants/ 激活变异代码.
本脚本设置 MUTANT_UNDER_TEST,从 mutants/ 对每个变异体运行相关测试.
只有 pytest 退出码 1 表示断言杀死变异体;中断,内部错误,用法错误,
未收集用例等都属于 infra-error,整次测量失败且不持久化分数.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


GEN_BASELINE = ["tests/test_net.py"]
MUTANT_TIMEOUT_SECONDS = int(os.environ.get("MUTATION_TEST_TIMEOUT", "300"))

TARGETS: dict[str, list[str]] = {
    "shared.ai_gateway": ["tests/test_ai_gateway.py"],
    "shared.db": ["tests/test_db.py"],
    "scheduler": [
        "tests/test_scheduler.py",
        "tests/test_runner_ops.py",
        "tests/test_pipeline_config.py",
    ],
    "worker": ["tests/test_worker.py", "tests/test_transport.py"],
}

_MUTANT_DEF = re.compile(
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+(x[\wǁ]+__mutmut_\d+)[ \t]*\(",
    re.M,
)
_SEL = re.compile(r"pytest_add_cli_args_test_selection = \[[^\]]*\]")


class MutationOutcome(str, Enum):
    KILLED = "killed"
    SURVIVED = "survived"
    INFRA_ERROR = "infra-error"


@dataclass
class MutationCounts:
    killed: int = 0
    survived: int = 0
    infra_error: int = 0

    @property
    def valid_total(self) -> int:
        return self.killed + self.survived

    @property
    def valid(self) -> bool:
        return self.infra_error == 0


@dataclass
class TargetResult:
    prefix: str
    counts: MutationCounts
    detail: str | None = None


def classify_pytest_exit_code(returncode: int) -> MutationOutcome:
    """保守分类 pytest 退出码,避免基础设施故障虚高变异分数."""
    if returncode == 0:
        return MutationOutcome.SURVIVED
    if returncode == 1:
        return MutationOutcome.KILLED
    return MutationOutcome.INFRA_ERROR


def _set_generation_selection() -> str:
    """临时把 mutmut 生成阶段切到最快基线,返回原 pyproject 以便还原."""
    path = pathlib.Path("pyproject.toml")
    original = path.read_text()
    selection = ", ".join(repr(item) for item in [*GEN_BASELINE, "-m", "not fuzz"])
    updated, replacements = _SEL.subn(
        f"pytest_add_cli_args_test_selection = [{selection}]", original
    )
    if replacements != 1:
        raise RuntimeError("mutmut test selection config was not found exactly once")
    path.write_text(updated)
    return original


def _generate_mutants(prefix: str) -> None:
    """从干净 mutants/ 生成目标变异体,异常时也必须还原 pyproject."""
    mutants_dir = pathlib.Path("mutants")
    if mutants_dir.exists():
        shutil.rmtree(mutants_dir)
    original = _set_generation_selection()
    try:
        # mutmut 自身的存活结果不用于计分.
        # 非零只表示生成阶段未可靠完成.
        # 生成结果还要经过非空 mutants/ + clean baseline 二次校验.
        result = subprocess.run(
            ["mutmut", "run", prefix + "*"],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"mutmut generation failed with exit code {result.returncode}")
    finally:
        pathlib.Path("pyproject.toml").write_text(original)


def _enumerate_mutants(prefix: str) -> list[str]:
    """从生成源码枚举 `{module}.{mutant_func}`."""
    ids: list[str] = []
    for path in sorted(pathlib.Path("mutants").rglob("*.py")):
        relative = path.relative_to("mutants")
        if relative.parts[0] == "tests":
            continue
        module = ".".join(relative.with_suffix("").parts)
        if not (module == prefix or module.startswith(prefix + ".")):
            continue
        for match in _MUTANT_DEF.finditer(path.read_text()):
            ids.append(f"{module}.{match.group(1)}")
    return ids


def _run_pytest(tests: list[str], mutant_id: str | None = None) -> int:
    env = {**os.environ}
    if mutant_id is not None:
        env["MUTANT_UNDER_TEST"] = mutant_id
    else:
        env.pop("MUTANT_UNDER_TEST", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"],
        cwd="mutants",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=MUTANT_TIMEOUT_SECONDS,
    )
    return result.returncode


def score_mutants(
    mutant_ids: list[str],
    *,
    run_mutant: Callable[[str], int],
) -> MutationCounts:
    counts = MutationCounts()
    for mutant_id in mutant_ids:
        try:
            outcome = classify_pytest_exit_code(run_mutant(mutant_id))
        except (OSError, subprocess.TimeoutExpired):
            outcome = MutationOutcome.INFRA_ERROR
        if outcome is MutationOutcome.KILLED:
            counts.killed += 1
        elif outcome is MutationOutcome.SURVIVED:
            counts.survived += 1
        else:
            counts.infra_error += 1
    return counts


def measure_mutants(
    mutant_ids: list[str],
    *,
    run_baseline: Callable[[], int],
    run_mutant: Callable[[str], int],
) -> tuple[MutationCounts, str | None]:
    """先校验 clean baseline,再计算变异体三态结果."""
    if not mutant_ids:
        return MutationCounts(infra_error=1), "no mutants generated"
    try:
        baseline_returncode = run_baseline()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return MutationCounts(infra_error=1), f"baseline launch failed: {exc}"
    if baseline_returncode != 0:
        return (
            MutationCounts(infra_error=1),
            f"clean baseline failed with pytest exit code {baseline_returncode}",
        )
    counts = score_mutants(mutant_ids, run_mutant=run_mutant)
    detail = None if counts.valid else f"{counts.infra_error} mutant test runs had infra errors"
    return counts, detail


def select_targets(only: str | None) -> dict[str, list[str]]:
    if not only:
        return TARGETS
    return {prefix: tests for prefix, tests in TARGETS.items() if only in prefix}


def _score_percent(counts: MutationCounts) -> float:
    return 100.0 * counts.killed / counts.valid_total if counts.valid_total else 0.0


def _print_report(results: list[TargetResult]) -> None:
    total = MutationCounts(
        killed=sum(result.counts.killed for result in results),
        survived=sum(result.counts.survived for result in results),
        infra_error=sum(result.counts.infra_error for result in results),
    )

    print("\n变异分数汇总(基础设施错误不计 killed)")
    for result in results:
        counts = result.counts
        print(
            f"  {result.prefix:22s} killed={counts.killed:4d} survived={counts.survived:4d}"
            f" infra={counts.infra_error:3d} total={counts.valid_total:4d}"
            f" score={_score_percent(counts):5.1f}%"
        )
        if result.detail:
            print(f"    infra detail: {result.detail}")
    print(
        f"  {'TOTAL':22s} killed={total.killed:4d} survived={total.survived:4d}"
        f" infra={total.infra_error:3d} total={total.valid_total:4d}"
        f" score={_score_percent(total):.1f}%"
    )

    print("\n<!--MUTATION-SUMMARY-->")
    print("### 变异分数(killed/有效总数;基础设施错误使测量失败)")
    print("| 模块 | killed | survived | infra-error | valid total | score |")
    print("|---|---:|---:|---:|---:|---:|")
    for result in results:
        counts = result.counts
        print(
            f"| `{result.prefix}` | {counts.killed} | {counts.survived} |"
            f" {counts.infra_error} | {counts.valid_total} | {_score_percent(counts):.1f}% |"
        )
    print(
        f"| **TOTAL** | **{total.killed}** | **{total.survived}** |"
        f" **{total.infra_error}** | **{total.valid_total}** | **{_score_percent(total):.1f}%** |"
    )
    print("<!--/MUTATION-SUMMARY-->")

    if total.valid:
        print("<!--MUTATION-CSV-->")
        for result in results:
            counts = result.counts
            print(f"{result.prefix},{counts.killed},{counts.survived}")
        print("<!--/MUTATION-CSV-->")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    if len(args) > 2:
        print("usage: mutation_score.py [target-substring]", file=sys.stderr)
        return 2
    only = args[1].strip() if len(args) == 2 else None
    selected = select_targets(only or None)
    if not selected:
        print(f"unknown mutation target: {only}", file=sys.stderr)
        return 2

    results: list[TargetResult] = []
    for prefix, tests in selected.items():
        print(f"mutation scoring: {prefix}  (tests: {' '.join(tests)})", flush=True)
        try:
            _generate_mutants(prefix)
            mutant_ids = _enumerate_mutants(prefix)
            counts, detail = measure_mutants(
                mutant_ids,
                run_baseline=lambda tests=tests: _run_pytest(tests),
                run_mutant=lambda mutant_id, tests=tests: _run_pytest(tests, mutant_id),
            )
        except (OSError, RuntimeError) as exc:
            counts = MutationCounts(infra_error=1)
            detail = f"mutation setup failed: {exc}"
        results.append(TargetResult(prefix, counts, detail))

    _print_report(results)
    return 0 if all(result.counts.valid for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
