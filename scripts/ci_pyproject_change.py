#!/usr/bin/env python3
"""判断 pyproject 是否有版本字段之外的语义变化."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from typing import Any


VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def project_version(data: dict[str, Any]) -> str | None:
    """返回可按发布规则识别的三段式版本."""
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    if isinstance(version, str) and VERSION_PATTERN.fullmatch(version):
        return version
    return None


def has_relevant_change(base_content: str, head_content: str) -> bool:
    """返回除合法 project.version 值、格式和注释外是否存在变化."""
    base_data = tomllib.loads(base_content)
    head_data = tomllib.loads(head_content)
    if project_version(base_data) is not None and project_version(head_data) is not None:
        base_data["project"] = {
            key: value for key, value in base_data["project"].items() if key != "version"
        }
        head_data["project"] = {
            key: value for key, value in head_data["project"].items() if key != "version"
        }
    return base_data != head_data


def git_file(revision: str) -> str:
    """读取 revision 中的 pyproject;失败由调用方按需保守处理."""
    result = subprocess.run(
        ["git", "show", f"{revision}:pyproject.toml"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: ci_pyproject_change.py <base-revision> <head-revision>", file=sys.stderr)
        return 2

    try:
        changed = has_relevant_change(git_file(argv[1]), git_file(argv[2]))
    except (OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        # 基线缺失或文件异常时宁可多构建一次,也不能漏掉依赖变化.
        print(f"pyproject comparison failed, rebuilding backend: {exc}", file=sys.stderr)
        changed = True

    print("true" if changed else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
