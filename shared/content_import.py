"""便携内容仓库导入核心:plan -> 空库物化 -> 投影重建 -> 验收(设计稿 05 号 §2.9/§2.10)。

只实现 `--target empty` 分支;merge 冲突分类与 GC 属 P3b。本模块从 P1 仓库读,
向全新 SQLite + 对象存储写,journal 走独立文件(shared/content_import_journal)。

三条不变量贯穿全程:
- 状态只有一份。绝不从 snapshot 复制备份时的 jobs.status/job_steps.status,
  一律由当前 pipeline + 已恢复 manifest 重新投影(§2.9)。
- 对象发布顺序与 P2a 采集序对偶:先发布 outputs、最后发布 manifest。中途崩溃
  时没有 final manifest,下游不会误判可复用(§2.5.4)。
- 切换前失败不动原环境:新库与 staging 直接丢弃,journal 独立留存供排查(§2.10 阶段5)。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

import structlog

from .config import AppConfig, load_config
from .content_import_guard import (
    LiveTargetError,
    assert_write_authorized,
    create_import_storage,
    is_live_db,
    is_live_object_bucket,
    production_bucket,
)
from .content_policy import PolicyError
from .content_repository import (
    ContentRepository,
    RepositoryError,
    SNAPSHOT_RECORD_GROUPS,
)
from .content_result import (
    ResultDestination,
    ResultFileError,
    emit_result,
    ensure_output_roots_disjoint,
    prepare_result_destination,
)
from .db import SCHEMA_VERSION, Database
from .models import JobPart, Step, StepStatus
from .pipeline_scope import expand_pipeline_steps
from .content_import_journal import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_MATERIALIZING,
    STATUS_PREPARING,
    STATUS_PROJECTING,
    ContentImportJournal,
    ImportJournalError,
)
from .content_maintenance import (
    MaintenanceLockError,
    acquire_import_target_lease,
    path_resources,
)
from .step_completion import (
    DETERMINISTIC_SKIP_REASONS,
    read_valid_manifest,
    step_definition_digest_for,
    verify_manifest_outputs_metadata,
)
from .step_manifest import (
    ManifestError,
    canonical_digest,
    canonical_json_bytes,
    manifest_relative_path,
)
from .step_scope import execution_step_key, part_scope

_log = structlog.get_logger(__name__)

_CHUNK_SIZE = 1024 * 1024
# 物化阶段的 FK 闭包顺序(§2.10 阶段2);顺序即依赖,不得重排。
MATERIALIZE_ORDER = (
    "collection", "ingested_item",
    "job_core", "job_user_state", "part_core",
    "user_config",
    "step_result",
    "prompt_override", "prompt_override_version",
    # definition_version 必须先于 glossary:glossary.current_definition_version_id
    # 的触发器要求被指向的版本已存在且是该 term 的最大版本(v6 契约)。
    "definition_version", "glossary",
    "study",
    "ai_usage", "ai_task_log", "failure_event",
    "legacy_archive", "job_relation",
)

# 投影 reason:manifest 在但与当前定义不兼容,历史结果仍留仓库供审计(§2.10 阶段3-4)。
REASON_STALE_MANIFEST = "stale_manifest"
REASON_MISSING_MANIFEST = "missing_manifest"
# 上游未完成导致的压制;只用于 DAG 方向,不再兼作产物不符的原因。
REASON_UPSTREAM_INVALID = "upstream_invalid"
REASON_OUTPUT_MISMATCH = "output_mismatch"
REASON_UNKNOWN_PIPELINE = "unknown_pipeline"

# 默认写隔离 staging 而不是线上对象根:导入是重建演练,默认不该碰生产产物。
# 要写线上必须由入口脚本显式 --into-live 指定。
DEFAULT_IMPORT_STAGING = "/data/import-staging/jobs"
DEFAULT_CONFIG_STAGING = "/data/import-staging/prompts"
DEFAULT_LIVE_CONFIG_ROOT = "/data/prompts"
# journal 默认落在与任何目标库都无父子关系的稳定位置:阶段5 丢弃目标库目录时
# 不能把唯一的崩溃证据一起删掉。
DEFAULT_JOURNAL_PATH = "/data/content-import/journal.sqlite3"


class ImportError_(RuntimeError):
    """导入 fail-closed 错误;切换前抛出即代表原环境未被改动。"""


class _AlreadyImported(ImportError_):
    """幂等短路;专门的类型让 except 分支能跳过写 failed,不毁成功证据。"""


MODE_EMPTY = "empty"
MODE_MERGE = "merge"
PORTABLE_SNAPSHOT_V1 = "flori-portable-snapshot/v1"
PORTABLE_SNAPSHOT_V2 = "flori-portable-snapshot/v2"

ACTION_INSERT = "insert"
ACTION_NOOP = "noop"
ACTION_CONFLICT = "conflict"
ACTION_SKIP = "skip"

# 冲突类型(§2.9 merge 七条规则的判定结果)。
CONFLICT_JOB_IDENTITY = "job_identity"
CONFLICT_STEP_MANIFEST = "step_manifest"
CONFLICT_LEDGER = "immutable_ledger"
CONFLICT_USER_STATE = "user_state"

_GLOBAL_CONFIG_KINDS = frozenset({"prompts", "profiles", "styles", "templates"})
_JOB_AI_CONFIG_KEYS = frozenset({
    "ai_override", "ai_overrides", "prompt_override", "prompt_overrides", "ai_config",
})


@dataclass(frozen=True)
class MergeConflict:
    """一条可读的冲突记录:谁、哪条 record、哪种冲突、两边现值摘要。"""
    unit: str
    kind: str
    natural_key: str
    conflict: str
    target_digest: str | None
    snapshot_digest: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "unit": self.unit, "kind": self.kind, "natural_key": self.natural_key,
            "conflict": self.conflict, "target_digest": self.target_digest,
            "snapshot_digest": self.snapshot_digest, "detail": self.detail,
        }


@dataclass
class MergeClassification:
    """分类结果;materialize 只吃 clean 部分,冲突单元根本不写(不是写完回滚)。"""
    actions: dict[str, str] = field(default_factory=dict)
    conflicts: list[MergeConflict] = field(default_factory=list)
    conflicted_units: set[str] = field(default_factory=set)
    user_state_kept: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {
        ACTION_INSERT: 0, ACTION_NOOP: 0, ACTION_CONFLICT: 0, ACTION_SKIP: 0,
    })

    def clean_records(
        self, records: Sequence[tuple[str, str, dict]],
    ) -> list[tuple[str, str, dict]]:
        return [
            item for item in records
            if self.actions.get(item[1]) == ACTION_INSERT
        ]


def _unit_of(kind: str, body: Mapping) -> str:
    """原子单元键:Job 相关记录同属一个 Job 单元,账本行各自独立。

    §2.9 要求冲突时"整单元零修改";Job 的 core/Part 清单/step 结果必须同进同出,
    否则会留下半个 Job。
    """
    if kind in ("job_core", "job_user_state", "part_core", "step_result",
                "failure_event", "job_relation"):
        return f"job:{body.get('job_id') or body.get('id')}"
    if kind == "user_config" and body.get("kind") == "job_ai_config":
        parts = PurePosixPath(str(body.get("path") or "")).parts
        if len(parts) == 3 and parts[0] == "jobs":
            return f"job:{parts[1]}"
    return f"{kind}:{_natural_key_of(kind, body)}"


def _natural_key_of(kind: str, body: Mapping) -> str:
    """各 kind 的自然键;与备份侧序列化的身份字段一致。"""
    if kind == "job_core":
        return body["id"]
    if kind == "job_user_state":
        return body["job_id"]
    if kind == "part_core":
        return f"{body['job_id']}/{body['id']}"
    if kind == "step_result":
        return f"{body['job_id']}/{body['scope_key']}/{body['step']}"
    if kind == "failure_event":
        return body["exec_id"]
    if kind == "collection":
        return body["id"]
    if kind == "ingested_item":
        return f"{body['collection_id']}/{body['item_id']}"
    if kind == "glossary":
        return f"{body['domain']}/{body['term']}"
    if kind == "definition_version":
        return body["definition_version_id"]
    if kind in ("prompt_override", "prompt_override_version"):
        parts = [
            body["scope"], body.get("domain", ""), body.get("pipeline", ""),
            body.get("document_kind", ""), body["step"],
        ]
        if kind == "prompt_override_version":
            parts.append(str(body["version"]))
        return "/".join(parts)
    if kind == "study":
        row = body["row"]
        from .content_policy import STUDY_TABLE_PRIMARY_KEYS

        keys = STUDY_TABLE_PRIMARY_KEYS[body["table"]]
        return body["table"] + "/" + "/".join(str(row[column]) for column in keys)
    if kind == "ai_usage":
        return body["exec_id"]
    if kind == "ai_task_log":
        return f"{body['task_id']}/{body['created_at']}/{body['exec_id']}"
    if kind == "user_config":
        return body["path"]
    if kind == "legacy_archive":
        return f"{body['table']}#{body.get('chunk_index', 0)}"
    if kind == "job_relation":
        return body["job_id"]
    raise ImportError_(f"no natural key rule for record kind {kind!r}")


# 账本类 record 的目标侧取行方式:(表名, WHERE 子句, 取参数的函数)。
_LEDGER_LOOKUP: Mapping[str, tuple[str, str, Callable[[Mapping], tuple]]] = {
    "collection": ("collections", "id=?", lambda b: (b["id"],)),
    "ingested_item": (
        "ingested_items", "collection_id=? AND item_id=?",
        lambda b: (b["collection_id"], b["item_id"]),
    ),
    "glossary": ("glossary", "domain=? AND term=?", lambda b: (b["domain"], b["term"])),
    "definition_version": (
        "concept_definition_versions", "definition_version_id=?",
        lambda b: (b["definition_version_id"],),
    ),
    "prompt_override": (
        "prompt_overrides",
        "scope=? AND domain=? AND pipeline=? AND document_kind=? AND step=?",
        lambda b: (
            b["scope"], b.get("domain", ""), b.get("pipeline", ""),
            b.get("document_kind", ""), b["step"],
        ),
    ),
    "prompt_override_version": (
        "prompt_override_versions",
        "scope=? AND domain=? AND pipeline=? AND document_kind=? AND step=? AND version=?",
        lambda b: (
            b["scope"], b.get("domain", ""), b.get("pipeline", ""),
            b.get("document_kind", ""), b["step"], b["version"],
        ),
    ),
    "ai_usage": ("ai_usage", "exec_id=?", lambda b: (b["exec_id"],)),
    "ai_task_log": (
        "ai_task_logs", "task_id=? AND created_at=? AND exec_id=?",
        lambda b: (b["task_id"], b["created_at"], b["exec_id"]),
    ),
}


def _digest_of(kind: str, body: Mapping) -> str:
    """走 validate_record 同一条门算 digest,保证与仓库里的 record digest 可比。"""
    from .content_policy import validate_record

    return "sha256:" + hashlib.sha256(validate_record(kind, dict(body))).hexdigest()


def _target_ledger_digest(
    connection: sqlite3.Connection, kind: str, body: Mapping,
) -> str | None:
    """把目标库现有行按备份侧同一套 serializer 重新序列化后算 digest。

    "如果现在备份目标库,这条自然键会产出什么" —— 用它和 snapshot 里的
    record digest 直接比,免去逐字段写第二套比较逻辑(那必然与备份侧漂移)。
    """
    from .content_backup import _serialize_ledger_row

    lookup = _LEDGER_LOOKUP.get(kind)
    if lookup is None:
        return None
    table, where, params = lookup
    rows = connection.execute(
        f"SELECT * FROM {table} WHERE {where}", params(body),
    ).fetchall()
    # ai_task_logs 的历史 schema 只有自增 rowid,没有自然键唯一约束。
    # 导入侧必须显式要求复合键恰好一行,否则 INSERT OR IGNORE
    # 会在崩溃重放时悠然增殖审计记录。
    if kind == "ai_task_log" and len(rows) != 1:
        return None if not rows else "sha256:" + "0" * 64
    row = rows[0] if rows else None
    if row is None:
        return None
    target_kind, target_body = _serialize_ledger_row(table, row)
    try:
        return _digest_of(target_kind, target_body)
    except (PolicyError, ImportError_):
        return "sha256:" + "0" * 64  # 目标行不合当前 allowlist,视为不同


def _target_study_digest(
    connection: sqlite3.Connection, body: Mapping,
) -> str | None:
    from .content_backup import _serialize_ledger_row
    from .content_policy import STUDY_TABLE_PRIMARY_KEYS

    table = body["table"]
    keys = STUDY_TABLE_PRIMARY_KEYS[table]
    where = " AND ".join(f"{column}=?" for column in keys)
    row = connection.execute(
        f"SELECT * FROM {table} WHERE {where}",
        tuple(body["row"][column] for column in keys),
    ).fetchone()
    if row is None:
        return None
    target_kind, target_body = _serialize_ledger_row(table, row)
    try:
        return _digest_of(target_kind, target_body)
    except (PolicyError, ImportError_):
        return "sha256:" + "0" * 64


def _target_job_core_digest(
    connection: sqlite3.Connection, job_id: str,
) -> str | None:
    from .content_backup import _serialize_job_core

    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    try:
        return _digest_of("job_core", _serialize_job_core(row))
    except (PolicyError, ImportError_):
        return "sha256:" + "0" * 64


def _target_part_ids(connection: sqlite3.Connection, job_id: str) -> list[str]:
    return [
        row[0] for row in connection.execute(
            "SELECT id FROM job_parts WHERE job_id=? ORDER BY part_index", (job_id,),
        )
    ]


def _target_part_core_digest(
    connection: sqlite3.Connection, job_id: str, part_id: str,
    *, include_source_blob: bool = False,
) -> str | None:
    """Part 身份包含完整业务核心,不只是 ID 和顺序。"""
    from .content_backup import _serialize_part_core

    row = connection.execute(
        "SELECT * FROM job_parts WHERE job_id=? AND id=?", (job_id, part_id),
    ).fetchone()
    if row is None:
        return None
    try:
        body = _serialize_part_core(row)
        if include_source_blob:
            source_digest = body.get("source_digest")
            if not source_digest:
                return "sha256:" + "0" * 64
            body["source_blob"] = source_digest
        return _digest_of("part_core", body)
    except (PolicyError, ImportError_):
        return "sha256:" + "0" * 64


async def _target_step_result_digest(
    storage, job_id: str, scope_key: str, step: str,
) -> str | None:
    """目标库当前那一步的 manifest 若存在,算出等价 step_result digest。"""
    manifest = await read_valid_manifest(storage, job_id, scope_key, step)
    if manifest is None:
        return None
    try:
        return _digest_of("step_result", {
            "job_id": job_id, "scope_key": scope_key, "step": step,
            "manifest": manifest,
            "output_blobs": {
                entry["path"]: entry["sha256"] for entry in manifest["outputs"]
            },
        })
    except (PolicyError, ImportError_):
        return "sha256:" + "0" * 64


async def classify_merge(
    *,
    connection: sqlite3.Connection,
    storage,
    records: Sequence[tuple[str, str, dict]],
    repository: ContentRepository | None = None,
    config_root: Path | None = None,
    source_roots: Mapping[str, Path] | None = None,
    apply_user_state: bool = False,
) -> MergeClassification:
    """§2.9 merge 七条规则的判定;只读,不写任何东西。

    先整体分类再物化,是"冲突时整单元零修改"的实现方式:不是写完回滚,
    而是有冲突的单元根本不进 materialize。
    """
    result = MergeClassification()
    selected_config_root = Path(config_root or DEFAULT_CONFIG_STAGING)
    selected_source_roots = source_roots or {}
    snapshot_parts: dict[str, list[tuple[int, str]]] = {}
    for kind, digest, body in records:
        if kind == "part_core":
            snapshot_parts.setdefault(body["job_id"], []).append(
                (body["part_index"], body["id"])
            )

    for kind, digest, body in records:
        unit = _unit_of(kind, body)
        natural_key = _natural_key_of(kind, body)
        action = ACTION_INSERT
        conflict: MergeConflict | None = None

        if kind == "job_core":
            target = _target_job_core_digest(connection, body["id"])
            if target is not None:
                # 规则 5:Job core 或有序 Part 清单不同 -> 身份冲突,整 Job 拒绝。
                target_parts = _target_part_ids(connection, body["id"])
                snapshot_order = [
                    part_id for _index, part_id
                    in sorted(snapshot_parts.get(body["id"], []))
                ]
                if target != digest:
                    conflict = MergeConflict(
                        unit, kind, natural_key, CONFLICT_JOB_IDENTITY, target, digest,
                        "job immutable core differs from target",
                    )
                elif target_parts and target_parts != snapshot_order:
                    conflict = MergeConflict(
                        unit, kind, natural_key, CONFLICT_JOB_IDENTITY, target, digest,
                        f"ordered part list differs: target={target_parts} "
                        f"snapshot={snapshot_order}",
                    )
                else:
                    action = ACTION_NOOP
        elif kind == "part_core":
            target = _target_part_core_digest(
                connection, body["job_id"], body["id"],
                include_source_blob="source_blob" in body,
            )
            if target is not None:
                if target == digest:
                    action = ACTION_NOOP
                    if "source_blob" in body:
                        from .source_library import SourceReferenceError, parse_source_ref

                        try:
                            source_ref = parse_source_ref(body.get("source_ref"))
                        except SourceReferenceError:
                            source_ref = None
                        root = (
                            selected_source_roots.get(source_ref.root_id)
                            if source_ref is not None else None
                        )
                        if root is None:
                            action = ACTION_INSERT
                        else:
                            observed = await _sha256_local(
                                root, source_ref.relative_path,
                            )
                            if observed is None:
                                action = ACTION_INSERT
                            elif observed != body["source_blob"]:
                                conflict = MergeConflict(
                                    unit, kind, natural_key, CONFLICT_JOB_IDENTITY,
                                    observed, digest,
                                    "vendored source target exists with different content",
                                )
                else:
                    conflict = MergeConflict(
                        unit, kind, natural_key, CONFLICT_JOB_IDENTITY,
                        target, digest,
                        "part immutable core differs from target",
                    )
        elif kind == "job_user_state":
            # 规则 6:用户状态默认保留目标,仅显式 --apply-user-state 且前置匹配才改。
            row = connection.execute(
                "SELECT collection_id FROM jobs WHERE id=?", (body["job_id"],),
            ).fetchone()
            if row is None:
                action = ACTION_INSERT
            elif (row[0] or None) == (body.get("collection_id") or None):
                action = ACTION_NOOP
            elif apply_user_state and _user_state_precondition_ok(body, row[0]):
                action = ACTION_INSERT
            elif apply_user_state:
                # 给了开关但前置摘要对不上:目标在备份之后又被改过,覆盖会
                # 静默吞掉那次修改,按冲突处理而不是照改(§2.9-6)。
                conflict = MergeConflict(
                    unit, kind, natural_key, CONFLICT_USER_STATE,
                    _user_state_revision(row[0], body["job_id"]), digest,
                    "target user state changed after this snapshot was taken; "
                    "--apply-user-state refuses to overwrite an unseen edit",
                )
            else:
                action = ACTION_SKIP
                result.user_state_kept.append({
                    "job_id": body["job_id"],
                    "target_collection_id": row[0],
                    "snapshot_collection_id": body.get("collection_id"),
                })
        elif kind == "step_result":
            target = await _target_step_result_digest(
                storage, body["job_id"], body["scope_key"], body["step"],
            )
            if target is None:
                action = ACTION_INSERT  # 规则 3:单调补齐此前不存在的步
            elif target == digest:
                action = ACTION_NOOP
            else:
                # 规则 4:同一 active 步已有不同 manifest -> 冲突,整单元不覆盖。
                conflict = MergeConflict(
                    unit, kind, natural_key, CONFLICT_STEP_MANIFEST, target, digest,
                    "target already has a different manifest for this step",
                )
        elif kind == "study":
            target = _target_study_digest(connection, body)
            if target is not None:
                action, conflict = _ledger_verdict(
                    unit, kind, natural_key, target, digest,
                )
        elif kind in _LEDGER_LOOKUP:
            target = _target_ledger_digest(connection, kind, body)
            if target is not None:
                action, conflict = _ledger_verdict(
                    unit, kind, natural_key, target, digest,
                )
        elif kind == "user_config":
            target: str | None
            config_kind = body["kind"]
            if config_kind in _GLOBAL_CONFIG_KINDS:
                try:
                    rel = _Materializer._validated_config_path(
                        config_kind, body["path"],
                    )
                except ImportError_:
                    rel = ""
                observed = await _sha256_local(selected_config_root, rel) if rel else None
                target = None if observed is None else (
                    digest if observed == body["blob"] else observed
                )
            elif config_kind == "domain_config":
                prefix, _, rel = body["path"].rpartition("/")
                observed = await _sha256_object(storage, prefix, rel)
                target = None if observed is None else (
                    digest if observed == body["blob"] else observed
                )
            elif config_kind == "job_ai_config":
                path = PurePosixPath(body["path"])
                raw = (
                    await storage.read_file(path.parts[1], "job.json")
                    if len(path.parts) == 3 else None
                )
                try:
                    expected = json.loads(repository.read_blob(body["blob"])) \
                        if repository is not None else None
                    observed_doc = json.loads(raw) if raw else None
                except (
                    RepositoryError, json.JSONDecodeError, UnicodeDecodeError, TypeError,
                ):
                    expected = observed_doc = None
                if not isinstance(observed_doc, dict):
                    target = None
                elif not isinstance(expected, dict) or any(
                    key in observed_doc and observed_doc[key] != value
                    for key, value in expected.items()
                ):
                    target = "sha256:" + "0" * 64
                elif all(key in observed_doc for key in expected):
                    target = digest
                else:
                    target = None
            else:
                target = "sha256:" + "0" * 64
            if target is None:
                action = ACTION_INSERT
            elif target == digest:
                action = ACTION_NOOP
            else:
                conflict = MergeConflict(
                    unit, kind, natural_key, CONFLICT_USER_STATE,
                    target, digest,
                    "target user configuration differs; import refuses to overwrite",
                )
        elif kind in ("failure_event", "legacy_archive", "job_relation"):
            # 这些在活动库没有落表面(P3a 起即 no-op 物化),merge 下同样无需比对。
            action = ACTION_NOOP

        if conflict is not None:
            action = ACTION_CONFLICT
            result.conflicts.append(conflict)
            result.conflicted_units.add(unit)
        result.actions[digest] = action
        result.counts[action] = result.counts.get(action, 0) + 1

    # 冲突单元内的其余 record 一律降为 skip:整单元零修改。
    if result.conflicted_units:
        for kind, digest, body in records:
            if _unit_of(kind, body) in result.conflicted_units \
                    and result.actions.get(digest) == ACTION_INSERT:
                result.actions[digest] = ACTION_SKIP
                result.counts[ACTION_INSERT] -= 1
                result.counts[ACTION_SKIP] += 1
    return result


def _resolve_records_for_classification(
    repository: ContentRepository, snapshot_digest: str,
) -> tuple[dict, list[tuple[str, str, dict]]]:
    """分类阶段用的快照展开;与 build_plan 走同一条校验路径。"""
    body = repository.get_snapshot(snapshot_digest)
    return body, _iter_snapshot_records(repository, body)


def _materialized_job_ids(
    materializer: "_Materializer", selected: Sequence[tuple[str, str, dict]],
) -> set[str]:
    """本次 merge 真正动过的 Job 集合;重投影只覆盖它们。

    以实际选入物化的 record 为准,而不是快照里的全部 Job:冲突单元与快照外的
    本地 Job 都不在其中,因此它们的运行态不会被这次 merge 碰到。
    """
    job_ids: set[str] = set()
    handled = set().union(*materializer.handled.values()) if materializer.handled else set()
    for kind, digest, body in selected:
        if digest not in handled:
            continue
        job_id = body.get("job_id") or (body.get("id") if kind == "job_core" else None)
        if job_id:
            job_ids.add(job_id)
    return job_ids


async def _classify_for_merge(
    *,
    repository: ContentRepository,
    snapshot: str,
    target_db_path: Path,
    storage,
    apply_user_state: bool,
    config_root: Path,
    source_roots: Mapping[str, Path],
) -> MergeClassification:
    """merge 分类的唯一入口;CLI 的 --plan 与真正导入共用它,避免两条路分叉。

    storage 必须指向目标库自己的产物根:规则 4(同 step 不同 manifest)靠读
    目标侧 manifest 判定,若把刚建的空 staging 当目标根,冲突永远判不出来,
    merge 会把本地已有的不同结果直接盖掉。根不可信时 fail-closed。
    """
    target_db_path = Path(target_db_path)
    if not target_db_path.exists():
        raise ImportError_(
            f"merge target {target_db_path} does not exist; "
            "use the empty mode to build a new database"
        )
    _assert_merge_storage_root(target_db_path, storage)
    resolved = _resolve_snapshot(repository, snapshot)
    _body, records = _resolve_records_for_classification(repository, resolved)
    probe = sqlite3.connect(target_db_path)
    probe.row_factory = sqlite3.Row
    try:
        return await classify_merge(
            connection=probe, storage=storage, repository=repository,
            config_root=config_root, source_roots=source_roots, records=records,
            apply_user_state=apply_user_state,
        )
    finally:
        probe.close()


def _assert_merge_storage_root(target_db_path: Path, storage) -> None:
    """merge 的分类器与写入面必须对着同一个产物根。

    本地盘可直接比对路径(约定 <data>/db/analyzer.db 与 <data>/jobs 同根)。
    对象存储没有本地路径,但"同一个根"这条不变量照样要成立:库的线上性与桶的
    生产性必须一致。旧实现在这里直接 return,等于对生产后端完全不设防——拿线上
    库配隔离桶做分类,规则 4 的冲突永远判不出来。
    """
    jobs_dir = getattr(storage, "jobs_dir", None)
    if jobs_dir is None:
        bucket = getattr(storage, "bucket", None)
        if bucket is None:
            raise ImportError_(
                "merge needs a storage backend that exposes its artifact root "
                "(local jobs_dir or object bucket); refusing to classify blind"
            )
        db_live = is_live_db(target_db_path)
        bucket_live = is_live_object_bucket(bucket)
        if db_live != bucket_live:
            raise ImportError_(
                f"merge needs the target's own artifact root: database "
                f"{target_db_path} is {'live' if db_live else 'isolated'} but bucket "
                f"{bucket!r} is {'the production bucket' if bucket_live else 'isolated'} "
                f"(production bucket is {production_bucket()!r}). Classifying against a "
                "different root would miss step-manifest conflicts and silently "
                "overwrite local results"
            )
        return
    expected = Path(target_db_path).resolve().parent.parent / "jobs"
    actual = Path(jobs_dir).resolve()
    if actual != expected.resolve():
        raise ImportError_(
            f"merge needs the target's own artifact root: database "
            f"{target_db_path} implies {expected}, but storage points at {actual}. "
            "Classifying against a different root would miss step-manifest conflicts "
            "and silently overwrite local results; pass the matching --jobs-dir "
            "(or --into-live) instead"
        )


def _record_skipped(
    journal: ContentImportJournal,
    import_id: str,
    classification: MergeClassification,
    records: Sequence[tuple[str, str, dict]],
) -> None:
    """把 no-op/冲突/跳过的判定也写进 journal,导入账本要能解释"为什么没动"。"""
    for kind, digest, body in records:
        action = classification.actions.get(digest, ACTION_SKIP)
        if action == ACTION_INSERT:
            continue  # 已由 materializer 登记
        journal.record_processed(
            import_id, record_digest=digest, kind=kind,
            natural_key=_natural_key_of(kind, body), action=action,
        )


def _user_state_revision(collection_id: object, job_id: str) -> str:
    """按备份侧同一算式算目标现值的 revision,用于前置比对。"""
    return canonical_digest({"job_id": job_id, "collection_id": collection_id})


def _user_state_precondition_ok(body: Mapping, target_collection_id: object) -> bool:
    """§2.9-6 的前置门:快照记录的 revision 必须等于目标现值的 revision。

    快照没带 revision(旧快照)时保守拒绝:宁可让人显式处理,也不拿"没凭据"
    当作"可以覆盖"。
    """
    revision = body.get("revision")
    if not revision:
        return False
    return revision == _user_state_revision(target_collection_id, body["job_id"])


def _ledger_verdict(
    unit: str, kind: str, natural_key: str, target: str, digest: str,
) -> tuple[str, MergeConflict | None]:
    """规则 2/7:同键同内容 no-op;同键不同内容即不可变账本冲突。"""
    if target == digest:
        return ACTION_NOOP, None
    return ACTION_CONFLICT, MergeConflict(
        unit, kind, natural_key, CONFLICT_LEDGER, target, digest,
        "immutable ledger row exists with different content",
    )


# 对外用简名,避免与内建 ImportError 混淆导入方。
ContentImportError = ImportError_


# 每类 record 抽样核对目标库的自然键;resume 前用它证明 journal 进度确实对应本库。
_SAMPLE_PROBES: Mapping[str, tuple[str, str]] = {
    "job_core": ("jobs", "id"),
    "part_core": ("job_parts", "id"),
    "collection": ("collections", "id"),
    "glossary": ("glossary", "term"),
    "ai_usage": ("ai_usage", "exec_id"),
}


def _sample_missing_records(
    connection: sqlite3.Connection,
    records: Sequence[tuple[str, str, dict]],
    processed: set[str],
    *, per_kind: int = 3,
) -> list[str]:
    """在已登记 record 里按 kind 抽样,回查目标库是否真有对应行。"""
    seen: dict[str, int] = {}
    missing: list[str] = []
    for kind, digest, body in records:
        if digest not in processed or kind not in _SAMPLE_PROBES:
            continue
        if seen.get(kind, 0) >= per_kind:
            continue
        seen[kind] = seen.get(kind, 0) + 1
        table, column = _SAMPLE_PROBES[kind]
        key = body.get("id") or body.get("term") or body.get("exec_id")
        if key is None:
            continue
        row = connection.execute(
            f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1', (key,),
        ).fetchone()
        if row is None:
            missing.append(f"{kind}:{key}")
    return missing


def _assert_job_relations(
    records: Sequence[tuple[str, str, dict]], handled: Mapping[str, set[str]],
    *, extra_known: set[str] | None = None,
) -> None:
    """用 P2a 写的 job_relation 交叉核对本次处理过的 digest 集合。

    这正是 job_relation 存在的意义:光把它读进来再丢掉,等于没做完整性闭环。
    empty 模式要求边集 ⊆ 实际物化集;merge 传 extra_known 把 no-op/跳过/冲突
    的 digest 也算作"已知",于是允许边集不完整,但悬空引用仍然报错。
    """
    processed_all = set().union(*handled.values()) if handled else set()
    if extra_known:
        processed_all = processed_all | extra_known
    for kind, digest, body in records:
        if kind != "job_relation":
            continue
        expected = {body["core"], *body["parts"], *body["step_results"].values(),
                    *body["failures"]}
        if "user_state" in body:
            expected.add(body["user_state"])
        missing = sorted(expected - processed_all)
        if missing:
            raise ImportError_(
                f"job_relation {body['job_id']} references records that were not "
                f"materialized: {missing[:5]}"
            )


@dataclass
class ImportPlan:
    snapshot_digest: str
    plan_digest: str
    counts: dict
    bytes_to_write: int
    disk_free_bytes: int
    conflicts: list[str]
    partial: bool
    job_ids: list[str]
    mode: str = MODE_EMPTY
    merge_conflicts: list[dict] = field(default_factory=list)
    user_state_kept: list[dict] = field(default_factory=list)
    request_digest: str = ""
    snapshot_format: str = ""
    portable_ready: bool = False
    readiness_reason: str | None = None
    readiness_reasons: list[str] = field(default_factory=list)
    completeness: dict = field(default_factory=dict)
    config_root: str = ""
    required_source_roots: list[str] = field(default_factory=list)
    missing_source_root_mappings: list[str] = field(default_factory=list)
    global_config_files: int = 0
    global_config_bytes: int = 0
    vendored_source_parts: int = 0
    vendored_source_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.conflicts


@dataclass
class ImportResult:
    import_id: str
    snapshot_digest: str
    plan: ImportPlan
    materialized: dict
    projection: dict
    verification: dict
    resumed: bool
    merge_report: dict = field(default_factory=dict)
    # 保活 ref 是否真的挂上;只读仓库下挂不上属可接受降级,但必须让操作者看见。
    snapshot_guard: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _storage_identity(storage) -> str:
    """返回不含凭据的规范存储身份;导入幂等不能跨根或跨桶复用。"""
    jobs_dir = getattr(storage, "jobs_dir", None)
    if jobs_dir is not None:
        return f"local:{Path(jobs_dir).resolve()}"
    bucket = getattr(storage, "bucket", None)
    endpoint = getattr(storage, "_endpoint", None)
    secure = bool(getattr(storage, "_secure", False))
    if bucket is None or endpoint is None:
        raise ImportError_(
            "import storage does not expose a stable local root or object namespace"
        )
    return f"object:{'https' if secure else 'http'}://{endpoint}/{bucket}"


def _relative_parts(rel: str, *, label: str) -> tuple[str, ...]:
    if not isinstance(rel, str) or "\\" in rel or "\x00" in rel:
        raise ImportError_(f"invalid {label} relative path {rel!r}")
    path = PurePosixPath(rel)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != rel
    ):
        raise ImportError_(f"invalid {label} relative path {rel!r}")
    return path.parts


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_controlled_directory(
    path: Path, *, label: str, create: bool,
) -> int:
    """从文件系统根逐层固定目录实体,可选创建缺失分量。"""
    if not path.is_absolute():
        raise ImportError_(f"{label} must be an absolute path")
    flags = _directory_open_flags()
    current = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o750, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    # 并发创建仍必须经过下面的 O_NOFOLLOW openat 复验。
                    pass
                child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_relative_parent(
    root_fd: int, rel: str, *, label: str, create: bool,
) -> tuple[int, str]:
    """从已固定的目标根打开相对父目录,不重新解析可变根路径。"""
    relative = _relative_parts(rel, label=label)
    flags = _directory_open_flags()
    current = os.dup(root_fd)
    try:
        for part in relative[:-1]:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o750, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current, relative[-1]
    except BaseException:
        os.close(current)
        raise


def _open_controlled_parent(
    root: Path, rel: str, *, label: str, create: bool,
) -> tuple[int, str]:
    """逐层用 dirfd + O_NOFOLLOW 固定目录,消除 symlink swap 窗口。"""
    root_fd = _open_controlled_directory(root, label=label, create=create)
    try:
        return _open_relative_parent(
            root_fd, rel, label=label, create=create,
        )
    finally:
        os.close(root_fd)


def _opened_directory_descends_from(
    directory_fd: int, ancestor: os.stat_result,
) -> bool:
    """沿真实 .. 关系检查目录仍在已固定目标根内。"""
    current_fd = os.dup(directory_fd)
    flags = _directory_open_flags()
    try:
        while True:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) == (ancestor.st_dev, ancestor.st_ino):
                return True
            parent_fd = os.open("..", flags, dir_fd=current_fd)
            parent = os.fstat(parent_fd)
            if (parent.st_dev, parent.st_ino) == (current.st_dev, current.st_ino):
                os.close(parent_fd)
                return False
            os.close(current_fd)
            current_fd = parent_fd
    finally:
        os.close(current_fd)


def _opened_directory_aliases_tree(directory_fd: int, tree_root: Path) -> bool:
    """识别目录位于仓库中或通过bind mount复用仓库任一目录实体。"""
    try:
        tree_identity = tree_root.stat(follow_symlinks=False)
    except OSError as exc:
        raise ImportError_(f"cannot inspect content repository root: {exc}") from exc
    if _opened_directory_descends_from(directory_fd, tree_identity):
        return True
    opened = os.fstat(directory_fd)
    try:
        for _path, directory_names, _files, tree_fd in os.fwalk(
            tree_root, topdown=True, follow_symlinks=False,
        ):
            directory_names[:] = [
                name for name in directory_names
                if not stat.S_ISLNK(os.stat(
                    name, dir_fd=tree_fd, follow_symlinks=False,
                ).st_mode)
            ]
            current = os.fstat(tree_fd)
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                return True
    except OSError as exc:
        raise ImportError_(
            f"cannot inspect content repository directory identities: {exc}"
        ) from exc
    return False


def _hash_open_file(fd: int) -> str:
    hasher = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, _CHUNK_SIZE):
        hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _hash_controlled_local(root: Path, rel: str, *, label: str) -> str | None:
    try:
        parent_fd, name = _open_controlled_parent(
            root, rel, label=label, create=False,
        )
    except (FileNotFoundError, NotADirectoryError, OSError, ImportError_):
        return None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except (FileNotFoundError, OSError):
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            return _hash_open_file(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


@dataclass(frozen=True)
class ImportRequest:
    """决定导入写入结果的规范请求;唯一摘要同时供 plan 与 journal 使用。"""

    snapshot_digest: str
    target_db_path: str
    target_storage: str
    mode: str
    apply_user_state: bool
    allow_partial: bool
    schema_version: int
    config_digest: str
    target_config_root: str = ""
    target_source_roots: tuple[tuple[str, str, str], ...] = ()
    allow_incomplete_portable_snapshot: bool = False

    @property
    def digest(self) -> str:
        return canonical_digest({
            "snapshot_digest": self.snapshot_digest,
            "target_db_path": self.target_db_path,
            "target_storage": self.target_storage,
            "mode": self.mode,
            "apply_user_state": self.apply_user_state,
            "allow_partial": self.allow_partial,
            "schema_version": self.schema_version,
            "config_digest": self.config_digest,
            "target_config_root": self.target_config_root,
            "target_source_roots": [list(item) for item in self.target_source_roots],
            "allow_incomplete_portable_snapshot": (
                self.allow_incomplete_portable_snapshot
            ),
        })


def _config_digest(config: AppConfig) -> str:
    """pipeline 语义变更会改变投影结果,必须进请求身份。"""
    return canonical_digest({"pipelines": config.pipelines})


def _normalized_source_roots(
    roots: Mapping[str, Path] | None,
) -> dict[str, Path]:
    from .source_library import SourceReferenceError, build_source_ref

    result: dict[str, Path] = {}
    for root_id, raw_path in sorted((roots or {}).items()):
        try:
            build_source_ref(root_id, "identity-probe.mp4")
        except SourceReferenceError as exc:
            raise ImportError_(f"invalid source root id {root_id!r}") from exc
        path = Path(raw_path)
        if not path.is_absolute():
            raise ImportError_(f"source root {root_id!r} must be an absolute path")
        result[root_id] = Path(os.path.abspath(path))
    return result


def _source_root_identities(roots: Mapping[str, Path]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for root_id, path in sorted(roots.items()):
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ImportError_(f"source root {root_id!r} is not readable: {path}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ImportError_(f"source root {root_id!r} is not a directory: {path}")
        identities[root_id] = f"{info.st_dev}:{info.st_ino}"
    return identities


def _verify_source_root_identities(
    roots: Mapping[str, Path], expected: Mapping[str, str] | None,
) -> dict[str, str]:
    actual = _source_root_identities(roots)
    if expected is not None:
        normalized_expected = dict(sorted(expected.items()))
        if set(normalized_expected) != set(actual):
            raise ImportError_("source root identity keys do not match --source-root mappings")
        for root_id, observed in actual.items():
            if normalized_expected[root_id] != observed:
                raise ImportError_(
                    f"source root {root_id!r} changed before container validation: "
                    f"{normalized_expected[root_id]} != {observed}"
                )
    return actual


def _resolve_snapshot(repository: ContentRepository, ref_or_digest: str) -> str:
    """ref 名或 digest 都接受;digest 形态优先按 digest 解析。"""
    if ref_or_digest.startswith("sha256:"):
        return ref_or_digest
    try:
        return repository.get_ref(ref_or_digest)
    except RepositoryError as exc:
        raise ImportError_(f"cannot resolve snapshot {ref_or_digest!r}: {exc}") from exc


def _snapshot_readiness(
    body: Mapping,
) -> tuple[bool, str | None, list[str], dict]:
    """把 snapshot 兼容性与 portable 完整性分开;旧 v1 可读但不冒充可恢复。"""
    snapshot_format = body.get("format")
    if snapshot_format == PORTABLE_SNAPSHOT_V1:
        reason = "legacy_snapshot_without_completeness"
        return False, reason, [reason], {}
    if snapshot_format != PORTABLE_SNAPSHOT_V2:
        reason = "unsupported_snapshot_format"
        return False, reason, [reason], {}
    completeness = body.get("completeness")
    if not isinstance(completeness, dict):
        reason = "snapshot_completeness_missing"
        return False, reason, [reason], {}
    ready = completeness.get("portable_ready") is True
    raw_reasons = completeness.get("readiness_reasons")
    reasons = (
        list(raw_reasons)
        if isinstance(raw_reasons, list)
        and all(isinstance(item, str) and item for item in raw_reasons)
        else []
    )
    if ready:
        reasons = []
    elif not reasons:
        reasons = ["snapshot_completeness_not_ready"]
    return ready, reasons[0] if reasons else None, reasons, dict(completeness)


def _iter_snapshot_records(
    repository: ContentRepository, body: Mapping,
) -> list[tuple[str, str, dict]]:
    """展开快照全部 record 为 (kind, digest, body);顺序稳定。

    get_record 会重新校验 canonical 字节、digest 与 allowlist 策略,因此这次遍历
    本身就是 §2.10 阶段0 要求的 record 全链校验。
    """
    result: list[tuple[str, str, dict]] = []
    for group, digests in sorted(body["records"].items()):
        kinds = SNAPSHOT_RECORD_GROUPS[group]
        for digest in digests:
            for kind in sorted(kinds):
                if repository.has_record(kind, digest):
                    result.append((kind, digest, repository.get_record(kind, digest)))
                    break
            else:
                raise ImportError_(f"record {digest} missing from repository")
    return result


def build_plan(
    *,
    repository: ContentRepository,
    snapshot: str,
    target_db_path: Path,
    allow_partial: bool = False,
    verify_blobs: bool = True,
    resuming: bool = False,
    config: AppConfig | None = None,
    mode: str = MODE_EMPTY,
    classification: "MergeClassification | None" = None,
    request: "ImportRequest | None" = None,
) -> tuple[ImportPlan, dict, list[tuple[str, str, dict]]]:
    """阶段0 安全门:全链校验 + 计划产出;返回 (plan, snapshot body, records)。

    verify_blobs 打开时逐个重算 blob SHA(首次导入应开);关闭仅用于已 scrub 过的
    仓库上做快速复算,不改变正确性判定的其他部分。
    resuming=True 时跳过空库门:目标库里的行正是本次导入上一轮写的,由 journal
    背书;此时再拿"非空"拦自己就永远无法 resume。
    """
    digest = _resolve_snapshot(repository, snapshot)
    try:
        # get_snapshot 内含 canonical/digest/闭包全验证(P1 契约)。
        body = repository.get_snapshot(digest)
    except RepositoryError as exc:
        raise ImportError_(f"snapshot {digest} rejected: {exc}") from exc

    (
        portable_ready,
        readiness_reason,
        readiness_reasons,
        completeness,
    ) = _snapshot_readiness(body)

    conflicts: list[str] = []
    selector = body["selector"]
    if selector["partial"] and not allow_partial and mode == MODE_EMPTY:
        # merge 下导入局部快照是正常用途(往开发库补 Job),只有 empty 会把
        # 局部快照误当成"系统全貌"重建,门只对 empty 生效。
        conflicts.append(
            f"snapshot is partial (job_ids={selector['job_ids']}); empty import would "
            "rebuild a partial system as if it were complete - pass allow_partial to override"
        )

    try:
        records = _iter_snapshot_records(repository, body)
    except (RepositoryError, PolicyError) as exc:
        raise ImportError_(f"record chain verification failed: {exc}") from exc

    required_source_roots: set[str] = set()
    vendored_source_parts = 0
    vendored_source_bytes = 0
    global_config_files = 0
    global_config_bytes = 0
    from .source_library import SourceReferenceError, parse_source_ref

    for kind, _record_digest, record in records:
        if kind == "part_core" and "source_blob" in record:
            try:
                source_ref = parse_source_ref(record.get("source_ref"))
            except SourceReferenceError as exc:
                raise ImportError_(
                    f"vendored part {record.get('id')} has invalid source_ref: {exc}"
                ) from exc
            required_source_roots.add(source_ref.root_id)
            vendored_source_parts += 1
            vendored_source_bytes += int(record.get("size_bytes") or 0)
        elif kind == "user_config" and record.get("kind") in _GLOBAL_CONFIG_KINDS:
            global_config_files += 1
            global_config_bytes += int(record.get("size_bytes") or 0)
    provided_source_roots = (
        {key for key, _path, _identity in request.target_source_roots}
        if request is not None else set()
    )
    missing_source_roots = sorted(required_source_roots - provided_source_roots)
    if request is not None and missing_source_roots:
        conflicts.append(
            "vendored source media requires missing --source-root mappings: "
            + ", ".join(missing_source_roots)
        )

    bytes_to_write = 0
    for blob in body["blob_refs"]:
        try:
            if verify_blobs:
                size = repository.verify_blob(blob)
            else:
                # 不重算 SHA 也要给出真实字节数:--plan 的整个用途就是"看清要写多少",
                # 而紧接着的磁盘余量门拿 bytes_to_write 作判据。这里恒 0 会让预演既
                # 报不出容量又永远不触发余量门(P4 演练实测)。stat 便宜,重算才贵。
                path = repository.blob_path(blob)
                if not path.is_file():
                    raise RepositoryError(f"blob {blob} not found")
                size = path.stat().st_size
        except (RepositoryError, OSError) as exc:
            raise ImportError_(f"blob chain verification failed: {exc}") from exc
        bytes_to_write += size

    # noop/conflict/pending 属 merge 模式的分类结果;empty 模式下没有可比对的
    # 既有状态,用 None 明说"不适用",不拿 0 冒充"已分类且为零"。
    counts: dict = {
        "insert": len(records),
        "rebuild": sum(1 for kind, _d, _b in records if kind == "step_result"),
        "blobs": len(body["blob_refs"]),
        "noop": None, "conflict": None, "pending": None,
    }
    merge_conflicts: list[dict] = []
    user_state_kept: list[dict] = []
    if classification is not None:
        # merge 模式下三个计数不再是"不适用",而是真实分类结果。
        counts["insert"] = classification.counts[ACTION_INSERT]
        counts["noop"] = classification.counts[ACTION_NOOP]
        counts["conflict"] = classification.counts[ACTION_CONFLICT]
        counts["pending"] = classification.counts[ACTION_SKIP]
        merge_conflicts = [item.as_dict() for item in classification.conflicts]
        user_state_kept = list(classification.user_state_kept)
        if merge_conflicts:
            units = sorted({item["unit"] for item in merge_conflicts})
            conflicts.append(
                f"{len(merge_conflicts)} merge conflicts across {len(units)} units "
                f"(e.g. {units[:3]}); those units will not be modified"
            )
        ledger_hits = [
            item for item in merge_conflicts
            if item["conflict"] == CONFLICT_LEDGER
        ]
        if ledger_hits:
            # 不可变账本同键不同内容 = 仓库损坏或身份撞车,不能只记在报告里
            # 让操作者拿着 exit 0 走人。
            conflicts.append(
                f"{len(ledger_hits)} immutable ledger conflicts "
                f"(e.g. {[item['natural_key'] for item in ledger_hits][:3]}); "
                "the snapshot and the target disagree on rows that must never differ"
            )

    if config is not None:
        unknown_pipelines = sorted({
            item["pipeline"] for kind, _d, item in records
            if kind == "job_core" and item["pipeline"] not in config.pipelines
        })
        if unknown_pipelines:
            conflicts.append(
                f"snapshot references pipelines missing from current config: "
                f"{unknown_pipelines}; those jobs cannot be projected"
            )

    target_db_path = Path(target_db_path)
    # merge 的前提就是目标库有数据,空库门只属于 empty 模式。
    if mode == MODE_EMPTY and not resuming:
        conflicts.extend(_empty_target_conflicts(target_db_path))
    probe_dir = target_db_path.parent if target_db_path.parent.exists() else Path.cwd()
    disk_free = shutil.disk_usage(probe_dir).free
    if disk_free < bytes_to_write:
        conflicts.append(
            f"insufficient disk space: need {bytes_to_write} bytes, free {disk_free}"
        )

    # plan_digest 只由"这个快照要导入什么"决定。容量事实(bytes_to_write)和随目标库
    # 状态漂移的量(insert/noop/conflict 计数)都不进身份:前者会让 --plan 与真正导入
    # 算出两个 digest,后者会让同一 snapshot 第二次 merge 变身份,两种都让
    # journal.begin 判"计划变了"而拒绝续跑。
    plan_core = {
        "snapshot_digest": digest,
        "records_total": len(records),
        "blobs_total": len(body["blob_refs"]),
        "selector": selector,
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "request_digest": request.digest if request is not None else None,
    }
    plan = ImportPlan(
        snapshot_digest=digest,
        plan_digest=canonical_digest(plan_core),
        counts=counts,
        bytes_to_write=bytes_to_write,
        disk_free_bytes=disk_free,
        conflicts=conflicts,
        partial=selector["partial"],
        job_ids=list(selector["job_ids"]),
        mode=mode,
        merge_conflicts=merge_conflicts,
        user_state_kept=user_state_kept,
        request_digest=request.digest if request is not None else "",
        snapshot_format=str(body.get("format") or ""),
        portable_ready=portable_ready,
        readiness_reason=readiness_reason,
        readiness_reasons=readiness_reasons,
        completeness=completeness,
        config_root=request.target_config_root if request is not None else "",
        required_source_roots=sorted(required_source_roots),
        missing_source_root_mappings=missing_source_roots,
        global_config_files=global_config_files,
        global_config_bytes=global_config_bytes,
        vendored_source_parts=vendored_source_parts,
        vendored_source_bytes=vendored_source_bytes,
    )
    return plan, body, records


def _empty_target_conflicts(target_db_path: Path) -> list[str]:
    """empty 模式的目标库门:不存在,或只含当前 migration 建的空 schema。

    逐表扫全库而不是抽查几张:物化写十几张表,只查 jobs/collections 之类会让
    其余表非空的库通过,然后在中途撞 UNIQUE,留下半写的库。
    """
    if not target_db_path.exists():
        return []
    connection = sqlite3.connect(f"file:{target_db_path}?mode=ro", uri=True)
    try:
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        if not tables:
            return []
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            return [
                f"target database schema v{version} != current v{SCHEMA_VERSION}; "
                "empty import needs an absent or freshly migrated database"
            ]
        # FTS5 虚表及其 shadow 表建库即自带行(config/data),它们是可重建投影,
        # 不构成"这个库里已经有业务数据"。
        from .content_policy import CATEGORY_REBUILDABLE, classify_table

        populated: list[str] = []
        for table in sorted(tables):
            if table == "schema_migrations":
                continue
            try:
                if classify_table(table)[0] == CATEGORY_REBUILDABLE:
                    continue
            except PolicyError:
                pass
            try:
                count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.DatabaseError:
                continue  # FTS5 shadow 等不可直接计数的对象跳过
            if count:
                populated.append(f"{table}={count}")
        if populated:
            return [
                "target database is not empty: " + ", ".join(populated[:8])
                + "; empty import refuses to write into a populated database"
            ]
        return []
    finally:
        connection.close()


def _target_binding_token(target_db_path: Path) -> str | None:
    """从目标库自身派生绑定指纹;不往业务库加任何 schema 对象。

    取 schema_migrations 账本的 (version, applied_at) 加上文件 inode/device:
    建库时 applied_at 现写、inode 现分配,所以库被丢弃重建后指纹必然不同。
    journal 记下它,resume 时比对——这正是"丢弃新库后同 generation 重跑
    却因 digest 已登记而跳过物化,最终空库报成功"的拦截点(§2.10 阶段5)。
    """
    path = Path(target_db_path)
    if not path.exists():
        return None
    try:
        stat = path.stat()
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ledger = connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        return None
    payload = canonical_json_bytes({
        "ledger": [[int(row[0]), str(row[1])] for row in ledger],
        "ino": stat.st_ino,
        "dev": stat.st_dev,
    })
    return "bind:" + hashlib.sha256(payload).hexdigest()[:32]


class _Materializer:
    """把已验证 record 与 blob 写进全新目标环境;顺序与 read-back 由本类保证。"""

    def __init__(
        self,
        *,
        repository: ContentRepository,
        connection: sqlite3.Connection,
        storage,
        journal: ContentImportJournal,
        import_id: str,
        processed: set[str],
        config_root: Path,
        source_roots: Mapping[str, Path],
        commit_each: bool = True,
    ) -> None:
        self.repository = repository
        self.connection = connection
        self.storage = storage
        self.journal = journal
        self.import_id = import_id
        self.processed = processed
        self.config_root = Path(config_root)
        self.source_roots = {key: Path(value) for key, value in source_roots.items()}
        # merge 直写活动库时整批裹在一个显式事务里,逐条 commit 会当场把它提交掉,
        # 让"中途失败不留半写状态"这条不变量失效。
        self.commit_each = commit_each
        self.stats: dict[str, int] = {
            "records_inserted": 0, "records_resumed": 0,
            "objects_written": 0, "objects_reused": 0, "bytes_written": 0,
        }
        # kind -> 本次已落实的 record digest;job_relation 闭环核对用。
        self.handled: dict[str, set[str]] = {}

    def _mark(self, kind: str, digest: str, natural_key: str, action: str) -> None:
        self.handled.setdefault(kind, set()).add(digest)
        self.journal.record_processed(
            self.import_id, record_digest=digest, kind=kind,
            natural_key=natural_key, action=action,
        )
        self.processed.add(digest)

    async def materialize(self, records: Sequence[tuple[str, str, dict]]) -> None:
        by_kind: dict[str, list[tuple[str, dict]]] = {}
        for kind, digest, body in records:
            by_kind.setdefault(kind, []).append((digest, body))
        # 未知 kind 预检必须在写任何东西之前:早期版本放在循环末尾,
        # 等于写了一半才发现不认识某个 kind,留下半物化的库。
        unknown = sorted(set(by_kind) - set(MATERIALIZE_ORDER))
        if unknown:
            raise ImportError_(f"no materializer for record kinds {unknown}")
        for kind in MATERIALIZE_ORDER:
            for digest, body in by_kind.get(kind, ()):
                if digest in self.processed:
                    # resume:已验证过的内容不重复复制(含大视频)。
                    self.stats["records_resumed"] += 1
                    self.handled.setdefault(kind, set()).add(digest)
                    continue
                await self._materialize_one(kind, digest, body)

    async def _materialize_one(self, kind: str, digest: str, body: dict) -> None:
        handler = getattr(self, f"_put_{kind}", None)
        if handler is None:
            raise ImportError_(f"no materializer for record kind {kind!r}")
        result = handler(body)
        natural_key = await result if asyncio.iscoroutine(result) else result
        if self.commit_each:
            self.connection.commit()
        self.stats["records_inserted"] += 1
        self._mark(kind, digest, str(natural_key), "insert")


    @staticmethod
    def _json_text(value: object, default: str = "{}") -> str:
        if value is None:
            return default
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _put_collection(self, body: dict) -> str:
        self.connection.execute(
            """INSERT OR IGNORE INTO collections
               (id, name, domain, description, tags, source_type, source_id,
                sync_enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                body["id"], body["name"], body.get("domain", "general"),
                body.get("description", ""), self._json_text(body.get("tags"), "[]"),
                body.get("source_type"), body.get("source_id"),
                int(body.get("sync_enabled", 1)),
                body["created_at"], body.get("updated_at", body["created_at"]),
            ),
        )
        return body["id"]

    async def _put_job_core(self, body: dict) -> str:
        # status/progress/error 刻意写初始值:真值由阶段3 投影产生(§2.9)。
        self.connection.execute(
            """INSERT OR IGNORE INTO jobs
               (id, content_type, document_kind, pipeline, url, title, domain,
                source, style_tags, status, progress_pct, meta, published_at,
                created_at, updated_at, lineage_key, is_current, source_digest,
                pipeline_digest, parent_job_id)
               VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?,?,?,?,?,?,?,?)""",
            (
                body["id"], body["content_type"], body.get("document_kind", ""),
                body["pipeline"], body.get("url"), body.get("title"),
                body.get("domain", "general"), body.get("source"),
                self._json_text(body.get("style_tags"), "[]"),
                self._json_text(body.get("meta")), body.get("published_at"),
                body["created_at"], body["created_at"],
                body.get("lineage_key"), int(body.get("is_current", 1)),
                body.get("source_digest"), body.get("pipeline_digest"),
                body.get("parent_job_id"),
            ),
        )
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        job_doc = {
            "id": body["id"],
            "url": body.get("url"),
            "source": body.get("source"),
            "content_type": body["content_type"],
            "title": body.get("title"),
            "document_kind": body.get("document_kind") or None,
            "domain": body.get("domain", "general"),
            "style_tags": body.get("style_tags") or [],
            "created_at": body["created_at"],
            "flags": meta.get("flags") if isinstance(meta.get("flags"), dict) else {},
        }
        if isinstance(meta.get("creation_fingerprint"), str):
            job_doc["creation_fingerprint"] = meta["creation_fingerprint"]
        await self._merge_job_document(body["id"], "job.json", job_doc)
        return body["id"]

    def _put_job_user_state(self, body: dict) -> str:
        self.connection.execute(
            "UPDATE jobs SET collection_id=? WHERE id=?",
            (body.get("collection_id"), body["job_id"]),
        )
        return body["job_id"]

    async def _put_part_core(self, body: dict) -> str:
        source_blob = body.get("source_blob")
        if source_blob is not None:
            if source_blob != body.get("source_digest"):
                raise ImportError_(
                    f"part {body['id']} source_blob must equal source_digest"
                )
            from .source_library import SourceReferenceError, parse_source_ref

            try:
                source_ref = parse_source_ref(body.get("source_ref"))
            except SourceReferenceError as exc:
                raise ImportError_(
                    f"part {body['id']} has invalid vendored source_ref: {exc}"
                ) from exc
            root = self.source_roots.get(source_ref.root_id)
            if root is None:
                raise ImportError_(
                    f"part {body['id']} requires --source-root "
                    f"{source_ref.root_id}=TARGET_DIR"
                )
            await self._publish_local_blob(
                root,
                source_ref.relative_path,
                source_blob,
                body["size_bytes"],
                label=f"source root {source_ref.root_id}",
            )
        self.connection.execute(
            """INSERT OR IGNORE INTO job_parts
               (id, job_id, part_index, title, source_url, source_ref,
                source_digest, size_bytes, duration_ms, meta, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                body["id"], body["job_id"], body["part_index"], body.get("title"),
                body.get("source_url"), body.get("source_ref"),
                body.get("source_digest"), body.get("size_bytes"),
                body.get("duration_ms"), self._json_text(body.get("meta")),
                body["created_at"], body.get("updated_at", body["created_at"]),
            ),
        )
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        part_doc = {
            "job_id": body["job_id"],
            "part_id": body["id"],
            "part_index": body["part_index"],
            "title": body.get("title"),
            "url": body.get("source_url"),
            "source": meta.get("source"),
            "content_type": "video",
        }
        if body.get("source_ref"):
            part_doc.update({
                "source_ref": body["source_ref"],
                "source_digest": body.get("source_digest"),
                "size_bytes": body.get("size_bytes"),
            })
        raw = await self.storage.read_file(body["job_id"], "job.json")
        if not raw:
            raise ImportError_(f"job.json missing before part {body['id']} restore")
        try:
            job_doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise ImportError_(f"job {body['job_id']} job.json is invalid") from exc
        if not isinstance(job_doc, dict):
            raise ImportError_(f"job {body['job_id']} job.json must be an object")
        parts = job_doc.get("parts")
        if parts is None:
            parts = []
            job_doc["parts"] = parts
        if not isinstance(parts, list) or any(not isinstance(item, dict) for item in parts):
            raise ImportError_(f"job {body['job_id']} job.json parts is invalid")
        existing = next(
            (item for item in parts if item.get("part_id") == body["id"]), None,
        )
        if existing is None:
            parts.append(part_doc)
            parts.sort(key=lambda item: int(item.get("part_index") or 0))
        else:
            for key, value in part_doc.items():
                if key in existing and existing[key] != value:
                    raise ImportError_(
                        f"job.json part {body['id']} field {key} conflicts with snapshot"
                    )
                existing[key] = value
        await self.storage.write_file(
            body["job_id"], "job.json",
            json.dumps(job_doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        await self._merge_job_document(
            body["job_id"], f"parts/{body['id']}/job.json", part_doc,
        )
        return f"{body['job_id']}/{body['id']}"

    def _put_prompt_override(self, body: dict) -> str:
        self.connection.execute(
            """INSERT OR IGNORE INTO prompt_overrides
               (scope, domain, pipeline, document_kind, step, content, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                body["scope"], body.get("domain", ""), body.get("pipeline", ""),
                body.get("document_kind", ""), body["step"], body["content"],
                body["version"], body.get("updated_at", _now()),
            ),
        )
        return f"{body['scope']}/{body['step']}"

    def _put_prompt_override_version(self, body: dict) -> str:
        self.connection.execute(
            """INSERT OR IGNORE INTO prompt_override_versions
               (scope, domain, pipeline, document_kind, step, version, content,
                note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                body["scope"], body.get("domain", ""), body.get("pipeline", ""),
                body.get("document_kind", ""), body["step"], body["version"],
                body["content"], body.get("note"), body["created_at"],
            ),
        )
        return f"{body['scope']}/{body['step']}/v{body['version']}"

    def _put_glossary(self, body: dict) -> str:
        self.connection.execute(
            """INSERT OR IGNORE INTO glossary
               (domain, term, definition, zh_name, aliases, occurrences, related,
                status, watched, is_topic, definition_locked, lock_revision,
                current_definition_version_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                body["domain"], body["term"], body.get("definition", ""),
                body.get("zh_name"), self._json_text(body.get("aliases"), "[]"),
                self._json_text(body.get("occurrences"), "[]"),
                self._json_text(body.get("related"), "[]"),
                body.get("status", "active"), int(body.get("watched", 0)),
                int(body.get("is_topic", 0)), int(body.get("definition_locked", 0)),
                int(body.get("lock_revision", 0)),
                body.get("current_definition_version_id"),
                body.get("created_at", _now()), body.get("updated_at", _now()),
            ),
        )
        return f"{body['domain']}/{body['term']}"

    def _put_definition_version(self, body: dict) -> str:
        self.connection.execute(
            """INSERT OR IGNORE INTO concept_definition_versions
               (definition_version_id, domain, term, version, definition,
                source_evidence_ids_json, source_set_fingerprint, strategy,
                provider, model, prompt_hash, input_hash, supersedes_version_id,
                actor, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                body["definition_version_id"], body["domain"], body["term"],
                body["version"], body.get("definition", ""),
                self._json_text(body.get("source_evidence_ids_json"), "[]"),
                body["source_set_fingerprint"], body["strategy"],
                body.get("provider"), body.get("model"), body.get("prompt_hash"),
                body.get("input_hash"), body.get("supersedes_version_id"),
                body["actor"], body["created_at"],
            ),
        )
        return body["definition_version_id"]

    def _put_ingested_item(self, body: dict) -> str:
        self.connection.execute(
            "INSERT OR IGNORE INTO ingested_items (collection_id, item_id, ingested_at) VALUES (?,?,?)",
            (body["collection_id"], body["item_id"], body["ingested_at"]),
        )
        return f"{body['collection_id']}/{body['item_id']}"

    def _put_study(self, body: dict) -> str:
        table, row = body["table"], body["row"]
        columns = sorted(row)
        placeholders = ",".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )
        return f"{table}/{row.get('card_id') or row.get('batch_id') or ''}"

    def _put_ai_usage(self, body: dict) -> str:
        columns = sorted(body)
        placeholders = ",".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT OR IGNORE INTO ai_usage ({','.join(columns)}) VALUES ({placeholders})",
            tuple(body[column] for column in columns),
        )
        return body["exec_id"]

    def _put_ai_task_log(self, body: dict) -> str:
        expected = _digest_of("ai_task_log", body)
        existing = _target_ledger_digest(self.connection, "ai_task_log", body)
        if existing is not None:
            if existing == expected:
                return f"{body['task_id']}/{body['created_at']}/{body['exec_id']}"
            raise ImportError_(
                "ai_task_log composite identity already exists with different or "
                "duplicated content"
            )
        columns = sorted(body)
        placeholders = ",".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO ai_task_logs ({','.join(columns)}) VALUES ({placeholders})",
            tuple(body[column] for column in columns),
        )
        return f"{body['task_id']}/{body['created_at']}/{body['exec_id']}"

    def _put_failure_event(self, body: dict) -> str:
        # 无 step_failure_events 表(P2b 才建):本单元只把失败审计留在仓库,
        # 不在活动库伪造失败状态(§2.4B "不恢复成活动失败状态")。
        return f"{body['job_id']}/{body['scope_key']}/{body['step']}"

    def _put_legacy_archive(self, body: dict) -> str:
        # 旧归档只供人工追溯,不在新库重建同名活动表(§2.4B-3)。
        return f"{body['table']}#{body.get('chunk_index', 0)}"

    def _put_job_relation(self, body: dict) -> str:
        # 关系索引是仓库侧事实,活动库由 FK 自身表达,无需落表。
        return body["job_id"]

    async def _put_user_config(self, body: dict) -> str:
        """用户配置按明确写面恢复;全局配置绝不落进 Job 对象根。

        走 _publish_object 的 read-back 校验:异 hash 拒绝覆盖,不猜哪份更新。
        """
        kind = body["kind"]
        if kind == "job_ai_config":
            await self._restore_job_ai_config(body)
            return body["path"]
        if kind in _GLOBAL_CONFIG_KINDS:
            path = self._validated_config_path(kind, body["path"])
            await self._publish_local_blob(
                self.config_root,
                path,
                body["blob"],
                body["size_bytes"],
                label="config root",
            )
            return body["path"]
        if kind != "domain_config":
            raise ImportError_(f"unsupported user_config kind {kind!r}")
        prefix, _, rel = body["path"].rpartition("/")
        await self._publish_object(prefix, rel, body["blob"], body["size_bytes"])
        return body["path"]

    async def _merge_job_document(
        self, job_id: str, rel: str, fields: Mapping,
    ) -> None:
        raw = await self.storage.read_file(job_id, rel)
        if raw:
            try:
                document = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
                raise ImportError_(f"{job_id}/{rel} is invalid JSON") from exc
            if not isinstance(document, dict):
                raise ImportError_(f"{job_id}/{rel} must be a JSON object")
        else:
            document = {}
        for key, value in fields.items():
            if key in document and document[key] != value:
                raise ImportError_(
                    f"{job_id}/{rel} field {key} conflicts with snapshot"
                )
            document[key] = value
        await self.storage.write_file(
            job_id,
            rel,
            json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    async def _read_small_blob(self, digest: str, size_bytes: int) -> bytes:
        if size_bytes > 4 * 1024 * 1024:
            raise ImportError_(f"configuration blob {digest} exceeds 4 MiB")
        chunks: list[bytes] = []
        total = 0
        with self.repository.open_blob_stream(digest) as source:
            while True:
                chunk = await asyncio.to_thread(source.read, _CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        if total != size_bytes:
            raise ImportError_(
                f"configuration blob {digest} size mismatch ({total} != {size_bytes})"
            )
        data = b"".join(chunks)
        observed = "sha256:" + hashlib.sha256(data).hexdigest()
        if observed != digest:
            raise ImportError_(f"configuration blob {digest} failed digest verification")
        return data

    async def _restore_job_ai_config(self, body: Mapping) -> None:
        path = PurePosixPath(body["path"])
        if (
            len(path.parts) != 3
            or path.parts[0] != "jobs"
            or path.parts[2] != "ai-config.json"
        ):
            raise ImportError_(f"invalid job_ai_config path {body['path']!r}")
        job_id = path.parts[1]
        data = await self._read_small_blob(body["blob"], body["size_bytes"])
        try:
            fields = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise ImportError_(f"job AI config for {job_id} is invalid JSON") from exc
        if not isinstance(fields, dict):
            raise ImportError_(f"job AI config for {job_id} must be an object")
        unknown = sorted(set(fields) - _JOB_AI_CONFIG_KEYS)
        if unknown:
            raise ImportError_(
                f"job AI config for {job_id} contains forbidden keys {unknown}"
            )
        ai_overrides = fields.get("ai_overrides")
        if ai_overrides is not None:
            if not isinstance(ai_overrides, dict):
                raise ImportError_(f"job AI config for {job_id} ai_overrides is invalid")
            from .ai_routing import parse_ai_override

            for step in ai_overrides:
                if not isinstance(step, str) or not step:
                    raise ImportError_(
                        f"job AI config for {job_id} has an invalid AI step"
                    )
                _provider, reason = parse_ai_override(
                    {"ai_overrides": ai_overrides}, step,
                )
                if reason is not None:
                    raise ImportError_(
                        f"job AI config for {job_id} ai_overrides.{step}: {reason}"
                    )
        prompt_overrides = fields.get("prompt_overrides")
        if prompt_overrides is not None:
            if not isinstance(prompt_overrides, dict):
                raise ImportError_(
                    f"job AI config for {job_id} prompt_overrides is invalid"
                )
            from .prompt_resolver import PromptResolutionError, parse_prompt_override

            for step in prompt_overrides:
                if not isinstance(step, str) or not step:
                    raise ImportError_(
                        f"job AI config for {job_id} has an invalid prompt step"
                    )
                try:
                    parse_prompt_override(prompt_overrides, step)
                except PromptResolutionError as exc:
                    raise ImportError_(
                        f"job AI config for {job_id} prompt_overrides.{step}: {exc}"
                    ) from exc
        if "ai_config" in fields and not isinstance(fields["ai_config"], dict):
            raise ImportError_(f"job AI config for {job_id} ai_config is invalid")
        await self._merge_job_document(job_id, "job.json", fields)

    @staticmethod
    def _validated_config_path(kind: str, raw: str) -> str:
        parts = _relative_parts(raw, label="global config")
        if parts[0] != "prompts":
            raise ImportError_(f"global config path {raw!r} is outside prompts/")
        if kind == "prompts":
            if len(parts) != 2 or not parts[1].endswith(".md"):
                raise ImportError_(
                    f"prompt config path {raw!r} must be prompts/<name>.md"
                )
        elif (
            kind not in {"profiles", "styles", "templates"}
            or len(parts) < 3
            or parts[1] != kind
        ):
            raise ImportError_(
                f"global config path {raw!r} does not match kind {kind!r}"
            )
        # --config-root 已表示目标 prompts 根,不能再多写一层 prompts/prompts。
        return PurePosixPath(*parts[1:]).as_posix()

    async def _publish_local_blob(
        self,
        root: Path,
        rel: str,
        digest: str,
        size_bytes: int,
        *,
        label: str,
    ) -> None:
        """create-if-absent 发布本地 blob;拒绝路径穿越、symlink 与异内容覆盖。"""
        verified_size = await asyncio.to_thread(self.repository.verify_blob, digest)
        if verified_size != size_bytes:
            raise ImportError_(
                f"{label} blob {digest} size mismatch ({verified_size} != {size_bytes})"
            )

        def publish() -> tuple[bool, int]:
            root_fd = _open_controlled_directory(root, label=label, create=True)
            root_identity = os.fstat(root_fd)

            def validate_root() -> None:
                try:
                    current = os.stat(root, follow_symlinks=False)
                except OSError as exc:
                    raise ImportError_(
                        f"{label} root changed during publication: {root}"
                    ) from exc
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino)
                    != (root_identity.st_dev, root_identity.st_ino)
                    or _opened_directory_aliases_tree(
                        root_fd, self.repository.root,
                    )
                ):
                    raise ImportError_(
                        f"{label} root changed or aliases the content repository: {root}"
                    )

            temporary = ""
            parent_fd: int | None = None
            try:
                validate_root()
                parent_fd, target_name = _open_relative_parent(
                    root_fd, rel, label=label, create=True,
                )
                temporary = f".{target_name}.import-{uuid.uuid4().hex}"

                def validate_parent() -> None:
                    validate_root()
                    try:
                        current_parent_fd, current_target_name = _open_relative_parent(
                            root_fd, rel, label=label, create=False,
                        )
                    except (OSError, ImportError_) as exc:
                        raise ImportError_(
                            f"{label} parent path changed during publication: {rel}"
                        ) from exc
                    try:
                        opened_parent = os.fstat(parent_fd)
                        current_parent = os.fstat(current_parent_fd)
                        path_changed = (
                            current_target_name != target_name
                            or (opened_parent.st_dev, opened_parent.st_ino)
                            != (current_parent.st_dev, current_parent.st_ino)
                        )
                    finally:
                        os.close(current_parent_fd)
                    if (
                        path_changed
                        or not _opened_directory_descends_from(parent_fd, root_identity)
                        or _opened_directory_aliases_tree(
                            parent_fd, self.repository.root,
                        )
                    ):
                        raise ImportError_(
                            f"{label} parent moved outside its root or into the "
                            f"content repository: {rel}"
                        )

                validate_parent()
                try:
                    existing_fd = os.open(
                        target_name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    existing_fd = None
                if existing_fd is not None:
                    try:
                        if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                            raise ImportError_(
                                f"{label} target is not a regular file: {rel}"
                            )
                        observed = _hash_open_file(existing_fd)
                    finally:
                        os.close(existing_fd)
                    if observed != digest:
                        raise ImportError_(
                            f"{label} target {rel} conflicts ({observed} != {digest})"
                        )
                    validate_parent()
                    return False, 0

                temp_fd = os.open(
                    temporary,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o640,
                    dir_fd=parent_fd,
                )
                try:
                    written = 0
                    with self.repository.open_blob_stream(digest) as source:
                        while True:
                            chunk = source.read(_CHUNK_SIZE)
                            if not chunk:
                                break
                            view = memoryview(chunk)
                            while view:
                                consumed = os.write(temp_fd, view)
                                view = view[consumed:]
                            written += len(chunk)
                    os.fsync(temp_fd)
                    if written != size_bytes or _hash_open_file(temp_fd) != digest:
                        raise ImportError_(
                            f"{label} staged blob verification failed: {rel}"
                        )
                    temporary_info = os.fstat(temp_fd)
                finally:
                    os.close(temp_fd)
                validate_parent()
                try:
                    os.link(
                        temporary,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    validate_parent()
                    raced_fd = os.open(
                        target_name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    try:
                        same = (
                            stat.S_ISREG(os.fstat(raced_fd).st_mode)
                            and _hash_open_file(raced_fd) == digest
                        )
                    finally:
                        os.close(raced_fd)
                    if not same:
                        raise ImportError_(
                            f"{label} target raced with different content: {rel}"
                        )
                    validate_parent()
                    return False, 0
                os.fsync(parent_fd)
                try:
                    validate_parent()
                except ImportError_:
                    try:
                        published = os.stat(
                            target_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if (published.st_dev, published.st_ino) == (
                            temporary_info.st_dev, temporary_info.st_ino,
                        ):
                            os.unlink(target_name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                    except FileNotFoundError:
                        pass
                    raise
                return True, written
            finally:
                if parent_fd is not None:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except FileNotFoundError:
                        pass
                    os.close(parent_fd)
                os.close(root_fd)

        try:
            created, written = await asyncio.to_thread(publish)
        except ImportError_:
            raise
        except OSError as exc:
            raise ImportError_(f"{label} path is unsafe or unavailable: {rel}") from exc
        if created:
            self.stats["objects_written"] += 1
            self.stats["bytes_written"] += written
        else:
            self.stats["objects_reused"] += 1


    async def _put_step_result(self, body: dict) -> str:
        job_id = body["job_id"]
        manifest = body["manifest"]
        part_id = manifest["scope"]["part_id"]
        prefix = f"parts/{part_id}/" if part_id else ""
        for entry in manifest["outputs"]:
            await self._publish_object(
                job_id, prefix + entry["path"], entry["sha256"], entry["size_bytes"],
            )
        # manifest 最后发布:中途崩溃时下游看不到 final manifest,不会误判可复用。
        await self.storage.write_file(
            job_id, manifest_relative_path(body["scope_key"], body["step"]),
            canonical_json_bytes(manifest),
        )
        return execution_step_key(body["scope_key"], body["step"])

    async def _publish_object(
        self, job_id: str, rel: str, digest: str, size_bytes: int,
    ) -> None:
        """写一个产物并 read-back 校验;已存在同 hash 跳过写入但仍校验,异 hash 拒绝。"""
        existing = await self.storage.file_size(job_id, rel)
        if existing is not None:
            observed = await self._hash_object(job_id, rel)
            if observed == digest:
                self.stats["objects_reused"] += 1
                return
            raise ImportError_(
                f"object {job_id}/{rel} exists with different content "
                f"({observed} != {digest}); import refuses to overwrite"
            )
        # 分块流式写:read_blob 只许小对象(P1 契约),真实视频整块入内存会 OOM。
        written = 0
        with self.repository.open_blob_stream(digest) as source:
            async def _chunks():
                nonlocal written
                while True:
                    chunk = await asyncio.to_thread(source.read, _CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    yield chunk

            await self.storage.write_stream(
                job_id, rel, _chunks(),
                expected_size=size_bytes, expected_sha256=digest.split(":", 1)[1],
            )
        if written != size_bytes:
            raise ImportError_(
                f"blob {digest} size disagrees with manifest for {rel} "
                f"({written} != {size_bytes})"
            )
        observed = await self._hash_object(job_id, rel)
        if observed != digest:
            raise ImportError_(f"read-back mismatch for {job_id}/{rel}: {observed}")
        self.stats["objects_written"] += 1
        self.stats["bytes_written"] += size_bytes

    async def _hash_object(self, job_id: str, rel: str) -> str | None:
        stream = await self.storage.open_stream(job_id, rel)
        if stream is None:
            return None
        hasher = hashlib.sha256()
        async for chunk in stream:
            hasher.update(chunk)
        return "sha256:" + hasher.hexdigest()


@dataclass
class _ProjectionOutcome:
    steps: int = 0
    done: int = 0
    skipped: int = 0
    waiting: int = 0
    reasons: dict = field(default_factory=dict)


def _topological_order(expanded: Mapping[str, Mapping]) -> list[str]:
    """按 depends_on 拓扑排序展开后的步骤键;成环时回退到稳定字典序。

    投影必须自上游而下:下游只有在全部上游 done/skipped 时才允许判 done,
    否则会出现"上游 waiting、下游 done"的悬空完成(scheduler/recovery.py
    的 _demote_step_and_downstream 明文防范的方向)。
    """
    pending = {key: set(cfg.get("depends_on", []) or ()) & set(expanded) for key, cfg in expanded.items()}
    ordered: list[str] = []
    while pending:
        ready = sorted(key for key, deps in pending.items() if not deps - set(ordered))
        if not ready:
            ordered.extend(sorted(pending))
            break
        for key in ready:
            ordered.append(key)
            pending.pop(key)
    return ordered


async def rebuild_projection(
    *,
    connection: sqlite3.Connection,
    storage,
    config: AppConfig,
    job_ids: Sequence[str] | None = None,
    commit_each: bool = True,
) -> dict:
    """阶段3:按当前 pipeline 重投影 job_steps 与 jobs 聚合状态。

    job_ids 限定重投影范围。empty 模式传 None(整库都是本次建的);merge 必须
    只传本次真正物化的 Job:否则会把冲突单元和与快照无关的本地 Job 一起重写,
    把它们的 status/error/progress 和运行中步骤清成 pending/waiting——而 merge
    的既定用途正是"往开发库里补 Job",那等于每次 merge 清掉全库运行态。

    全程单连接:job_steps 写入与 jobs 聚合走同一条连接、每 Job 提交一次。
    早期版本让 upsert_step 走 Database 自己的连接、聚合走本连接,两条连接
    互等对方的隐式事务,第二个 Job 起必然 "database is locked"。

    判定顺序:上游闭包 -> manifest schema -> 定义摘要 -> 产物字节 -> outcome。
    只有"全部上游 done/skipped + 当前定义摘要匹配 + 声明产物逐个 SHA 一致 +
    outcome 明确为 done/确定性 skipped"才判完成,其余一律 waiting 并记原因。
    input digest 不在此处重算(需要各步 input_hashes 的运行期上下文),由调度时
    既有复用判定负责;因此本阶段对输入保守,不会把不可复用的步判成 done。
    """
    outcome = _ProjectionOutcome()
    missing_pipelines: dict[str, str] = {}
    if job_ids is None:
        job_rows = connection.execute(
            "SELECT id, pipeline, domain, style_tags FROM jobs ORDER BY id"
        ).fetchall()
    elif job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        job_rows = connection.execute(
            f"SELECT id, pipeline, domain, style_tags FROM jobs "
            f"WHERE id IN ({placeholders}) ORDER BY id",
            tuple(job_ids),
        ).fetchall()
    else:
        job_rows = []
    for job_row in job_rows:
        job_id = job_row["id"]
        parts = [
            JobPart(id=row["id"], job_id=job_id, part_index=row["part_index"])
            for row in connection.execute(
                "SELECT id, part_index FROM job_parts WHERE job_id=? ORDER BY part_index",
                (job_id,),
            )
        ]
        pipeline_cfg = config.pipelines.get(job_row["pipeline"])
        if pipeline_cfg is None:
            # 当前配置没有这条 pipeline:不静默留空,记名后由 verify 报出来。
            missing_pipelines[job_id] = job_row["pipeline"]
            outcome.reasons[REASON_UNKNOWN_PIPELINE] = (
                outcome.reasons.get(REASON_UNKNOWN_PIPELINE, 0) + 1
            )
            _apply_job_aggregate(connection, job_id, {}, [])
            if commit_each:
                connection.commit()
            continue
        steps_list = pipeline_cfg.get("steps", [])
        expanded = expand_pipeline_steps(steps_list, parts)
        desired_steps = {
            (str(cfg.get("scope_key", "job")), str(cfg["template_step"]))
            for cfg in expanded.values()
        }
        for stale in connection.execute(
            "SELECT scope_key, step FROM job_steps WHERE job_id=?", (job_id,),
        ).fetchall():
            key = (str(stale[0]), str(stale[1]))
            if key not in desired_steps:
                connection.execute(
                    "DELETE FROM job_steps WHERE job_id=? AND scope_key=? AND step=?",
                    (job_id, *key),
                )
        try:
            style_tags = json.loads(job_row["style_tags"] or "[]")
        except ValueError:
            style_tags = []
        statuses: dict[str, str] = {}
        for key in _topological_order(expanded):
            cfg = expanded[key]
            upstream = [
                statuses.get(dependency)
                for dependency in (cfg.get("depends_on") or ())
                if dependency in expanded
            ]
            if any(value not in ("done", "skipped") for value in upstream):
                status, reason = StepStatus.WAITING, REASON_UPSTREAM_INVALID
            else:
                status, reason = await _project_step(
                    storage=storage, config=config, job_row=job_row,
                    style_tags=style_tags, cfg=cfg,
                )
            statuses[key] = status.value
            _upsert_step_row(connection, job_id, cfg, status, reason)
            outcome.steps += 1
            if status is StepStatus.DONE:
                outcome.done += 1
            elif status is StepStatus.SKIPPED:
                outcome.skipped += 1
            else:
                outcome.waiting += 1
            if reason:
                outcome.reasons[reason] = outcome.reasons.get(reason, 0) + 1
        _apply_job_aggregate(connection, job_id, statuses, list(expanded.values()))
        if commit_each:
            connection.commit()
    _rebuild_collection_counts(connection)
    if commit_each:
        connection.commit()
    return {
        "steps": outcome.steps, "done": outcome.done, "skipped": outcome.skipped,
        "waiting": outcome.waiting, "reasons": outcome.reasons,
        "jobs_with_unknown_pipeline": missing_pipelines,
    }


def _upsert_step_row(
    connection: sqlite3.Connection, job_id: str, cfg: Mapping,
    status: StepStatus, reason: str | None,
) -> None:
    """在投影连接上直接写 job_steps;与聚合同事务,避免跨连接互锁。"""
    meta = json.dumps(
        {"projection_reason": reason} if reason else {},
        ensure_ascii=False, sort_keys=True,
    )
    connection.execute(
        """INSERT INTO job_steps (job_id, scope_key, step, status, pool, meta)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(job_id, scope_key, step) DO UPDATE SET
             status=excluded.status, pool=excluded.pool,
             input_hash=NULL, worker_id=NULL, started_at=NULL, finished_at=NULL,
             duration_sec=NULL, meta=excluded.meta, error=NULL, retries=0""",
        (
            job_id, cfg.get("scope_key", "job"), cfg["template_step"],
            status.value, str(cfg.get("pool") or ""), meta,
        ),
    )


async def _sha256_object(storage, job_id: str, rel: str) -> str | None:
    stream = await storage.open_stream(job_id, rel)
    if stream is None:
        return None
    hasher = hashlib.sha256()
    async for chunk in stream:
        hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


async def _sha256_local(root: Path, rel: str) -> str | None:
    return await asyncio.to_thread(
        _hash_controlled_local, root, rel, label="local restore root",
    )


async def _materialized_record_matches(
    connection: sqlite3.Connection,
    storage,
    repository: ContentRepository,
    config_root: Path,
    source_roots: Mapping[str, Path],
    kind: str,
    digest: str,
    body: Mapping,
) -> bool:
    """逐 record 证明目标事实与快照摘要一致;续跑不信任 journal 自报。"""
    if kind == "job_core":
        return _target_job_core_digest(connection, body["id"]) == digest
    if kind == "part_core":
        matches = _target_part_core_digest(
            connection, body["job_id"], body["id"],
            include_source_blob="source_blob" in body,
        ) == digest
        if not matches or "source_blob" not in body:
            return matches
        from .source_library import SourceReferenceError, parse_source_ref

        try:
            source_ref = parse_source_ref(body.get("source_ref"))
        except SourceReferenceError:
            return False
        root = source_roots.get(source_ref.root_id)
        return root is not None and await _sha256_local(
            Path(root), source_ref.relative_path,
        ) == body["source_blob"]
    if kind == "job_user_state":
        row = connection.execute(
            "SELECT collection_id FROM jobs WHERE id=?", (body["job_id"],),
        ).fetchone()
        if row is None:
            return False
        current = {
            "job_id": body["job_id"],
            "collection_id": row[0],
            "revision": _user_state_revision(row[0], body["job_id"]),
        }
        return _digest_of(kind, current) == digest
    if kind == "step_result":
        manifest = await read_valid_manifest(
            storage, body["job_id"], body["scope_key"], body["step"],
        )
        if manifest is None or manifest != body["manifest"]:
            return False
        part_id = manifest["scope"]["part_id"]
        prefix = f"parts/{part_id}/" if part_id else ""
        for entry in manifest["outputs"]:
            if await _sha256_object(
                storage, body["job_id"], prefix + entry["path"],
            ) != entry["sha256"]:
                return False
        return _digest_of(kind, {
            "job_id": body["job_id"],
            "scope_key": body["scope_key"],
            "step": body["step"],
            "manifest": manifest,
            "output_blobs": {
                entry["path"]: entry["sha256"] for entry in manifest["outputs"]
            },
        }) == digest
    if kind == "user_config":
        if body["kind"] == "job_ai_config":
            path = PurePosixPath(body["path"])
            if len(path.parts) != 3 or path.parts[0] != "jobs":
                return False
            if body["size_bytes"] > 4 * 1024 * 1024:
                return False
            try:
                expected = json.loads(repository.read_blob(body["blob"]))
            except (RepositoryError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
                return False
            raw = await storage.read_file(path.parts[1], "job.json")
            try:
                observed = json.loads(raw) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                return False
            return (
                isinstance(expected, dict)
                and isinstance(observed, dict)
                and not (set(expected) - _JOB_AI_CONFIG_KEYS)
                and all(observed.get(key) == value for key, value in expected.items())
            )
        if body["kind"] in _GLOBAL_CONFIG_KINDS:
            try:
                rel = _Materializer._validated_config_path(
                    body["kind"], body["path"],
                )
            except ImportError_:
                return False
            return await _sha256_local(config_root, rel) == body["blob"]
        prefix, _, rel = body["path"].rpartition("/")
        return await _sha256_object(storage, prefix, rel) == body["blob"]
    if kind == "study":
        return _target_study_digest(connection, body) == digest
    if kind in _LEDGER_LOOKUP:
        return _target_ledger_digest(connection, kind, body) == digest
    if kind in ("failure_event", "legacy_archive", "job_relation"):
        # 这三类是便携仓库中的审计/关系事实,不投影为活动 DB 状态。
        return True
    raise ImportError_(f"no target closure verifier for record kind {kind!r}")


async def _verify_materialized_closure(
    connection: sqlite3.Connection,
    storage,
    repository: ContentRepository,
    config_root: Path,
    source_roots: Mapping[str, Path],
    records: Sequence[tuple[str, str, dict]],
    *,
    expected: set[str] | None = None,
) -> list[str]:
    """全量校验应已落地的 record 与对象;返回首批不一致摘要。"""
    mismatches: list[str] = []
    for kind, digest, body in records:
        if expected is not None and digest not in expected:
            continue
        if not await _materialized_record_matches(
            connection, storage, repository, config_root, source_roots,
            kind, digest, body,
        ):
            mismatches.append(f"{kind}:{_natural_key_of(kind, body)}:{digest}")
            if len(mismatches) >= 20:
                break
    return mismatches


def _job_ids_for_digests(
    records: Sequence[tuple[str, str, dict]], digests: set[str],
) -> set[str]:
    result: set[str] = set()
    for kind, digest, body in records:
        if digest not in digests:
            continue
        job_id = body.get("job_id") or (body.get("id") if kind == "job_core" else None)
        if isinstance(job_id, str) and job_id:
            result.add(job_id)
    return result


async def _project_step(
    *, storage, config: AppConfig, job_row: sqlite3.Row,
    style_tags: list, cfg: Mapping,
) -> tuple[StepStatus, str | None]:
    job_id = job_row["id"]
    scope_key = cfg.get("scope_key", "job")
    step_name = cfg["template_step"]
    # 走既有单一来源:read_valid_manifest 内含 validate_manifest,structurally
    # 非法的 manifest 返回 None,不会被当成"字段缺失但可用"。
    manifest = await read_valid_manifest(storage, job_id, scope_key, step_name)
    if manifest is None:
        return StepStatus.WAITING, REASON_MISSING_MANIFEST
    try:
        current_digest = step_definition_digest_for(
            job_row["pipeline"], cfg, config=config,
            domain=job_row["domain"] or "", style_tags=style_tags,
        )
    except Exception:  # noqa: BLE001 - 定义不可解析等同不兼容,不让投影中止
        return StepStatus.WAITING, REASON_STALE_MANIFEST
    if manifest["compatibility"]["definition_digest"] != current_digest:
        return StepStatus.WAITING, REASON_STALE_MANIFEST
    if not await verify_manifest_outputs_metadata(storage, job_id, manifest):
        return StepStatus.WAITING, REASON_OUTPUT_MISMATCH
    # size 相等不足以证明字节相同:同名同长度的损坏/篡改产物会被判 done,
    # 而这些对象未必经过本次 materialize 的 read-back。逐个流式重算。
    part_id = manifest["scope"]["part_id"]
    prefix = f"parts/{part_id}/" if part_id else ""
    for entry in manifest["outputs"]:
        observed = await _sha256_object(storage, job_id, prefix + entry["path"])
        if observed != entry["sha256"]:
            return StepStatus.WAITING, REASON_OUTPUT_MISMATCH
    outcome = manifest["outcome"]
    if outcome == "skipped":
        if manifest["skip"]["reason_code"] not in DETERMINISTIC_SKIP_REASONS:
            return StepStatus.WAITING, REASON_STALE_MANIFEST
        return StepStatus.SKIPPED, None
    if outcome == "done":
        return StepStatus.DONE, None
    # 未知 outcome 一律保守:不给完成态兜底出口。
    return StepStatus.WAITING, REASON_STALE_MANIFEST


def _apply_job_aggregate(
    connection: sqlite3.Connection, job_id: str, statuses: Mapping[str, str],
    steps_config: Sequence[Mapping],
) -> None:
    """由重投影后的步骤状态聚合 jobs.status/progress_pct;绝不来自 snapshot。

    进度算式与 scheduler 的 _calc_progress 保持一致(权重和 + round),
    否则同一 Job 在导入后与下一次调度后会显示两个百分比。
    """
    done_weight = sum(
        cfg.get("weight", 1) for cfg in steps_config
        if statuses.get(cfg["name"]) in ("done", "skipped")
    )
    total_weight = sum(cfg.get("weight", 1) for cfg in steps_config)
    progress = round(100 * done_weight / max(total_weight, 1))
    finished = sum(1 for value in statuses.values() if value in ("done", "skipped"))
    if statuses and finished == len(statuses):
        status = "done"
    else:
        # 恢复只重建可复用状态,不等于授权开始执行。显式 activate 后才进入 pending,
        # scheduler 启动恢复也因此不会把刚导入的任务自动跑起来。
        status = "pending_activation"
    connection.execute(
        "UPDATE jobs SET status=?, progress_pct=?, error=NULL, updated_at=? WHERE id=?",
        (status, progress, _now(), job_id),
    )


def _rebuild_collection_counts(connection: sqlite3.Connection) -> None:
    connection.execute(
        """UPDATE collections SET job_count=(
               SELECT COUNT(*) FROM jobs WHERE jobs.collection_id=collections.id
           )"""
    )


async def rebuild_search_index(
    *, database: Database, connection: sqlite3.Connection, storage, config: AppConfig,
) -> dict:
    """检查笔记索引的重建归属,不在导入侧写 notes_fts5。

    重要的所有权决定:笔记索引 + canonical_evidence 由 scheduler 既有的幂等补齐
    通道负责(JobFinalizer.reconcile_completion_effects,scheduler/job_finalizer.py:63,
    由 background._periodic_loop 每 30s 触发)。它的谓词是
    list_unindexed_done_jobs —— "status=done 且 notes_fts5 里没有该 job"
    (shared/repositories/search.py:25)。

    所以导入侧绝不能先把 notes_fts5 填上:那会让谓词永远为假,scheduler 再也
    不会认领这些 Job,canonical_evidence 于是永久为空。早期版本正是这么写的,只因
    它读的 config 键(pipelines.<name>.index)在 configs/pipelines.yaml 里根本不存在
    (真实位置是 steps.<name>.on_complete[].candidates)而恰好没生效 —— 一个 bug
    盖住了另一个 bug。这里把归属显式交还给 scheduler,并报告待补齐的 Job 数,
    让"恢复后启动 scheduler 即补齐"成为可验证的步骤而不是巧合。

    index_note 成功后会走纯 CPU concept occurrence 重放;它只替换出现投影,
    不新增术语、不触发概念综合或其他 AI 副作用。
    """
    pending = [
        row[0] for row in connection.execute(
            """SELECT id FROM jobs WHERE status='done' AND is_current=1
               ORDER BY created_at LIMIT 200"""
        )
    ]
    return {
        "notes_indexed": 0,
        "owned_by": "scheduler.JobFinalizer.reconcile_completion_effects",
        "awaiting_backfill": pending,
        "deferred": [],
        "note": (
            "start the scheduler against this database to backfill notes_fts5 / "
            "note_chunks / canonical_evidence / concept_occurrences"
        ),
    }


def verify_target(
    *, database: Database, connection: sqlite3.Connection, plan: ImportPlan,
    projection: Mapping | None = None,
) -> dict:
    """阶段4 验收:migration validator + FK + integrity,以及来自新投影的统计。"""
    from .migrations import migration_steps

    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise ImportError_(f"target integrity_check failed: {integrity}")
    fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise ImportError_(f"target has {len(fk_errors)} foreign key violations")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise ImportError_(f"target schema v{version} != current v{SCHEMA_VERSION}")
    validator = migration_steps()[-1].validate
    if validator is not None:
        try:
            validator(connection)
        except Exception as exc:
            raise ImportError_(f"target schema validator failed: {exc}") from exc
    # Part 清单连续性(§2.10 阶段4-3);validator 已覆盖全库,这里给出可读统计。
    counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("jobs", "job_parts", "job_steps", "collections", "ai_usage")
    }
    status_rows = connection.execute(
        "SELECT status, COUNT(*) FROM job_steps GROUP BY status"
    ).fetchall()
    result = {
        "counts": counts,
        "step_statuses": {row[0]: int(row[1]) for row in status_rows},
        "schema_version": version,
    }
    # 快照里的 pipeline 在当前配置缺失时,这些 Job 一步都展不开;必须显名报出,
    # 否则它们只是"步骤数为 0"地静默留在 pending。
    unknown = dict((projection or {}).get("jobs_with_unknown_pipeline", {}))
    if unknown:
        result["jobs_with_unknown_pipeline"] = unknown
    return result


async def run_import(
    *,
    repository: ContentRepository,
    snapshot: str,
    target_db_path: Path,
    storage,
    journal_path: Path,
    target_generation: str,
    config_dir: Path | None = None,
    allow_partial: bool = False,
    verify_blobs: bool = True,
    rebuild_index: bool = True,
    mode: str = MODE_EMPTY,
    apply_user_state: bool = False,
    live_authorization: dict | None = None,
    into_live: bool = False,
    allow_incomplete_portable_snapshot: bool = False,
    config_root: Path | None = None,
    source_roots: Mapping[str, Path] | None = None,
    expected_source_root_identities: Mapping[str, str] | None = None,
) -> ImportResult:
    """按 DB + storage namespace 独占后执行导入,所有调用方共用同一道门。"""
    live_resources: tuple[str, ...] = ()
    if live_authorization is not None:
        raw_resources = live_authorization.get("maintenance_resources", ())
        if not isinstance(raw_resources, (list, tuple)) or not all(
            isinstance(item, str) for item in raw_resources
        ):
            raise MaintenanceLockError("live authorization has invalid maintenance resources")
        live_resources = tuple(raw_resources)
    selected_config_root = Path(
        config_root
        if config_root is not None
        else DEFAULT_LIVE_CONFIG_ROOT if into_live else DEFAULT_CONFIG_STAGING
    )
    if not selected_config_root.is_absolute():
        raise ImportError_("config root must be an absolute path")
    selected_config_root = Path(os.path.abspath(selected_config_root))
    normalized_source_roots = _normalized_source_roots(source_roots)
    source_root_identities = _verify_source_root_identities(
        normalized_source_roots, expected_source_root_identities,
    )
    extra_resources = set(live_resources)
    extra_resources.update(path_resources("config-root", selected_config_root))
    for root in normalized_source_roots.values():
        extra_resources.update(path_resources("source-root", root))
    # 一次按全资源排序拿齐锁,避免先拿 target 再拿 config/source/live root 的 ABBA 死锁。
    with acquire_import_target_lease(
        db_path=target_db_path,
        storage=storage,
        extra_resources=extra_resources,
    ):
        return await _run_import_locked(
            repository=repository,
            snapshot=snapshot,
            target_db_path=target_db_path,
            storage=storage,
            journal_path=journal_path,
            target_generation=target_generation,
            config_dir=config_dir,
            allow_partial=allow_partial,
            verify_blobs=verify_blobs,
            rebuild_index=rebuild_index,
            mode=mode,
            apply_user_state=apply_user_state,
            into_live=into_live,
            allow_incomplete_portable_snapshot=allow_incomplete_portable_snapshot,
            config_root=selected_config_root,
            source_roots=normalized_source_roots,
            source_root_identities=source_root_identities,
        )


async def _run_import_locked(
    *,
    repository: ContentRepository,
    snapshot: str,
    target_db_path: Path,
    storage,
    journal_path: Path,
    target_generation: str,
    config_dir: Path | None = None,
    allow_partial: bool = False,
    verify_blobs: bool = True,
    rebuild_index: bool = True,
    mode: str = MODE_EMPTY,
    apply_user_state: bool = False,
    into_live: bool = False,
    allow_incomplete_portable_snapshot: bool = False,
    config_root: Path | None = None,
    source_roots: Mapping[str, Path] | None = None,
    source_root_identities: Mapping[str, str] | None = None,
) -> ImportResult:
    """导入全流程(§2.10 阶段0-4);失败抛 ImportError_ 且不切换任何配置。

    mode=empty 要求目标库空;mode=merge 按 §2.9 七条规则分类后只物化干净单元,
    冲突单元根本不写(不是写完回滚)。

    resume 由 journal 驱动:同 (snapshot, target_generation) 已有未完成条目时,
    跳过已登记的 record,不从零复制已验证的大文件。
    """
    target_db_path = Path(target_db_path)
    resolved = _resolve_snapshot(repository, snapshot)
    import_id = "imp_" + hashlib.sha256(
        f"{resolved}:{target_generation}".encode()
    ).hexdigest()[:24]

    # 幂等判定先于计划:已完成的同 (snapshot, generation) 直接说清楚"已导入过",
    # 否则操作者只会看到"目标库非空"这种下游症状(§2.9 重复导入即 no-op)。
    # 裸 digest 导入不被任何 ref 指着,并发 GC 可能正好把它清掉;
    # 导入期间挂一个短命保活 ref,结束即摘。
    # 仓库按只读挂载是恢复路径的安全属性(它可能是最后一份拷贝),优先级高于保活 ref。
    # 只读时 set_ref 抛的是 OSError 而非 RepositoryError:必须一起接住,否则按 digest
    # 导入在出货入口直接崩(P4 演练实测)。降级后只是失去并发 GC 保护,导入本身照常。
    guard_ref = f"import-{import_id}"
    guard_added = False
    guard_error: str | None = None
    try:
        if snapshot.startswith("sha256:"):
            repository.set_ref(guard_ref, resolved)
            guard_added = True
    except (RepositoryError, OSError) as exc:
        guard_error = str(exc)
        _log.warning("import_guard_ref_failed", ref=guard_ref, error=guard_error)

    # journal 构造必须在 try 之内:它在 try 之外时,一次构造失败就让上面刚挂上的
    # 保活 ref 永久留在仓库里(无 TTL、无回收),GC 从此再也标记不到那个 snapshot。
    journal: ContentImportJournal | None = None
    try:
        journal = ContentImportJournal(journal_path)
        config = load_config(config_dir) if config_dir else load_config()
        selected_config_root = Path(
            config_root
            if config_root is not None
            else DEFAULT_LIVE_CONFIG_ROOT if into_live else DEFAULT_CONFIG_STAGING
        )
        if not selected_config_root.is_absolute():
            raise ImportError_("config root must be an absolute path")
        selected_config_root = Path(os.path.abspath(selected_config_root))
        normalized_source_roots = _normalized_source_roots(source_roots)
        actual_source_root_identities = _verify_source_root_identities(
            normalized_source_roots, source_root_identities,
        )
        live_prompt_roots = {
            Path(os.path.abspath(Path(config.prompts_dir))),
            Path(os.path.abspath(Path(DEFAULT_LIVE_CONFIG_ROOT))),
        }
        if (
            not into_live
            and selected_config_root in live_prompt_roots
        ):
            raise ImportError_(
                "isolated import refuses to write the active prompts root; use a "
                "staging --config-root or authorize --into-live"
            )
        target_path = str(target_db_path.resolve())
        storage_identity = _storage_identity(storage)
        incomplete_override = bool(
            into_live
            and allow_incomplete_portable_snapshot
            and os.environ.get("FLORI_ACCEPT_INCOMPLETE_PORTABLE") == "1"
        )
        request = ImportRequest(
            snapshot_digest=resolved,
            target_db_path=target_path,
            target_storage=storage_identity,
            mode=mode,
            apply_user_state=apply_user_state,
            allow_partial=allow_partial,
            schema_version=SCHEMA_VERSION,
            config_digest=_config_digest(config),
            target_config_root=str(selected_config_root),
            target_source_roots=tuple(
                (key, str(path), actual_source_root_identities[key])
                for key, path in sorted(normalized_source_roots.items())
            ),
            allow_incomplete_portable_snapshot=incomplete_override,
        )
        # 幂等短路必须先核对整个请求和物理目标;同 generation
        # 换库或换桶时不得把旧 complete 当作新目标的成功证据。
        done = journal.find(resolved, target_generation)
        completed_replay = False
        if done is not None and done.status == STATUS_COMPLETE and mode == MODE_EMPTY:
            current_token = _target_binding_token(target_db_path)
            if (
                not done.request_digest
                or done.request_digest != request.digest
                or done.mode != mode
                or done.target_db_path != target_path
                or not done.target_token
                or current_token != done.target_token
                or done.target_storage != storage_identity
            ):
                raise ImportError_(
                    f"completed journal import {done.import_id} is bound to a different "
                    "database, storage namespace, mode, or import policy; use a new "
                    "--target-generation"
                )
            completed_replay = True
        replay_merge = (
            mode == MODE_MERGE and done is not None and done.status == STATUS_COMPLETE
        )
        if replay_merge:
            # merge 可重复执行(第二次应全 no-op);旧判定账本要等 begin 成功后再清,
            # 先清后 begin 会在 begin 失败时把上一轮的审计一起毁掉。
            done = None
        # 续跑资格:条目处于未完成态、有已登记进度、且目标库绑定 token 仍然一致。
        pending = done
        resuming = False
        if pending is not None and pending.status != STATUS_COMPLETE:
            if not pending.request_digest or pending.request_digest != request.digest:
                raise ImportError_(
                    f"journal progress for {pending.import_id} belongs to a different "
                    "database, storage namespace, mode, or import policy; use a new "
                    "--target-generation"
                )
            current_token = _target_binding_token(target_db_path)
            if pending.target_token:
                same_target = (
                    pending.target_db_path == target_path
                    and current_token == pending.target_token
                    and pending.target_storage == storage_identity
                )
                if not same_target:
                    raise ImportError_(
                        f"journal progress for {pending.import_id} was recorded against a "
                        "different target database or storage namespace. Use a new "
                        "--target-generation"
                    )
                # 已绑定的 pending/failed 即使尚无 processed record 也必须续跑:
                # 第一次 DB commit 与 journal mark 之间硬崩正是零进度窗口。
                resuming = True
        classification: MergeClassification | None = None
        if mode == MODE_MERGE:
            classification = await _classify_for_merge(
                repository=repository, snapshot=resolved,
                target_db_path=target_db_path, storage=storage,
                apply_user_state=apply_user_state,
                config_root=selected_config_root,
                source_roots=normalized_source_roots,
            )
        plan, body, records = build_plan(
            repository=repository, snapshot=resolved, target_db_path=target_db_path,
            allow_partial=allow_partial, verify_blobs=verify_blobs,
            resuming=resuming or completed_replay, config=config, mode=mode,
            classification=classification,
            request=request,
        )
        if mode == MODE_MERGE:
            # merge 的冲突不是"停止导入",而是"这些单元不动";其余照常物化。
            plan.conflicts = [
                item for item in plan.conflicts
                if "merge conflicts" not in item
            ]
        if into_live and not plan.portable_ready and not incomplete_override:
            raise ImportError_(
                "snapshot is not portable-ready "
                f"({plan.readiness_reason}); writing a live target requires both "
                "--allow-incomplete-portable-snapshot and "
                "FLORI_ACCEPT_INCOMPLETE_PORTABLE=1"
            )
        if not plan.ok:
            raise ImportError_(
                "import plan has unresolved conflicts: " + "; ".join(plan.conflicts)
            )
        if completed_replay:
            probe = sqlite3.connect(target_db_path)
            probe.row_factory = sqlite3.Row
            try:
                mismatches = await _verify_materialized_closure(
                    probe, storage, repository, selected_config_root,
                    normalized_source_roots, records,
                )
                if mismatches:
                    raise ImportError_(
                        "completed journal target no longer matches the imported "
                        f"snapshot: {mismatches[:5]}"
                    )
                database = Database(target_db_path)
                try:
                    verify_target(
                        database=database, connection=probe, plan=plan,
                        projection=done.summary.get("projection", {}),
                    )
                finally:
                    database.close()
            finally:
                probe.close()
            raise _AlreadyImported(
                f"snapshot {resolved} already imported into generation "
                f"{target_generation} (import {done.import_id}); nothing to do"
            )
        try:
            entry = journal.begin(
                import_id=import_id, snapshot_digest=plan.snapshot_digest,
                target_generation=target_generation, plan_digest=plan.plan_digest,
                request_digest=request.digest, mode=mode,
            )
        except ImportJournalError as exc:
            raise ImportError_(str(exc)) from exc
        if replay_merge:
            journal.clear_records(import_id)

        # 阶段1:用当前代码 migrations 建全新 schema,绝不复制 snapshot 的 ledger。
        # 续跑时不重跑 init_schema:schema 早已就位,而它附带的数据不变量
        # (如"video job 必有 Part")在物化中断的半成品库上必然为假,
        # 那是本次要补完的状态,不是建库失败。完整性由结尾的 verify_target 把关。
        database = Database(target_db_path)
        if not resuming:
            database.init_schema()
        connection = sqlite3.connect(target_db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            token = _target_binding_token(target_db_path)
            journal.bind_target(
                import_id, target_path, token, storage_identity,
            )
            actions = journal.processed_actions(import_id) if resuming else {}
            processed = set(actions)
            resume_projection_jobs = _job_ids_for_digests(
                records,
                {digest for digest, action in actions.items() if action == ACTION_INSERT},
            )
            if resuming:
                must_exist = {
                    digest for digest, action in actions.items()
                    if action in (ACTION_INSERT, ACTION_NOOP)
                }
                mismatches = await _verify_materialized_closure(
                    connection, storage, repository, selected_config_root,
                    normalized_source_roots, records, expected=must_exist,
                )
                if mismatches:
                    _log.warning(
                        "content_import_progress_invalidated",
                        import_id=import_id, mismatches=mismatches[:3],
                    )
                    journal.clear_records(import_id)
                    processed = set()
                    resuming = False
            resumed = resuming
            journal.set_status(import_id, STATUS_MATERIALIZING)
            materializer = _Materializer(
                repository=repository, connection=connection, storage=storage,
                journal=journal, import_id=import_id, processed=processed,
                config_root=selected_config_root,
                source_roots=normalized_source_roots,
                commit_each=classification is None,
            )
            selected = (
                classification.clean_records(records)
                if classification is not None else records
            )
            merge_transaction = classification is not None
            if merge_transaction:
                connection.execute("BEGIN IMMEDIATE")
            try:
                if classification is not None:
                    locked = await classify_merge(
                        connection=connection, storage=storage,
                        repository=repository,
                        config_root=selected_config_root,
                        source_roots=normalized_source_roots,
                        records=records,
                        apply_user_state=apply_user_state,
                    )
                    if locked.actions != classification.actions:
                        raise ImportError_(
                            "merge target changed between plan and locked write; retry "
                            "after all writers are quiesced"
                        )
                await materializer.materialize(selected)
                if classification is None:
                    _assert_job_relations(records, materializer.handled)
                else:
                    _assert_job_relations(
                        records, materializer.handled,
                        extra_known={
                            digest for digest, action in classification.actions.items()
                            if action in (ACTION_NOOP, ACTION_SKIP, ACTION_CONFLICT)
                        },
                    )
                    _record_skipped(journal, import_id, classification, records)

                journal.set_status(import_id, STATUS_PROJECTING)
                projected_jobs = None
                if classification is not None:
                    projected_jobs = sorted(
                        _materialized_job_ids(materializer, selected)
                        | resume_projection_jobs
                    )
                projection = await rebuild_projection(
                    connection=connection, storage=storage, config=config,
                    job_ids=projected_jobs, commit_each=not merge_transaction,
                )
                if projected_jobs is not None:
                    projection["projected_jobs"] = projected_jobs
                if rebuild_index:
                    projection["search"] = await rebuild_search_index(
                        database=database, connection=connection,
                        storage=storage, config=config,
                    )
                expected = None
                if classification is not None:
                    expected = {
                        digest for digest, action in classification.actions.items()
                        if action in (ACTION_INSERT, ACTION_NOOP)
                    }
                mismatches = await _verify_materialized_closure(
                    connection, storage, repository, selected_config_root,
                    normalized_source_roots, records, expected=expected,
                )
                if mismatches:
                    raise ImportError_(
                        "target does not contain the complete imported record/object "
                        f"closure: {mismatches[:5]}"
                    )
                verification = verify_target(
                    database=database, connection=connection, plan=plan,
                    projection=projection,
                )
                if merge_transaction:
                    connection.commit()
            except BaseException:
                if merge_transaction and connection.in_transaction:
                    connection.rollback()
                # merge 的对象发布不能随 SQLite 回滚。保留 insert action
                # 让下次续跑强制投影相关 Job,并逐对象重验之前的进度。
                raise
        finally:
            connection.close()
            database.close()

        summary = {
            "materialized": materializer.stats,
            "projection": projection,
            "verification": verification,
        }
        journal.set_status(import_id, STATUS_COMPLETE, summary=summary)
        return ImportResult(
            import_id=import_id, snapshot_digest=plan.snapshot_digest, plan=plan,
            materialized=materializer.stats, projection=projection,
            verification=verification, resumed=resumed,
            snapshot_guard={"held": guard_added, "error": guard_error},
            merge_report={
                "mode": mode,
                "counts": dict(classification.counts),
                "conflicts": [item.as_dict() for item in classification.conflicts],
                "conflicted_units": sorted(classification.conflicted_units),
                "user_state_kept": classification.user_state_kept,
            } if classification is not None else {"mode": mode},
        )
    except _AlreadyImported:
        # 幂等短路不是失败:绝不能把 status=complete 改写成 failed。
        raise
    except BaseException as exc:
        try:
            if journal is not None:
                current = journal.find(resolved, target_generation)
                if current is None or current.status != STATUS_COMPLETE:
                    journal.set_status(
                        import_id, STATUS_FAILED, summary={"error": str(exc)[:2000]},
                    )
        except Exception as journal_exc:  # noqa: BLE001
            _log.warning("content_import_journal_status_failed", error=str(journal_exc))
        raise
    finally:
        # 保活 ref 必须摘掉,否则每次按 digest 导入都在仓库里留一个永久引用,
        # GC 标记阶段从全部 refs 出发 -> 被导入过的 snapshot 永远不可回收。
        if guard_added:
            try:
                repository.delete_ref(guard_ref)
            except (RepositoryError, OSError) as exc:  # noqa: BLE001
                _log.warning(
                    "import_guard_ref_release_failed", ref=guard_ref, error=str(exc),
                )
        if journal is not None:
            journal.close()




def _emit(payload: dict, result_file: ResultDestination | None) -> None:
    emit_result(payload, result_file)


def _plan_payload(plan: ImportPlan) -> dict:
    return {
        "snapshot_digest": plan.snapshot_digest,
        "plan_digest": plan.plan_digest,
        "request_digest": plan.request_digest,
        "counts": plan.counts,
        "bytes_to_write": plan.bytes_to_write,
        "disk_free_bytes": plan.disk_free_bytes,
        "partial": plan.partial,
        "job_ids": plan.job_ids,
        "conflicts": plan.conflicts,
        # 字段名不能叫 mode:CLI 的 payload 用 mode 表示动作(plan/verify-only),
        # 同名会被展开覆盖掉。
        "target_mode": plan.mode,
        "merge_conflicts": plan.merge_conflicts,
        "user_state_kept": plan.user_state_kept,
        "snapshot_format": plan.snapshot_format,
        "portable_ready": plan.portable_ready,
        "readiness_reason": plan.readiness_reason,
        "readiness_reasons": plan.readiness_reasons,
        "completeness": plan.completeness,
        "config_root": plan.config_root,
        "required_source_roots": plan.required_source_roots,
        "missing_source_root_mappings": plan.missing_source_root_mappings,
        "global_config_files": plan.global_config_files,
        "global_config_bytes": plan.global_config_bytes,
        "vendored_source_parts": plan.vendored_source_parts,
        "vendored_source_bytes": plan.vendored_source_bytes,
        "ok": plan.ok,
    }


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器;独立出来是为了让脚本发出的 argv 能喂进真解析器对账。"""
    parser = argparse.ArgumentParser(prog="content-import", description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--snapshot", default="latest", help="ref 名或 sha256: 摘要")
    parser.add_argument("--db", required=True, help="目标 SQLite(须不存在或为空 schema)")
    parser.add_argument(
        "--jobs-dir", default=DEFAULT_IMPORT_STAGING,
        help=f"对象写入根;默认隔离 staging {DEFAULT_IMPORT_STAGING}",
    )
    parser.add_argument(
        "--journal", default=DEFAULT_JOURNAL_PATH,
        help=f"独立 journal 文件(默认 {DEFAULT_JOURNAL_PATH};不得放在目标库目录内)",
    )
    parser.add_argument("--target-generation", default=None)
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--target", choices=(MODE_EMPTY, MODE_MERGE), default=MODE_EMPTY)
    parser.add_argument(
        "--apply-user-state", action="store_true",
        help="merge 模式下允许用快照的用户状态覆盖目标(默认保留目标并报告)",
    )
    parser.add_argument("--plan", action="store_true", help="只出计划,不写任何东西")
    parser.add_argument(
        "--verify-only", action="store_true",
        help="只做仓库全链完整性核验(逐 blob 重算),不看目标库与磁盘",
    )
    parser.add_argument(
        "--object-bucket", default=None,
        help="对象存储模式下的写入桶;隔离导入必须给出与生产桶不同的名字",
    )
    parser.add_argument(
        "--config-root",
        default=None,
        help=(
            "目标 prompts 根;隔离默认 /data/import-staging/prompts,"
            "--into-live 默认 /data/prompts"
        ),
    )
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        metavar="ROOT_ID=TARGET_DIR",
        help="vendored NAS 原媒体恢复映射;可重复指定且不猜生产路径",
    )
    parser.add_argument(
        "--source-root-identity",
        action="append",
        default=[],
        metavar="ROOT_ID=DEV:INO",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--into-live", action="store_true",
        help="确认本次要写线上面(库/产物根/生产桶);需配合 --dr-receipt",
    )
    parser.add_argument(
        "--dr-receipt", default=None,
        help="最近一次 exact DR 的 result JSON;写线上面时必需且会被解析校验",
    )
    parser.add_argument(
        "--allow-incomplete-portable-snapshot",
        action="store_true",
        help=(
            "接受不完整 portable snapshot 写入线上面;还必须显式设置 "
            "FLORI_ACCEPT_INCOMPLETE_PORTABLE=1"
        ),
    )
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--skip-index-rebuild", action="store_true")
    parser.add_argument("--list-imports", action="store_true", help="列出 journal 内全部导入")
    parser.add_argument("--result-file", default=None)
    return parser


def _parse_source_root_args(values: Sequence[str]) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        root_id, separator, raw_path = value.partition("=")
        if not separator or not root_id or not raw_path or root_id in parsed:
            raise ImportError_(
                f"invalid --source-root {value!r}; expected unique ROOT_ID=TARGET_DIR"
            )
        parsed[root_id] = Path(raw_path)
    return _normalized_source_roots(parsed)


def _parse_source_root_identity_args(values: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        root_id, separator, identity = value.partition("=")
        pieces = identity.split(":")
        if (
            not separator or not root_id or root_id in parsed
            or len(pieces) != 2 or any(not piece.isdigit() for piece in pieces)
        ):
            raise ImportError_(
                f"invalid --source-root-identity {value!r}; expected unique ROOT_ID=DEV:INO"
            )
        parsed[root_id] = identity
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        source_roots = _parse_source_root_args(args.source_root)
        expected_source_root_identities = _parse_source_root_identity_args(
            args.source_root_identity,
        )
        if source_roots and not expected_source_root_identities:
            raise ImportError_(
                "--source-root requires wrapper-bound --source-root-identity values"
            )
        source_root_identities = _verify_source_root_identities(
            source_roots, expected_source_root_identities,
        )
    except ImportError_ as exc:
        emit_result({"ok": False, "error": str(exc)}, None)
        return 2
    config_root = Path(
        args.config_root
        or (DEFAULT_LIVE_CONFIG_ROOT if args.into_live else DEFAULT_CONFIG_STAGING)
    )
    if not config_root.is_absolute():
        emit_result({"ok": False, "error": "--config-root must be absolute"}, None)
        return 2

    target_db = Path(args.db)
    journal_path = Path(args.journal)
    jobs_root = Path(args.jobs_dir)
    try:
        container_data_root = Path("/data") if Path("/data") in target_db.parents else None
        target_roots = (
            *((container_data_root,) if container_data_root else ()),
            jobs_root,
            config_root,
            journal_path.parent,
            *source_roots.values(),
        )
        ensure_output_roots_disjoint((Path(args.repo),), target_roots)
        args.result_file = prepare_result_destination(
            args.result_file,
            args.repo,
            protected_roots=target_roots,
            protected_files=(target_db, journal_path),
        )
    except ResultFileError as exc:
        emit_result({"ok": False, "error": str(exc)}, None)
        return 2

    if args.list_imports:
        try:
            with ContentImportJournal(Path(args.journal)) as journal:
                entries = [
                    {
                        "import_id": item.import_id,
                        "snapshot_digest": item.snapshot_digest,
                        "target_generation": item.target_generation,
                        "target_db_path": item.target_db_path,
                        "status": item.status,
                        "started_at": item.started_at,
                        "completed_at": item.completed_at,
                    }
                    for item in journal.list_all()
                ]
        except ImportJournalError as exc:
            _emit({"ok": False, "error": str(exc)}, args.result_file)
            return 1
        _emit({"ok": True, "mode": "list-imports", "imports": entries}, args.result_file)
        return 0

    try:
        repository = ContentRepository.open(Path(args.repo))
    except (RepositoryError, PolicyError) as exc:
        _emit({"ok": False, "error": str(exc)}, args.result_file)
        return 1

    if target_db.parent.resolve() in journal_path.resolve().parents or \
            journal_path.resolve().parent == target_db.parent.resolve():
        _emit({
            "ok": False,
            "error": f"journal {journal_path} sits inside the target database directory "
                     f"{target_db.parent}; stage-5 discards that directory and would take "
                     "the only crash evidence with it",
        }, args.result_file)
        return 2

    if args.verify_only:
        # 仓库完整性核验与目标无关:不查空库门、不查磁盘,逐 blob 全量重算。
        try:
            plan, _body, _records = build_plan(
                repository=repository, snapshot=args.snapshot,
                target_db_path=target_db, allow_partial=True,
                verify_blobs=True, resuming=True,
            )
        except (ImportError_, RepositoryError, PolicyError) as exc:
            _emit({"ok": False, "mode": "verify-only", "error": str(exc)}, args.result_file)
            return 1
        _emit({
            "ok": True, "mode": "verify-only",
            "snapshot_digest": plan.snapshot_digest,
            "snapshot_format": plan.snapshot_format,
            "portable_ready": plan.portable_ready,
            "readiness_reason": plan.readiness_reason,
            "completeness": plan.completeness,
            "records_verified": plan.counts["insert"],
            "blobs_verified": plan.counts["blobs"],
            "bytes_verified": plan.bytes_to_write,
        }, args.result_file)
        return 0

    # 只读路径(--plan/--verify-only)不过写入门:恢复流程第 1 步就是对着线上库
    # 出计划,把它们一起拦掉等于让门自己不可用。
    storage = create_import_storage(Path(args.jobs_dir), object_bucket=args.object_bucket)
    if args.plan:
        # 计划是决策前的快速预演:跳过逐 blob 重算(那是 --verify-only 的活)。
        # merge 下必须先分类再出计划,否则计划里的 plan_digest 与真实导入不一致。
        try:
            config = load_config(args.config_dir) if args.config_dir else load_config()
            request = ImportRequest(
                snapshot_digest=_resolve_snapshot(repository, args.snapshot),
                target_db_path=str(target_db.resolve()),
                target_storage=_storage_identity(storage),
                mode=args.target,
                apply_user_state=args.apply_user_state,
                allow_partial=args.allow_partial,
                schema_version=SCHEMA_VERSION,
                config_digest=_config_digest(config),
                target_config_root=str(Path(os.path.abspath(config_root))),
                target_source_roots=tuple(
                    (key, str(path), source_root_identities[key])
                    for key, path in sorted(source_roots.items())
                ),
                allow_incomplete_portable_snapshot=bool(
                    args.into_live
                    and args.allow_incomplete_portable_snapshot
                    and os.environ.get("FLORI_ACCEPT_INCOMPLETE_PORTABLE") == "1"
                ),
            )
            classification = None
            if args.target == MODE_MERGE:
                classification = asyncio.run(_classify_for_merge(
                    repository=repository, snapshot=args.snapshot,
                    target_db_path=target_db, storage=storage,
                    apply_user_state=args.apply_user_state,
                    config_root=Path(os.path.abspath(config_root)),
                    source_roots=source_roots,
                ))
            plan, _body, _records = build_plan(
                repository=repository, snapshot=args.snapshot,
                target_db_path=target_db, allow_partial=args.allow_partial,
                verify_blobs=False, config=config, mode=args.target,
                classification=classification,
                request=request,
            )
        except (ImportError_, RepositoryError, PolicyError) as exc:
            _emit({"ok": False, "error": str(exc)}, args.result_file)
            return 1
        _emit({"ok": plan.ok, "mode": "plan", **_plan_payload(plan)}, args.result_file)
        return 0 if plan.ok else 1

    # 写入门在这里,不在 shell:shell 只能比字符串,真正决定写到哪里的是本进程。
    # 判定依据是目标身份而非 flag,所以显式 --jobs-dir /data/jobs 这类绕过默认
    # 分支的写法照样被拦。
    try:
        authorization = assert_write_authorized(
            db_path=target_db, jobs_dir=Path(args.jobs_dir),
            object_bucket=args.object_bucket, config_root=config_root,
            source_roots=list(source_roots.values()),
            into_live=args.into_live,
            dr_receipt=args.dr_receipt or os.environ.get("FLORI_DR_RECEIPT") or None,
        )
    except LiveTargetError as exc:
        _emit({"ok": False, "mode": "import", "error": str(exc)}, args.result_file)
        return 2

    generation = args.target_generation or datetime.now(timezone.utc).strftime(
        "gen-%Y%m%dT%H%M%SZ"
    )
    try:
        result = asyncio.run(run_import(
            repository=repository,
            snapshot=args.snapshot,
            target_db_path=target_db,
            storage=storage,
            journal_path=journal_path,
            target_generation=generation,
            config_dir=Path(args.config_dir) if args.config_dir else None,
            allow_partial=args.allow_partial,
            rebuild_index=not args.skip_index_rebuild,
            mode=args.target,
            apply_user_state=args.apply_user_state,
            live_authorization=authorization,
            into_live=args.into_live,
            allow_incomplete_portable_snapshot=(
                args.allow_incomplete_portable_snapshot
            ),
            config_root=config_root,
            source_roots=source_roots,
            expected_source_root_identities=source_root_identities,
        ))
    except _AlreadyImported as exc:
        # 重复导入是设计中的 no-op(§2.9),不是失败:自动化按退出码判生死,
        # 良性重放报非零会让恢复流水线在"其实已经成功"时中止。
        _emit({
            "ok": True, "mode": "import", "target_generation": generation,
            "already_imported": True, "detail": str(exc),
        }, args.result_file)
        return 0
    except (LiveTargetError, MaintenanceLockError) as exc:
        _emit({"ok": False, "mode": "import", "error": str(exc)}, args.result_file)
        return 2
    except (ImportError_, RepositoryError, PolicyError, ImportJournalError) as exc:
        _emit({"ok": False, "target_generation": generation, "error": str(exc)},
              args.result_file)
        return 1
    _emit({
        "ok": True,
        "mode": "import",
        "import_id": result.import_id,
        "target_generation": generation,
        "resumed": result.resumed,
        "plan": _plan_payload(result.plan),
        "materialized": result.materialized,
        "projection": result.projection,
        "verification": result.verification,
        "merge": result.merge_report,
        "snapshot_guard": result.snapshot_guard,
        "authorization": {
            **authorization,
            "portable_snapshot": {
                "ready": result.plan.portable_ready,
                "reason": result.plan.readiness_reason,
                "override_requested": bool(
                    args.allow_incomplete_portable_snapshot
                ),
                "environment_confirmed": (
                    os.environ.get("FLORI_ACCEPT_INCOMPLETE_PORTABLE") == "1"
                ),
                "override_accepted": bool(
                    args.into_live
                    and not result.plan.portable_ready
                    and args.allow_incomplete_portable_snapshot
                    and os.environ.get("FLORI_ACCEPT_INCOMPLETE_PORTABLE") == "1"
                ),
            },
            "config_root": str(Path(os.path.abspath(config_root))),
            "source_roots": {
                key: str(path) for key, path in sorted(source_roots.items())
            },
        },
    }, args.result_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
