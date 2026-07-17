"""统一 Job 与 Part 步骤的执行身份编码。"""

from __future__ import annotations

import re
import hashlib


JOB_SCOPE = "job"
PART_SCOPE_PREFIX = "part:"
_SEPARATOR = "::"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def stable_part_id(job_id: str, part_index: int) -> str:
    """从 Job 身份和稳定序号生成可重复的非路径 Part ID。"""
    if not _SEGMENT_RE.fullmatch(job_id) or part_index < 1:
        raise ValueError("invalid job part identity")
    digest = hashlib.sha256(f"{job_id}:part:{part_index}".encode()).hexdigest()[:20]
    return f"pt_{digest}"


def part_scope(part_id: str) -> str:
    """把受控 part_id 转成 scope_key,非法路径片段直接拒绝。"""
    if not _SEGMENT_RE.fullmatch(part_id):
        raise ValueError("invalid part_id")
    return f"{PART_SCOPE_PREFIX}{part_id}"


def part_id_from_scope(scope_key: str) -> str | None:
    if scope_key == JOB_SCOPE:
        return None
    if not scope_key.startswith(PART_SCOPE_PREFIX):
        raise ValueError("invalid scope_key")
    part_id = scope_key[len(PART_SCOPE_PREFIX):]
    if not _SEGMENT_RE.fullmatch(part_id):
        raise ValueError("invalid scope_key")
    return part_id


def execution_step_key(scope_key: str, step_name: str) -> str:
    """生成 Redis/租约使用的唯一步骤键,Job scope 保持简洁。"""
    if not _SEGMENT_RE.fullmatch(step_name):
        raise ValueError("invalid step_name")
    part_id_from_scope(scope_key)
    if scope_key == JOB_SCOPE:
        return step_name
    return f"{scope_key}{_SEPARATOR}{step_name}"


def parse_execution_step(value: str) -> tuple[str, str]:
    """解析内部执行键;不接受多分隔符或任意 scope。"""
    if _SEPARATOR not in value:
        if not _SEGMENT_RE.fullmatch(value):
            raise ValueError("invalid execution step")
        return JOB_SCOPE, value
    if value.count(_SEPARATOR) != 1:
        raise ValueError("invalid execution step")
    scope_key, step_name = value.split(_SEPARATOR, 1)
    execution_step_key(scope_key, step_name)
    return scope_key, step_name
