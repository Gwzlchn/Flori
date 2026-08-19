#!/usr/bin/env python3
"""分类 pyproject 的运行时变化与项目版本变化."""

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


def has_project_version_change(base_content: str, head_content: str) -> bool:
    """返回 project.version 字段是否变化,包含删除与非法值变化."""
    base_data = tomllib.loads(base_content)
    head_data = tomllib.loads(head_content)
    base_project = base_data.get("project")
    head_project = head_data.get("project")
    base_version = base_project.get("version") if isinstance(base_project, dict) else None
    head_version = head_project.get("version") if isinstance(head_project, dict) else None
    return base_version != head_version


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
    kind = "relevant"
    revisions = argv[1:]
    if len(argv) == 5 and argv[1:3] == ["--kind", "version"]:
        kind = "version"
        revisions = argv[3:]
    elif len(argv) != 3:
        revisions = []
    if len(revisions) != 2:
        print(
            "usage: ci_pyproject_change.py [--kind version] "
            "<base-revision> <head-revision>",
            file=sys.stderr,
        )
        return 2

    try:
        base_content = git_file(revisions[0])
        head_content = git_file(revisions[1])
        if kind == "version":
            changed = has_project_version_change(base_content, head_content)
        else:
            changed = has_relevant_change(base_content, head_content)
    except (OSError, subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        # 基线缺失或文件异常时宁可多构建一次,也不能漏掉运行时或版本变化.
        print(f"pyproject comparison failed, rebuilding product images: {exc}", file=sys.stderr)
        changed = True

    print("true" if changed else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
