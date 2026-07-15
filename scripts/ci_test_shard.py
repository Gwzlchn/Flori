#!/usr/bin/env python3
"""在全量 collection 前以文件和巨型文件 nodeid 均衡 CI 分片。"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
from pathlib import Path
from typing import NoReturn


EXCLUDED_NORMAL_FILES = {
    "test_canonical_evidence_e2e.py",
    "test_worker.py",
}

# pytest collection、fixture 和 xdist 调度对每个 item 都有固定成本，call duration 不包含它。
NODE_SCHEDULING_WEIGHT_SECONDS = 0.2
MIN_NODE_SPLIT_COUNT = 32


def normal_test_files(repo: Path) -> list[str]:
    """枚举普通测试文件，worker 和 integration 套件由独立 job 持有。"""
    tests = repo / "tests"
    return [
        path.relative_to(repo).as_posix()
        for path in sorted(tests.glob("test_*.py"))
        if not path.name.startswith("test_step_")
        and path.name not in EXCLUDED_NORMAL_FILES
    ]


def load_durations(durations_path: Path) -> dict[str, float]:
    """读取有效非负 nodeid 时长；文件损坏必须阻断分片。"""
    try:
        raw = json.loads(durations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 pytest 时长文件: {durations_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"pytest 时长文件必须是 JSON object: {durations_path}")
    return {
        nodeid: float(value)
        for nodeid, value in raw.items()
        if isinstance(nodeid, str)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    }


def file_duration_weights(
    durations: dict[str, float],
    files: list[str],
) -> dict[str, float]:
    """聚合文件权重；未知文件用已知文件中位数，避免新测试聚成长尾。"""
    known = set(files)
    totals = {path: 0.0 for path in files}
    seen: set[str] = set()
    for nodeid, value in durations.items():
        path = nodeid.split("::", 1)[0]
        if path in known:
            totals[path] += value + NODE_SCHEDULING_WEIGHT_SECONDS
            seen.add(path)

    positive = [totals[path] for path in seen if totals[path] > 0]
    fallback = statistics.median(positive) if positive else 1.0
    return {
        path: totals[path] if path in seen and totals[path] > 0 else fallback
        for path in files
    }


def file_node_counts(
    durations: dict[str, float],
    files: list[str],
) -> dict[str, int]:
    """统计历史 node 数，供大量极短用例文件避免原子长杆。"""
    counts = {path: 0 for path in files}
    for nodeid in durations:
        path = nodeid.split("::", 1)[0]
        if path in counts:
            counts[path] += 1
    return counts


def heavy_test_files(
    weights: dict[str, float],
    node_counts: dict[str, int],
    splits: int,
) -> list[str]:
    """按耗时或 node 数接近半组负载的文件需拆开，避免原子长杆。"""
    duration_target = sum(weights.values()) / splits
    node_target = max(MIN_NODE_SPLIT_COUNT, sum(node_counts.values()) / splits / 2)
    return sorted(
        path
        for path, weight in weights.items()
        if weight > duration_target or node_counts[path] > node_target
    )


def collect_nodeids(repo: Path, files: list[str]) -> dict[str, list[str]]:
    """只 collection 巨型文件，返回实际 nodeid 以覆盖新增和参数化变化。"""
    if not files:
        return {}
    completed = subprocess.run(
        [
            "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "-m", "not fuzz", "--disable-warnings", *files,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
        suffix = f": {detail[0]}" if detail else ""
        raise ValueError(f"巨型测试文件 collection 失败{suffix}")

    result = {path: [] for path in files}
    prefixes = {path: f"{path}::" for path in files}
    for line in completed.stdout.splitlines():
        nodeid = line.strip()
        for path, prefix in prefixes.items():
            if nodeid.startswith(prefix):
                result[path].append(nodeid)
                break
    missing = [path for path, nodeids in result.items() if not nodeids]
    if missing:
        raise ValueError(f"巨型测试文件未 collection 到 nodeid: {', '.join(missing)}")
    flattened = [nodeid for nodeids in result.values() for nodeid in nodeids]
    if len(flattened) != len(set(flattened)):
        raise ValueError("巨型测试文件 collection 返回重复 nodeid")
    return result


def build_hybrid_shards(
    repo: Path,
    durations_path: Path,
    splits: int,
    collected: dict[str, list[str]] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """轻文件保持原子，巨型文件按实际 nodeid 与它们共同做确定性 LPT。"""
    if splits < 1:
        raise ValueError("CI shard 数量必须是正整数")
    files = normal_test_files(repo)
    if not files:
        raise ValueError("未找到普通测试文件")
    if splits > len(files):
        raise ValueError(f"CI shard 数量 {splits} 超过普通测试文件数 {len(files)}")

    durations = load_durations(durations_path)
    file_weights = file_duration_weights(durations, files)
    node_counts = file_node_counts(durations, files)
    heavy = heavy_test_files(file_weights, node_counts, splits)
    if collected is None:
        collected = collect_nodeids(repo, heavy)
    if set(collected) != set(heavy):
        raise ValueError("巨型测试文件 collection 集合与计划不一致")

    positive_nodes = [value for value in durations.values() if value > 0]
    node_fallback = (
        statistics.median(positive_nodes) if positive_nodes else 1.0
    ) + NODE_SCHEDULING_WEIGHT_SECONDS
    items = [
        (path, file_weights[path])
        for path in files
        if path not in collected
    ]
    for path in heavy:
        nodeids = collected[path]
        if not nodeids:
            raise ValueError(f"巨型测试文件未 collection 到 nodeid: {path}")
        items.extend(
            (
                nodeid,
                durations.get(
                    nodeid,
                    node_fallback - NODE_SCHEDULING_WEIGHT_SECONDS,
                ) + NODE_SCHEDULING_WEIGHT_SECONDS,
            )
            for nodeid in nodeids
        )
    names = [name for name, _weight in items]
    if len(names) != len(set(names)):
        raise ValueError("CI shard 调度项重复")

    groups: list[list[str]] = [[] for _ in range(splits)]
    totals = [0.0] * splits
    for name, weight in sorted(items, key=lambda item: (-item[1], item[0])):
        group = min(range(splits), key=lambda index: (totals[index], index))
        groups[group].append(name)
        totals[group] += weight
    if any(not group for group in groups):
        raise ValueError("CI shard 产生空组")
    for group in groups:
        group.sort()
    return groups, heavy


def _fail(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("/app"))
    parser.add_argument("--durations", type=Path, default=Path("/app/.test_durations"))
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--splits", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        shards, heavy = build_hybrid_shards(
            args.repo,
            args.durations,
            args.splits,
        )
    except ValueError as exc:
        _fail(parser, str(exc))
    if args.group < 1 or args.group > args.splits:
        _fail(parser, f"CI shard 超出范围: {args.group}/{args.splits}")
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        _fail(parser, "必须提供 pytest 命令")

    selected = shards[args.group - 1]
    print(
        f">> CI normal hybrid shard {args.group}/{args.splits}: "
        f"{len(selected)} items, {len(heavy)} heavy files split by nodeid",
        flush=True,
    )
    os.execvp(command[0], [*command, *selected])


if __name__ == "__main__":
    main()
