#!/usr/bin/env python3
"""在 pytest collection 前按完整测试文件均衡 CI 分片。"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from pathlib import Path
from typing import NoReturn


EXCLUDED_NORMAL_FILES = {
    "test_canonical_evidence_e2e.py",
    "test_worker.py",
}


def normal_test_files(repo: Path) -> list[str]:
    """枚举普通测试文件，worker 和 integration 套件由独立 job 持有。"""
    tests = repo / "tests"
    return [
        path.relative_to(repo).as_posix()
        for path in sorted(tests.glob("test_*.py"))
        if not path.name.startswith("test_step_")
        and path.name not in EXCLUDED_NORMAL_FILES
    ]


def duration_weights(
    durations_path: Path,
    files: list[str],
) -> dict[str, float]:
    """聚合 nodeid 时长；未知文件用已有文件中位数，避免新测试聚成长尾。"""
    try:
        raw = json.loads(durations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 pytest 时长文件: {durations_path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"pytest 时长文件必须是 JSON object: {durations_path}")

    known = set(files)
    totals = {path: 0.0 for path in files}
    seen: set[str] = set()
    for nodeid, value in raw.items():
        if not isinstance(nodeid, str) or not isinstance(value, (int, float)):
            continue
        path = nodeid.split("::", 1)[0]
        if path in known and value >= 0:
            totals[path] += float(value)
            seen.add(path)

    positive = [totals[path] for path in seen if totals[path] > 0]
    fallback = statistics.median(positive) if positive else 1.0
    return {
        path: totals[path] if path in seen and totals[path] > 0 else fallback
        for path in files
    }


def build_file_shards(
    repo: Path,
    durations_path: Path,
    splits: int,
) -> list[list[str]]:
    """用确定性 LPT 分配完整文件，保证各组并集完整且互不重叠。"""
    if splits < 1:
        raise ValueError("CI shard 数量必须是正整数")
    files = normal_test_files(repo)
    if not files:
        raise ValueError("未找到普通测试文件")
    if splits > len(files):
        raise ValueError(f"CI shard 数量 {splits} 超过普通测试文件数 {len(files)}")

    weights = duration_weights(durations_path, files)
    groups: list[list[str]] = [[] for _ in range(splits)]
    totals = [0.0] * splits
    for path in sorted(files, key=lambda item: (-weights[item], item)):
        group = min(range(splits), key=lambda index: (totals[index], index))
        groups[group].append(path)
        totals[group] += weights[path]
    for group in groups:
        group.sort()
    return groups


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
        shards = build_file_shards(args.repo, args.durations, args.splits)
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
        f">> CI normal file shard {args.group}/{args.splits}: "
        f"{len(selected)} files",
        flush=True,
    )
    os.execvp(command[0], [*command, *selected])


if __name__ == "__main__":
    main()
