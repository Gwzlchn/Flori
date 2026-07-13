#!/usr/bin/env python3
"""判断 docker/ 变更是否会影响构建产物."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys


PARSER_DIRECTIVE_PATTERN = re.compile(r"^\s*#\s*(?:syntax|escape|check)\s*=", re.IGNORECASE)


def is_dockerfile(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    return name == "Dockerfile" or name.endswith(".Dockerfile")


def semantic_dockerfile(content: str) -> str:
    """去掉普通整行注释;解析器指令和 heredoc 保守视为语义内容."""
    lines = content.splitlines()
    if any("<<" in line for line in lines if not line.lstrip().startswith("#")):
        return content

    semantic: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") and not PARSER_DIRECTIVE_PATTERN.match(line):
            continue
        semantic.append(line)
    return "\n".join(semantic)


def has_relevant_change(path: str, base_content: str, head_content: str) -> bool:
    """返回单个 docker/ 文件变更是否需要重建后端."""
    if not is_dockerfile(path):
        return True
    return semantic_dockerfile(base_content) != semantic_dockerfile(head_content)


def changed_paths(base: str, head: str, pathspecs: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base, head, "--", *pathspecs],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def git_file(revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: ci_docker_change.py <base-revision> <head-revision> [pathspec ...]",
            file=sys.stderr,
        )
        return 2

    pathspecs = argv[3:] or ["docker"]
    try:
        relevant = any(
            has_relevant_change(path, git_file(argv[1], path), git_file(argv[2], path))
            for path in changed_paths(argv[1], argv[2], pathspecs)
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        # 文件新增/删除,基线缺失或 Git 异常时宁可多构建,不能漏发.
        print(f"docker comparison failed, rebuilding backend: {exc}", file=sys.stderr)
        relevant = True

    print("true" if relevant else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
