"""step 完成权威读取:manifest 优先、.done fallback 的 dual 模式单一来源(§2.11 阶段A)。

STEP_COMPLETION_MODE=dual|manifest-only:dual(默认)下 manifest 是首选权威、缺失时
退回 .done 既有语义;manifest-only 代码路径就位,缺失/损坏即降级,不读 .done。
本模块同时收敛 worker/api/scheduler/backfill 共用的语义定义摘要计算,防三处漂移。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Mapping

from .config import AppConfig, load_domain_profile
from .step_manifest import (
    MANIFEST_FORMAT,
    MANIFEST_FORMAT_VERSION,
    ManifestError,
    canonical_digest,
    compute_input_digest,
    manifest_relative_path,
    validate_manifest,
)
from .step_semantic_definition import (
    SemanticDefinitionError,
    step_semantic_definition_digest,
)

MODE_DUAL = "dual"
MODE_MANIFEST_ONLY = "manifest-only"

# 确定性 skip 的 reason code(§2.8);no_worker 属环境性,schema 层拒绝持久化。
DETERMINISTIC_SKIP_REASONS = frozenset({"mechanical_only", "rule_false", "capability_downgrade"})


_warned_invalid_mode: set[str] = set()


def completion_mode() -> str:
    """迁移开关:环境值非法时告警一次后保守回 dual(既有行为),不 fail 调度器启动。"""
    mode = os.environ.get("STEP_COMPLETION_MODE", MODE_DUAL).strip().lower()
    if mode in (MODE_DUAL, MODE_MANIFEST_ONLY):
        return mode
    if mode not in _warned_invalid_mode:
        _warned_invalid_mode.add(mode)
        import structlog

        structlog.get_logger(component="step_completion").warning(
            "invalid_step_completion_mode", value=mode, fallback=MODE_DUAL,
        )
    return MODE_DUAL


def step_definition_digest_for(
    pipeline: str,
    raw_step: Mapping[str, object],
    *,
    config: AppConfig,
    domain: str,
    style_tags: list | None,
) -> str:
    """当前语义定义摘要(worker 生产/api 过期判定/scheduler 对账/backfill 同一算式)。

    config_digests 组成必须与 worker 侧写 manifest 时逐字节一致,否则同一定义
    会在读写两端产生两个摘要,全部产物被误判过期。
    """
    domain_cfg = load_domain_profile(config.config_dir, domain)
    config_digests = {
        "domain_profile": canonical_digest({"name": domain, **domain_cfg}),
        "style_tags": canonical_digest(list(style_tags or [])),
    }
    return step_semantic_definition_digest(
        pipeline=pipeline, step_config=dict(raw_step), config_digests=config_digests,
    )


def semantic_digest_for_version(
    pipeline: str,
    raw_step: Mapping[str, object],
    version: object,
    *,
    config: AppConfig,
    domain: str,
    style_tags: list | None,
) -> str:
    """按指定 version 重算语义摘要(provenance 版本边界门枚举历史版本用)。"""
    overridden = {**dict(raw_step), "version": str(version)}
    return step_definition_digest_for(
        pipeline, overridden, config=config, domain=domain, style_tags=style_tags,
    )


async def read_valid_manifest(storage, job_id: str, scope_key: str, step: str) -> dict | None:
    """读取并校验已发布 manifest;缺失/损坏返回 None(调用方按模式决定 fallback 或降级)。"""
    try:
        rel = manifest_relative_path(scope_key, step)
        raw = await storage.read_file(job_id, rel)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        validate_manifest(data)
    except (TypeError, ValueError, ManifestError):
        return None
    return data


async def verify_manifest_outputs_metadata(storage, job_id: str, manifest: dict) -> bool:
    """按 manifest 声明集做元数据级验证(size 精确匹配)。

    完整 SHA 重验交 backfill/verify 工具与首次全量哈希门;对账热路径用 size +
    对象存在性,避免启动时全库重读大文件(§2.7 memo 取舍,详见工具的 verify 子命令)。
    """
    scope_key = manifest["scope"]["scope_key"]
    part_id = manifest["scope"]["part_id"]
    prefix = f"parts/{part_id}/" if part_id else ""
    for entry in manifest["outputs"]:
        try:
            size = await storage.file_size(job_id, f"{prefix}{entry['path']}")
        except Exception:
            return False
        if size != entry["size_bytes"]:
            return False
    return True


def build_skipped_manifest(
    *,
    job_id: str,
    scope_key: str,
    step: str,
    part_index: int | None,
    job_generation: int,
    reason_code: str,
    definition_digest: str,
    input_fingerprints: dict[str, str] | None = None,
    rule_digest: str | None = None,
    condition_digest: str | None = None,
    flori_version: str = "0",
) -> tuple[dict, bytes]:
    """scheduler 侧签发确定性 skipped manifest(§2.8);环境性 skip 拒绝进入。"""
    if reason_code not in DETERMINISTIC_SKIP_REASONS:
        raise ManifestError(f"skip reason {reason_code!r} is not a durable completion fact")
    from .step_scope import part_id_from_scope

    part_id = part_id_from_scope(scope_key)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fingerprints = dict(input_fingerprints or {})
    manifest = {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "job_id": job_id,
        "scope": {
            "kind": "job" if part_id is None else "part",
            "scope_key": scope_key,
            "part_id": part_id,
            "part_index": part_index if part_id is not None else None,
        },
        "step": step,
        "outcome": "skipped",
        "execution": {
            # skip 无 worker 执行:确定性合成身份,标注签发主体。
            "exec_id": f"scheduler-skip:{uuid.uuid4().hex}",
            "job_generation": job_generation,
            "attempt": 1,
            "started_at": now,
            "committed_at": now,
            "duration_sec": 0.0,
        },
        "compatibility": {
            "input_fingerprints": fingerprints,
            "input_digest": compute_input_digest(fingerprints),
            "definition_digest": definition_digest,
        },
        "producer": {
            "flori_version": flori_version,
            "build_sha": os.environ.get("FLORI_GIT_COMMIT") or None,
            "worker_id": None,
            "runner": "scheduler",
            "image": None,
            "image_digest": None,
            "tool_versions": {},
            "kind": "scheduler_skip",
        },
        "outputs": [],
        "skip": {
            "reason_code": reason_code,
            "rule_digest": rule_digest,
            "condition_digest": condition_digest,
        },
    }
    encoded = validate_manifest(manifest)
    return manifest, encoded


__all__ = [
    "DETERMINISTIC_SKIP_REASONS",
    "MODE_DUAL",
    "MODE_MANIFEST_ONLY",
    "SemanticDefinitionError",
    "build_skipped_manifest",
    "completion_mode",
    "read_valid_manifest",
    "semantic_digest_for_version",
    "step_definition_digest_for",
    "verify_manifest_outputs_metadata",
]
