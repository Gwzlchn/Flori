#!/usr/bin/env python3
"""从已成功 CI 的候选 SHA 中选择当前 HEAD 的最近可发布基线."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def git_is_ancestor(base: str, head: str) -> bool:
    """返回 base 是否为 head 祖先;Git 异常按非祖先处理."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def git_distance(base: str, head: str) -> int | None:
    """返回 base 到 head 的提交距离;无法计算时返回 None."""
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..{head}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def select_change_base(
    head: str,
    candidates: Iterable[str],
    *,
    is_ancestor: Callable[[str, str], bool] = git_is_ancestor,
    distance: Callable[[str, str], int | None] = git_distance,
) -> str | None:
    """选择可验证祖先中距离 HEAD 最近的完整成功 SHA."""
    ancestors: list[tuple[int, str]] = []
    for raw in candidates:
        candidate = raw.strip().lower()
        if candidate == head or not SHA_PATTERN.fullmatch(candidate):
            continue
        if is_ancestor(candidate, head):
            commit_distance = distance(candidate, head)
            if commit_distance is not None:
                ancestors.append((commit_distance, candidate))
    return min(ancestors)[1] if ancestors else None


def main(argv: list[str], lines: Iterable[str] = sys.stdin) -> int:
    if len(argv) != 2 or not SHA_PATTERN.fullmatch(argv[1].lower()):
        print("usage: ci_select_change_base.py <head-sha>", file=sys.stderr)
        return 2
    selected = select_change_base(argv[1].lower(), lines)
    if selected:
        print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
