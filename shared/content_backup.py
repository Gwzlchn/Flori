"""便携内容仓库只读备份编排:DB 副本 -> allowlist 序列化 -> manifest 选择 -> CAS -> snapshot。

对应设计稿 05 号 §2.4/§2.5/§2.6/§2.7/§2.8。本模块只读业务系统:SQLite 用 online
backup 副本上的独立连接,对象存储只经 StorageBackend 读接口;唯一写入面是
ContentRepository(P1 契约)。任一步失败 refs 不动、tmp 留给下次 clean_tmp,
不发布半成品 snapshot。

编排顺序(P1 已固化,不得重排):预校验 ref 名 -> write_lock -> clean_tmp ->
in_progress receipt -> DB online backup 副本 + integrity/migration 验证 ->
分类扫描 -> allowlist 序列化 -> manifest 枚举与 eligible 判定(M1/M2 一致性重读)
-> blob 采集(流式 SHA) -> records -> put_snapshot -> set_ref -> 终态 receipt。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

import structlog

from .content_policy import (
    MAX_AUDIT_TEXT_CHARS,
    MAX_RECORD_CANONICAL_BYTES,
    PolicyError,
    StreamingSecretScanner,
    classify_table,
    redact_url,
    redact_urls_in_json,
    validate_record,
)
from .content_repository import (
    REPOSITORY_FORMAT,
    SNAPSHOT_FORMAT,
    SOURCE_MANIFEST_FORMAT,
    ContentRepository,
    RepositoryError,
    validate_ref_name,
)
from .content_result import (
    ResultDestination,
    ResultFileError,
    emit_result,
    ensure_output_roots_disjoint,
    prepare_result_destination,
)
from .db import SCHEMA_VERSION
from .migrations import migration_steps
from .source_library import (
    SourceIdentityMismatch,
    SourceLibrary,
    SourceReferenceError,
    parse_source_ref,
    source_roots_from_env,
)
from .storage import read_path_bounded
from .step_completion import DETERMINISTIC_SKIP_REASONS
from .step_manifest import (
    ManifestError,
    canonical_digest,
    canonical_json_bytes,
    manifest_relative_path,
    validate_manifest,
)
# 生命周期 dotfile 判定的单一来源(A2):worker 提交面与备份面必须同一套语义,
# 否则 .01_download.done 之类 sidecar 会被误判成未知业务产物,首备必然整体失败。
from .step_output_commit import _is_runtime_sidecar
from .step_scope import execution_step_key

_log = structlog.get_logger(__name__)

# 备份期间并发 rerun 的 M1/M2 重读上限(§2.7-4):超限整次失败,不吞成增量。
DEFAULT_CONSISTENCY_RETRIES = 3
# 单个失败 scope 记录的部分产物摘要上限:防异常目录撑爆 failure_event record。
MAX_PARTIAL_OUTPUT_ENTRIES = 200
# legacy_archive 分片阈值:超限分片而非中止整次备份。
LEGACY_ARCHIVE_CHUNK_ROWS = 2_000
LEGACY_ARCHIVE_CHUNK_BYTES = MAX_RECORD_CANONICAL_BYTES // 2

# 未完成下载/写入的中间文件后缀。只有这些才允许被当作失败 scope 的 partial
# 残留:失败 scope 不是"什么都能吞"的黑洞,其余文件即使落在失败 scope 也必须
# 走 unknown 门,否则真实业务产物会被静默丢弃却报成功。
_INCOMPLETE_SUFFIXES = (
    ".part", ".part-frag", ".ytdl", ".temp", ".tmp", ".download",
    ".crdownload", ".partial", ".aria2",
)

# job.json 里影响 AI 行为的键:只归档这些规范子集,不复制整个运行 sidecar。
_JOB_JSON_AI_KEYS = (
    # 当前 producer/consumer 真键(api/routes/jobs.py,shared/ai_routing.py)。
    "ai_overrides", "prompt_overrides",
    # 兼容早期/外部导入 sidecar;仍只取显式子集,不归档整个 job.json。
    "ai_override", "prompt_override", "ai_config",
)

# /data/prompts 仅这几个相对命名空间属于运行态可变用户配置。未知路径 fail-closed,
# 防止把凭据或临时文件误收进明文仓库。
_USER_CONFIG_LAYOUT: Mapping[str, tuple[frozenset[str], str]] = {
    "": (frozenset({".md"}), "prompts"),
    "profiles": (frozenset({".yaml", ".yml"}), "profiles"),
    "styles": (frozenset({".yaml", ".yml"}), "styles"),
    "templates": (frozenset({".md"}), "templates"),
}

# 各表的 JSON 文本列:序列化为结构化对象,消除文本格式差异造成的伪新 record。
# 名称带 _json 后缀的账本列(如 evidence_json)保持原文本,它们参与既有指纹。
_JOB_JSON_COLUMNS = frozenset({"style_tags", "meta"})
_COLLECTION_JSON_COLUMNS = frozenset({"tags"})
_GLOSSARY_JSON_COLUMNS = frozenset({"aliases", "related", "occurrences"})
# 按 source_type 分派的 URL 载体:订阅源 id 常是 feed URL。
_URL_SOURCE_TYPES = frozenset({"rss", "atom", "feed", "url", "http"})

_STUDY_TABLES = (
    "study_cards", "study_reviews", "study_review_logs",
    "study_suggestion_batches", "study_suggestion_inputs",
    "study_suggestion_evidence", "study_suggestions",
    "study_suggestion_evidence_links", "study_suggestion_operations",
)
_LEGACY_ARCHIVE_TABLE = "glossary_bak_clean_20260617"

# 随 job/chunk 重算的派生列不进快照:导入后由 revalidate 重新产生,
# 备份一份过期的有效性判断只会在恢复后误导人(§2.4D)。
_STUDY_DERIVED_COLUMNS: Mapping[str, frozenset[str]] = {
    "study_suggestion_evidence": frozenset({
        "status", "current_domain", "invalid_reason", "validated_at",
    }),
}

_TERMINAL_RECEIPT_OUTCOMES = frozenset({"success", "failed"})

# 编排面(scheduler/api)直接写进 Job 产物树、任何 step manifest 都认领不到的相对路径。
# 它们不是未知残留,也不进快照:全是可从已备份业务事实重新派生的东西,所有权留在
# 编排面(与 notes_fts5 同一划分)。新增编排写入必须同步登记,否则备份会 fail-closed。
# tests/test_artifact_declaration_contract.py 用 AST 扫描守住这条对账。
ORCHESTRATION_CLAIMED_PATHS = frozenset({
    "job.json",            # 仅 AI 配置规范子集按 user_config 收纳,完整 sidecar 可重建
    "input/term_map.json",  # scheduler/effects.py _export_term_map,提交与 rerun 各重算一次
})

# 文本类产物才可能把密钥当明文带出去;媒体字节没有扫描价值,也不该整块进内存。
_TEXT_BLOB_SUFFIXES = frozenset({
    ".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".csv", ".srt", ".vtt", ".ass",
    ".html", ".htm", ".xml", ".log",
})
def _is_text_blob(storage_rel: str) -> bool:
    return PurePosixPath(storage_rel).suffix.lower() in _TEXT_BLOB_SUFFIXES


class BackupError(RuntimeError):
    """备份编排 fail-closed 错误;不发布 snapshot,不动 refs。"""


@dataclass(frozen=True)
class ManifestExclusion:
    job_id: str
    scope_key: str
    step: str
    reason: str


@dataclass(frozen=True)
class BackupResult:
    snapshot_digest: str
    receipt_id: str | None
    hit_existing_snapshot: bool
    reused_run: bool
    stats: dict
    report: dict
    request_digest: str


class _Inconsistent(Exception):
    """单次 M1/M2 读取序列内部的不一致信号;在重试预算内消化,不外泄。"""


def _default_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _TimestampOutcome:
    value: str | None
    normalized_naive: bool = False
    unparsable: bool = False


def _normalize_utc_timestamp(value: object) -> _TimestampOutcome:
    """DB 时间戳归一为 P1 可接受的 UTC RFC3339。

    naive 串按仓库既有约定(shared/db.py 的 _parse_dt)补 UTC 而不是中止备份:
    旧库里本就存着 naive 串,fail-closed 会让整次备份卡死在历史数据上。
    真正不可解析的返回 unparsable,由调用方按 no_timestamp 同策处置。
    """
    if type(value) is not str or not value:
        return _TimestampOutcome(None)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return _TimestampOutcome(None, unparsable=True)
    normalized = False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
        normalized = True
    moment = moment.astimezone(timezone.utc)
    text = moment.isoformat(
        timespec="microseconds" if moment.microsecond else "seconds"
    )
    return _TimestampOutcome(text, normalized_naive=normalized)


def _parse_json_column(value: object, field_name: str) -> object:
    if type(value) is not str or not value.strip():
        return None
    try:
        return json.loads(value)
    except ValueError as exc:
        raise BackupError(f"{field_name}: stored JSON is invalid") from exc


def _snapshot_database(db_path: Path, copy_path: Path) -> None:
    """SQLite online backup 到隔离副本(§2.7-2);对源库只读,不参与写事务。"""
    if not db_path.is_file():
        raise BackupError(f"database {db_path} not found")
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(copy_path)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _verify_database(connection: sqlite3.Connection) -> int:
    """副本必须是完好且最新 Schema 的库:integrity/FK/user_version/迁移 validator 全过。"""
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise BackupError(f"database copy failed integrity_check: {integrity}")
    fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise BackupError(f"database copy has {len(fk_errors)} foreign key violations")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise BackupError(
            f"database schema v{version} != expected v{SCHEMA_VERSION}; "
            "portable backup only reads the current schema"
        )
    migrations = migration_steps()
    validator = migrations[-1].validate
    if validator is None:
        raise BackupError("latest migration has no validator")
    try:
        validator(connection)
    except Exception as exc:
        raise BackupError(f"schema validator failed: {exc}") from exc
    return version


def _classify_all_tables(connection: sqlite3.Connection) -> dict[str, str]:
    """备份面全表分类扫描:未知表由 classify_table fail-closed(§5.2.23)。"""
    categories: dict[str, str] = {}
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    for (name,) in rows:
        try:
            category, _detail = classify_table(name)
        except PolicyError as exc:
            raise BackupError(f"unclassified table blocks backup: {exc}") from exc
        categories[name] = category
    return categories


def _row_dict(row: sqlite3.Row, *, drop: frozenset[str] = frozenset()) -> dict:
    return {
        key: row[key] for key in row.keys()
        if key not in drop and row[key] is not None
    }


def _portable_source_url(value: str, field: str) -> str | None:
    """本地绝对路径不进快照;内容身份由 source digest 与 manifest blob 承担。"""
    try:
        scheme = urlsplit(value).scheme.casefold()
    except ValueError as exc:
        raise BackupError(f"{field}: source URL is invalid") from exc
    if scheme == "file":
        return None
    return redact_url(value, field).url


def _is_incomplete_artifact(path: str) -> bool:
    """下载器/写入器留下的半成品命名;精确后缀,不做模糊猜测。"""
    name = path.rsplit("/", 1)[-1].casefold()
    return name.endswith(_INCOMPLETE_SUFFIXES)


@dataclass
class _BusinessState:
    """DB 副本读出的备份原料;全部行按自然键有序,保证快照确定性。"""
    jobs: list[sqlite3.Row]
    parts: dict[str, list[sqlite3.Row]]
    steps: dict[str, list[sqlite3.Row]]
    ledgers: dict[str, list[sqlite3.Row]] = field(default_factory=dict)
    legacy_rows: list[dict] = field(default_factory=list)


def _read_business_state(
    connection: sqlite3.Connection, job_ids: Sequence[str] | None,
) -> _BusinessState:
    if job_ids is not None:
        selected = list(dict.fromkeys(job_ids))
        if not selected:
            raise BackupError("job filter must not be empty")
        placeholders = ",".join("?" for _ in selected)
        jobs = connection.execute(
            f"SELECT * FROM jobs WHERE id IN ({placeholders}) ORDER BY id", selected,
        ).fetchall()
        found = {row["id"] for row in jobs}
        missing = sorted(set(selected) - found)
        if missing:
            raise BackupError(f"requested jobs not found: {missing}")
    else:
        jobs = connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    parts: dict[str, list[sqlite3.Row]] = {}
    steps: dict[str, list[sqlite3.Row]] = {}
    for row in jobs:
        job_id = row["id"]
        parts[job_id] = connection.execute(
            "SELECT * FROM job_parts WHERE job_id=? ORDER BY part_index", (job_id,),
        ).fetchall()
        steps[job_id] = connection.execute(
            "SELECT * FROM job_steps WHERE job_id=? ORDER BY scope_key, step", (job_id,),
        ).fetchall()

    def _select(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return connection.execute(sql, params).fetchall()

    ledgers: dict[str, list[sqlite3.Row]] = {
        "collections": _select("SELECT * FROM collections ORDER BY id"),
        "ingested_items": _select(
            "SELECT * FROM ingested_items ORDER BY collection_id, item_id"
        ),
        "prompt_overrides": _select(
            "SELECT * FROM prompt_overrides ORDER BY scope, domain, pipeline, document_kind, step"
        ),
        "prompt_override_versions": _select(
            "SELECT * FROM prompt_override_versions "
            "ORDER BY scope, domain, pipeline, document_kind, step, version"
        ),
        "glossary": _select("SELECT * FROM glossary ORDER BY domain, term"),
        "concept_definition_versions": _select(
            "SELECT * FROM concept_definition_versions ORDER BY domain, term, version"
        ),
        "ai_task_logs": _select("SELECT * FROM ai_task_logs ORDER BY task_id, created_at, id"),
    }
    if job_ids is not None:
        placeholders = ",".join("?" for _ in jobs)
        ledgers["ai_usage"] = _select(
            f"SELECT * FROM ai_usage WHERE job_id IN ({placeholders}) "
            "ORDER BY exec_id, job_id, step, id",
            tuple(row["id"] for row in jobs),
        )
    else:
        ledgers["ai_usage"] = _select(
            "SELECT * FROM ai_usage ORDER BY exec_id, job_id, step, id"
        )
    for table in _STUDY_TABLES:
        ledgers[table] = _select(f"SELECT * FROM {table} ORDER BY rowid")

    legacy_rows: list[dict] = []
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_LEGACY_ARCHIVE_TABLE,),
    ).fetchone()
    if exists:
        legacy_rows = [
            _row_dict(row)
            for row in _select(f"SELECT * FROM {_LEGACY_ARCHIVE_TABLE} ORDER BY rowid")
        ]
    return _BusinessState(
        jobs=jobs, parts=parts, steps=steps, ledgers=ledgers, legacy_rows=legacy_rows,
    )


def _serialize_job_core(row: sqlite3.Row) -> dict:
    body: dict = {
        "id": row["id"],
        "content_type": row["content_type"],
        "pipeline": row["pipeline"],
        "created_at": row["created_at"],
    }
    for key in (
        "document_kind", "title", "domain", "source", "published_at",
        "lineage_key", "parent_job_id", "source_digest", "pipeline_digest",
    ):
        value = row[key]
        if value:
            body[key] = value
    if row["url"]:
        portable_url = _portable_source_url(row["url"], "jobs.url")
        if portable_url is not None:
            body["url"] = portable_url
    body["is_current"] = int(row["is_current"])
    for key in sorted(_JOB_JSON_COLUMNS):
        parsed = _parse_json_column(row[key], f"jobs.{key}")
        if parsed is not None:
            body[key] = parsed
    return body


def _serialize_part_core(row: sqlite3.Row, *, source_blob: str | None = None) -> dict:
    body: dict = {
        "id": row["id"],
        "job_id": row["job_id"],
        "part_index": int(row["part_index"]),
        "created_at": row["created_at"],
    }
    for key in ("title", "source_ref", "source_digest", "updated_at"):
        value = row[key]
        if value:
            body[key] = value
    if row["source_url"]:
        portable_url = _portable_source_url(row["source_url"], "job_parts.source_url")
        if portable_url is not None:
            body["source_url"] = portable_url
    for key in ("size_bytes", "duration_ms"):
        if row[key] is not None:
            body[key] = int(row[key])
    if source_blob is not None:
        body["source_blob"] = source_blob
    parsed = _parse_json_column(row["meta"], "job_parts.meta")
    if parsed is not None:
        body["meta"] = parsed
    return body


def _serialize_ledger_row(table: str, row: sqlite3.Row) -> tuple[str, dict]:
    """账本表行 -> (record kind, body);列级取舍见 §2.4A 与 P1 allowlist。"""
    if table == "collections":
        body = _row_dict(row, drop=frozenset({
            "job_count", "last_synced_at", "last_sync_status", "last_sync_error",
        }))
        for key in _COLLECTION_JSON_COLUMNS:
            parsed = _parse_json_column(body.pop(key, None), f"collections.{key}")
            if parsed is not None:
                body[key] = parsed
        # 订阅源 id 常是 feed URL:按 source_type 分派脱敏。
        source_type = str(body.get("source_type") or "").casefold()
        if source_type in _URL_SOURCE_TYPES and body.get("source_id"):
            body["source_id"] = redact_url(body["source_id"], "collections.source_id").url
        return "collection", body
    if table == "ingested_items":
        return "ingested_item", _row_dict(row)
    if table == "prompt_overrides":
        return "prompt_override", _row_dict(row)
    if table == "prompt_override_versions":
        return "prompt_override_version", _row_dict(row)
    if table == "glossary":
        body = _row_dict(row)
        for key in _GLOSSARY_JSON_COLUMNS:
            parsed = _parse_json_column(body.pop(key, None), f"glossary.{key}")
            if parsed is not None:
                body[key] = parsed
        return "glossary", body
    if table == "concept_definition_versions":
        return "definition_version", _row_dict(row)
    if table == "ai_usage":
        return "ai_usage", _row_dict(row, drop=frozenset({"id"}))
    if table == "ai_task_logs":
        body = _row_dict(row, drop=frozenset({"id"}))
        if not body.get("exec_id"):
            raise BackupError(
                f"ai_task_logs task_id={row['task_id']!r}: exec_id is required "
                "for the composite natural key"
            )
        return "ai_task_log", body
    if table in _STUDY_TABLES:
        return "study", {
            "table": table,
            "row": _row_dict(row, drop=_STUDY_DERIVED_COLUMNS.get(table, frozenset())),
        }
    raise BackupError(f"no serializer for ledger table {table!r}")


def _chunk_legacy_rows(rows: list[dict]) -> list[list[dict]]:
    """按行数/字节上限切分 legacy 归档;单行超限也独立成片,不中止备份。"""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for row in rows:
        row_size = len(canonical_json_bytes(row))
        if current and (
            len(current) >= LEGACY_ARCHIVE_CHUNK_ROWS
            or size + row_size > LEGACY_ARCHIVE_CHUNK_BYTES
        ):
            chunks.append(current)
            current, size = [], 0
        current.append(row)
        size += row_size
    if current:
        chunks.append(current)
    return chunks


@dataclass
class _JobArtifacts:
    job_id: str
    step_results: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    blob_digests: set[str] = field(default_factory=set)
    exclusions: list[ManifestExclusion] = field(default_factory=list)
    unknown_paths: list[str] = field(default_factory=list)
    approved_unknown_paths: list[str] = field(default_factory=list)
    missing_manifests: list[str] = field(default_factory=list)
    terminal_steps: int = 0
    ai_config: dict | None = None
    ai_config_complete: bool = True


class _BackupRun:
    """单次备份的编排状态机;实例只使用一次。"""

    def __init__(
        self,
        *,
        db_path: Path,
        storage,
        repository: ContentRepository,
        run_id: str,
        app_version: str,
        ref: str,
        job_ids: Sequence[str] | None,
        allow_unknown: bool,
        allowed_unknown: frozenset[str],
        allowed_secret_blobs: frozenset[str] = frozenset(),
        user_config_dir: Path | None,
        vendor_media: bool,
        full_rehash: bool,
        consistency_retries: int,
        work_dir: Path,
        now_fn: Callable[[], str] = _default_now,
    ) -> None:
        self.db_path = Path(db_path)
        self.storage = storage
        self.repository = repository
        self.run_id = run_id
        self.app_version = app_version
        self.ref = ref
        self.job_ids = list(job_ids) if job_ids is not None else None
        self.allow_unknown = allow_unknown
        self.allowed_unknown = allowed_unknown
        self.allowed_secret_blobs = allowed_secret_blobs
        self.user_config_dir = Path(user_config_dir) if user_config_dir is not None else None
        self.vendor_media = vendor_media
        self.full_rehash = full_rehash
        self.consistency_retries = consistency_retries
        self.work_dir = work_dir
        self.now_fn = now_fn
        self.stats: dict = {
            "blobs_created": 0, "blobs_reused": 0, "blob_bytes_total": 0,
            "blob_bytes_rehashed": 0, "step_results_incremental": 0,
        }
        self.report: dict = {
            "jobs": {},
            "normalized_naive_timestamps": 0,
            "url_redactions": {},
        }
        self.ai_usage_index: dict[tuple[str, str], list[str]] = {}
        self.record_cache: dict[tuple[str, str], dict] = {}
        self.published_blobs: set[str] = set()
        # 都用集合:一致性重读会把同一 blob 扫多遍,清单必须幂等(且要进 digest)。
        self.secret_exceptions: set[str] = set()
        self.manifests_seen = 0
        self.manifests_missing = 0
        self.records_total = 0


    def _prepare_record(self, kind: str, body: Mapping) -> dict:
        """入库前统一 URL 脱敏:meta/record_json 等自由结构里的内嵌 URL 一并处理。

        专用字段(job_core.url 等)已在序列化时精确脱敏;redact_url 幂等,
        再过一遍不变形。这里覆盖的是无法逐字段枚举的自由结构。
        """
        redacted, reasons = redact_urls_in_json(dict(body), kind)
        if reasons:
            bucket = self.report["url_redactions"].setdefault(kind, {})
            for reason in reasons:
                bucket[reason] = bucket.get(reason, 0) + 1
        return redacted

    def _put_record(self, kind: str, body: Mapping) -> str:
        prepared = self._prepare_record(kind, body)
        try:
            put = self.repository.put_record(kind, prepared)
        except (PolicyError, RepositoryError) as exc:
            raise BackupError(f"record {kind}: {exc}") from exc
        self.records_total += 1
        self.record_cache[(kind, put.digest)] = prepared
        return put.digest

    def _record_digest_for(self, kind: str, body: Mapping) -> str:
        """不落盘算出 record digest(增量判定用);走同一条 validate_record 门。"""
        prepared = self._prepare_record(kind, body)
        try:
            encoded = validate_record(kind, prepared)
        except PolicyError as exc:
            raise BackupError(f"record {kind}: {exc}") from exc
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


    def _load_state(self) -> tuple[_BusinessState, int, dict[str, str]]:
        copy_path = self.work_dir / "db-snapshot.sqlite3"
        _snapshot_database(self.db_path, copy_path)
        connection = sqlite3.connect(copy_path)
        connection.row_factory = sqlite3.Row
        try:
            db_user_version = _verify_database(connection)
            categories = _classify_all_tables(connection)
            state = _read_business_state(connection, self.job_ids)
        finally:
            connection.close()
        return state, db_user_version, categories

    def _build_ledger_records(self, state: _BusinessState) -> list[str]:
        digests: list[str] = []
        for table, rows in state.ledgers.items():
            for row in rows:
                kind, body = _serialize_ledger_row(table, row)
                digest = self._put_record(kind, body)
                digests.append(digest)
                if kind == "ai_usage":
                    key = (body.get("job_id"), body.get("step"))
                    if key[0] and key[1]:
                        self.ai_usage_index.setdefault(key, []).append(digest)
        if state.legacy_rows:
            chunks = _chunk_legacy_rows(state.legacy_rows)
            for index, chunk in enumerate(chunks):
                body: dict = {"table": _LEGACY_ARCHIVE_TABLE, "rows": chunk}
                if len(chunks) > 1:
                    body["chunk_index"] = index
                    body["chunk_total"] = len(chunks)
                digests.append(self._put_record("legacy_archive", body))
        return digests

    async def _collect_collection_configs(
        self, state: _BusinessState,
    ) -> tuple[list[str], set[str]]:
        """集合级用户配置(§2.4A):collections/{id}/terms.json 按 user_config 收进快照。

        这张表是人工维护的书籍术语表,不是派生物。term_map.json 由 glossary "加"
        它算出来;glossary 是 DB 行早就进了快照,它却只躺在对象存储里。漏掉它,
        恢复后 _export_term_map 读到空值会安静退回"只用 glossary"(read_file 返回
        假值,不抛不警),那本书的译名从此少一截且没人看得出来。
        """
        digests: list[str] = []
        blob_refs: set[str] = set()
        for row in state.ledgers.get("collections", ()):
            collection_id = row["id"]
            rel = f"collections/{collection_id}/terms.json"
            data = await self.storage.read_file(f"collections/{collection_id}", "terms.json")
            if not data:
                continue
            # 与产物 blob 同一条密钥防线:用户手写的文件同样可能粘进凭证。
            self._scan_blob_bytes(collection_id, rel, data)
            put = self.repository.put_blob_bytes(data)
            if put.created:
                self.stats["blobs_created"] += 1
                self.published_blobs.add(put.digest)
            else:
                self.stats["blobs_reused"] += 1
            self.stats["blob_bytes_total"] += put.size_bytes
            self.stats["blob_bytes_rehashed"] += put.size_bytes
            blob_refs.add(put.digest)
            digests.append(self._put_record("user_config", {
                "path": rel,
                "kind": "domain_config",
                "blob": put.digest,
                "size_bytes": put.size_bytes,
                "media_type": "application/json",
            }))
        return digests, blob_refs

    def _put_user_config_blob(
        self, *, scan_owner: str, path: str, kind: str, data: bytes, media_type: str,
    ) -> tuple[str, str]:
        """完整扫描并收纳一个小型用户配置;返回 (record digest, blob digest)。"""
        self._scan_blob_bytes(scan_owner, path, data)
        put = self.repository.put_blob_bytes(data)
        if put.created:
            self.stats["blobs_created"] += 1
            self.published_blobs.add(put.digest)
        else:
            self.stats["blobs_reused"] += 1
        self.stats["blob_bytes_total"] += put.size_bytes
        self.stats["blob_bytes_rehashed"] += put.size_bytes
        digest = self._put_record("user_config", {
            "path": path,
            "kind": kind,
            "blob": put.digest,
            "size_bytes": put.size_bytes,
            "media_type": media_type,
        })
        return digest, put.digest

    def _collect_user_configs(self) -> tuple[list[str], set[str], bool]:
        """收纳 /data/prompts 的允许子集;未提供根时显式标为不完整。"""
        if self.user_config_dir is None:
            return [], set(), False
        root = self.user_config_dir
        if not root.exists():
            return [], set(), True
        if root.is_symlink() or not root.is_dir():
            raise BackupError(f"user config root must be a non-symlink directory: {root}")
        files: list[tuple[Path, str, str]] = []
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                raise BackupError(f"user config path must not be a symlink: {entry}")
            if entry.is_file():
                suffixes, kind = _USER_CONFIG_LAYOUT[""]
                if entry.suffix.casefold() not in suffixes:
                    raise BackupError(f"unknown user config path: {entry.name}")
                files.append((entry, entry.name, kind))
                continue
            if not entry.is_dir() or entry.name not in _USER_CONFIG_LAYOUT:
                raise BackupError(f"unknown user config path: {entry.name}")
            suffixes, kind = _USER_CONFIG_LAYOUT[entry.name]
            for child in sorted(entry.iterdir(), key=lambda item: item.name):
                relative = f"{entry.name}/{child.name}"
                if child.is_symlink() or not child.is_file():
                    raise BackupError(f"user config path must be a regular file: {relative}")
                if child.suffix.casefold() not in suffixes:
                    raise BackupError(f"unknown user config path: {relative}")
                files.append((child, relative, kind))
        digests: list[str] = []
        blobs: set[str] = set()
        for file_path, relative, kind in files:
            try:
                data = read_path_bounded(
                    file_path,
                    MAX_RECORD_CANONICAL_BYTES,
                    trusted_root=root,
                )
            except OSError as exc:
                raise BackupError(f"cannot safely read user config {relative}: {exc}") from exc
            if len(data) > MAX_RECORD_CANONICAL_BYTES:
                raise BackupError(f"user config exceeds size limit: {relative}")
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise BackupError(f"user config is not valid UTF-8: {relative}") from exc
            record, blob = self._put_user_config_blob(
                scan_owner="user-config",
                path=f"prompts/{relative}",
                kind=kind,
                data=data,
                media_type=(
                    "text/markdown" if file_path.suffix.casefold() == ".md"
                    else "application/yaml"
                ),
            )
            digests.append(record)
            blobs.add(blob)
        return digests, blobs, True

    def _collect_job_ai_config(
        self, job_id: str, ai_config: Mapping,
    ) -> tuple[str, str]:
        """仅归档 job.json 中影响 AI 行为的规范子集。"""
        prepared, reasons = redact_urls_in_json(dict(ai_config), f"{job_id}.ai_config")
        if reasons:
            bucket = self.report["url_redactions"].setdefault("job_ai_config", {})
            for reason in reasons:
                bucket[reason] = bucket.get(reason, 0) + 1
        data = canonical_json_bytes(prepared)
        return self._put_user_config_blob(
            scan_owner=job_id,
            path=f"jobs/{job_id}/ai-config.json",
            kind="job_ai_config",
            data=data,
            media_type="application/json",
        )


    async def _read_manifest(self, job_id: str, rel: str) -> tuple[bytes | None, dict | None]:
        """读一次原始字节并就地校验:raw 供 M1/M2 逐字节比对,dict 供选择判定。

        不走 read_valid_manifest 是为了把同一 scope 的多次读收敛成一次,
        并拿到用于比对的原始字节;校验仍复用 validate_manifest 这一唯一契约原语。
        """
        raw = await self.storage.read_file(job_id, rel)
        if raw is None:
            return None, None
        try:
            data = json.loads(raw)
            validate_manifest(data)
        except (TypeError, ValueError, ManifestError):
            return raw, None
        return raw, data

    async def _hash_storage_object(
        self, job_id: str, storage_rel: str, *, spool: Path | None,
    ) -> tuple[str, int]:
        """流式 SHA;文本对象同步完整扫描,不为密钥门再读一遍对象。"""
        stream = await self.storage.open_stream(job_id, storage_rel)
        if stream is None:
            raise _Inconsistent(f"object missing: {job_id}/{storage_rel}")
        hasher = hashlib.sha256()
        total = 0
        try:
            writer = open(spool, "xb") if spool is not None else None
        except FileExistsError as exc:
            raise BackupError(
                f"repository spool path was occupied before exclusive create: {spool.name}"
            ) from exc
        scan_key = f"{job_id}:{storage_rel}"
        scan_allowed = scan_key in self.allowed_secret_blobs
        scanner = (
            StreamingSecretScanner(
                f"blob {storage_rel}", raise_on_match=not scan_allowed,
            )
            if _is_text_blob(storage_rel) else None
        )
        try:
            async for chunk in stream:
                hasher.update(chunk)
                total += len(chunk)
                if writer is not None:
                    writer.write(chunk)
                if scanner is not None:
                    try:
                        scanner.feed(chunk)
                    except PolicyError as exc:
                        raise BackupError(
                            f"blob {scan_key} contains a secret-shaped value ({exc}); "
                            "fix the producer or approve this exact path"
                        ) from exc
            if scanner is not None:
                try:
                    scanner.finish()
                except PolicyError as exc:
                    raise BackupError(
                        f"blob {scan_key} contains a secret-shaped value ({exc}); "
                        "fix the producer or approve this exact path"
                    ) from exc
                if scanner.matched:
                    self.secret_exceptions.add(scan_key)
        finally:
            if writer is not None:
                writer.close()
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()
        return "sha256:" + hasher.hexdigest(), total

    def _scan_blob_bytes(
        self, job_id: str, storage_rel: str, data: bytes,
    ) -> None:
        """文本产物的明文密钥门。

        record/receipt 早就逐字段扫过,blob 字节却一路裸奔:01_download 把重定向
        之后的 final_url/resolved_url 写进 input/metadata.json,CDN 签名就在
        那里,而 jobs.url 那侧已经脱敏——于是 snapshot.policy.secrets_included
        是在没人看过的字节上断言的。命中即整次失败,交人工修产出侧或显式放行。
        """
        key = f"{job_id}:{storage_rel}"
        scanner = StreamingSecretScanner(
            f"blob {storage_rel}",
            raise_on_match=key not in self.allowed_secret_blobs,
        )
        try:
            scanner.feed(data)
            scanner.finish()
        except PolicyError as exc:
            raise BackupError(
                f"blob {key} contains a secret-shaped value ({exc}); portable snapshots "
                "assert secrets_included=false, so this fails closed. Fix the producing "
                "step (redact before writing) or approve the path via "
                "--allow-secret-blob-file after review"
            ) from exc
        if scanner.matched:
            self.secret_exceptions.add(key)

    async def _collect_output_blob(
        self, job_id: str, storage_rel: str, entry: dict, tally: dict[str, int],
    ) -> None:
        """逐字节流式 SHA 与 manifest 核对(§2.5-1-3);绝不信任 ETag/source_digest。

        目标 blob 已在仓库时只读源验证不落盘;不在时直接 spool 到仓库 tmp/ 再
        adopt(同文件系统 link,省第二遍拷贝)。大小或摘要不符走 _Inconsistent
        进入重读预算;计数只写 tally,scope 成功后一次并入 stats,防重试重复计数。
        """
        declared = entry["sha256"]
        # 复用已有 blob 时同样扫:仓库里躺着的旧泄漏也必须被喊出来,而不是
        # 因为"上次已经收过"就永远沉默。
        if self.repository.has_blob(declared):
            observed, size = await self._hash_storage_object(
                job_id, storage_rel, spool=None,
            )
            if observed != declared or size != entry["size_bytes"]:
                raise _Inconsistent(f"output drift: {job_id}/{storage_rel}")
            # 本次运行刚发布的才算 created 的重复命中,其余是跨运行复用。
            tally["blobs_reused"] += 1
            tally["blob_bytes_total"] += size
            tally["blob_bytes_rehashed"] += size
            return
        spool = self.repository.tmp_dir / f"spool-{secrets.token_hex(16)}"
        try:
            observed, size = await self._hash_storage_object(
                job_id, storage_rel, spool=spool,
            )
            if observed != declared or size != entry["size_bytes"]:
                raise _Inconsistent(f"output drift: {job_id}/{storage_rel}")
            put = self.repository.adopt_blob_file(spool)
            if put.digest != declared:
                raise BackupError(f"spool digest drift for {job_id}/{storage_rel}")
            if put.created:
                tally["blobs_created"] += 1
                self.published_blobs.add(put.digest)
            else:
                tally["blobs_reused"] += 1
            tally["blob_bytes_total"] += size
            tally["blob_bytes_rehashed"] += size
        finally:
            spool.unlink(missing_ok=True)

    async def _incremental_hit(self, body: Mapping, manifest: dict) -> str | None:
        """内容寻址增量:同 step_result record 已在仓库且全部 blob 在位则跳过重读。

        正确性由内容寻址保证:record digest 覆盖整份 manifest(含每个 output 的
        size/sha),blob key 就是字节 SHA,三者齐备等价于重读一遍。位腐蚀由
        --verify/scrub 与 --full-rehash 负责,不摊进日常增量(12GB 级媒体全量重读
        不可行)。文本输出例外:每次都完整重读并扫描,否则新 v2 快照不能诚实断言
        secret_scan_complete=true。
        """
        if self.full_rehash:
            return None
        digest = self._record_digest_for("step_result", body)
        if not self.repository.has_record("step_result", digest):
            return None
        for entry in manifest["outputs"]:
            if not self.repository.has_blob(entry["sha256"]):
                return None
        part_id = manifest["scope"]["part_id"]
        prefix = f"parts/{part_id}/" if part_id else ""
        scanned_bytes = 0
        for entry in manifest["outputs"]:
            storage_rel = prefix + entry["path"]
            if not _is_text_blob(storage_rel):
                continue
            try:
                observed, size = await self._hash_storage_object(
                    body["job_id"], storage_rel, spool=None,
                )
            except _Inconsistent:
                return None
            if observed != entry["sha256"] or size != entry["size_bytes"]:
                return None
            scanned_bytes += size
        self.stats["blob_bytes_total"] += scanned_bytes
        self.stats["blob_bytes_rehashed"] += scanned_bytes
        return digest

    async def _collect_step(
        self,
        job_id: str,
        scope_key: str,
        step: str,
        part_index_map: Mapping[str, int],
    ) -> tuple[str, object]:
        """单个 (scope, step) 的 M1/M2 一致性采集(§2.7-4)。

        返回 ("eligible", (digest, manifest)) / ("excluded", reason) / ("missing", None)。
        身份不一致是伪造/串写而非竞态,直接 BackupError 不消耗重试。
        """
        part_id = scope_key.split(":", 1)[1] if scope_key != "job" else None
        prefix = f"parts/{part_id}/" if part_id else ""
        rel = manifest_relative_path(scope_key, step)
        seen_counted = False
        last_reason = "consistency retries exhausted"
        for _attempt in range(self.consistency_retries):
            raw, manifest = await self._read_manifest(job_id, rel)
            if raw is None:
                if not seen_counted:
                    # 首轮就没有 manifest:该步骤从未提交,合法缺失。
                    return ("missing", None)
                # 采集途中 manifest 消失 = 并发 rerun/delete,不能吞成增量(§2.7-5)。
                last_reason = f"manifest vanished during read: {job_id}/{scope_key}/{step}"
                continue
            if not seen_counted:
                self.manifests_seen += 1
                seen_counted = True
            if manifest is None:
                return ("excluded", "manifest_invalid")
            if manifest["job_id"] != job_id or manifest["scope"]["scope_key"] != scope_key \
                    or manifest["step"] != step:
                raise BackupError(
                    f"manifest identity mismatch at {job_id}/{scope_key}/{step}"
                )
            if part_id is not None and manifest["scope"]["part_index"] != part_index_map.get(part_id):
                raise BackupError(
                    f"manifest part_index disagrees with DB at {job_id}/{scope_key}"
                )
            if manifest["outcome"] == "skipped":
                reason = manifest["skip"]["reason_code"]
                if reason not in DETERMINISTIC_SKIP_REASONS:
                    return ("excluded", f"non_deterministic_skip:{reason}")
            record_body = {
                "job_id": job_id,
                "scope_key": scope_key,
                "step": step,
                "manifest": manifest,
                "output_blobs": {
                    entry["path"]: entry["sha256"] for entry in manifest["outputs"]
                },
            }
            hit = await self._incremental_hit(record_body, manifest)
            if hit is not None:
                # 快路径不读 outputs,没有"读字节"的时间窗,因此无需 M2 复读。
                self.stats["step_results_incremental"] += 1
                return ("eligible", (hit, manifest))
            tally = {
                "blobs_created": 0, "blobs_reused": 0,
                "blob_bytes_total": 0, "blob_bytes_rehashed": 0,
            }
            try:
                for entry in manifest["outputs"]:
                    await self._collect_output_blob(
                        job_id, prefix + entry["path"], entry, tally,
                    )
                confirm_raw, _confirm = await self._read_manifest(job_id, rel)
                if confirm_raw != raw:
                    raise _Inconsistent(
                        f"manifest replaced during read: {job_id}/{scope_key}/{step}"
                    )
            except _Inconsistent as exc:
                last_reason = str(exc)
                continue
            for key, value in tally.items():
                self.stats[key] += value
            return ("eligible", (self._put_record("step_result", record_body), manifest))
        raise BackupError(
            f"consistency retries exhausted at {job_id}/{scope_key}/{step}: {last_reason}"
        )

    async def _check_job_json(
        self, job_id: str, part_ids: list[str],
    ) -> tuple[dict | None, bool]:
        """核对根 job.json 的 Part 清单;返回 (AI 配置规范子集,是否完整)。

        DB 有 Part 而 job.json 缺 parts 键(或非数组)是清单损坏,不再放行。
        """
        raw = await self.storage.read_file(job_id, "job.json")
        if raw is None:
            if part_ids:
                raise BackupError(f"{job_id}: job.json missing while database has parts")
            return None, False
        try:
            root = json.loads(raw)
        except ValueError as exc:
            raise BackupError(f"{job_id}/job.json is not valid JSON") from exc
        if type(root) is not dict:
            raise BackupError(f"{job_id}/job.json must be an object")
        ai_config = {
            key: root[key] for key in _JOB_JSON_AI_KEYS
            if root.get(key) is not None
        }
        declared_raw = root.get("parts")
        if type(declared_raw) is not list:
            if part_ids:
                raise BackupError(
                    f"{job_id}/job.json has no parts array while database has "
                    f"{len(part_ids)} parts"
                )
            return ai_config or None, True
        declared = [entry.get("part_id") for entry in declared_raw if type(entry) is dict]
        if declared != part_ids:
            raise BackupError(f"{job_id}/job.json parts manifest disagrees with database")
        return ai_config or None, True

    def _classify_residue(
        self,
        artifacts: _JobArtifacts,
        job_id: str,
        sizes: Mapping[str, int],
        claimed: set[str],
        part_index_map: Mapping[str, int],
        failed_scopes: set[str],
    ) -> dict[str, list[dict]]:
        """未被 eligible manifest 认领的文件分流:sidecar 丢弃 / 半成品记审计 / 其余走 unknown。

        核心不变量:失败 scope 不是黑洞。只有下载器半成品命名的文件才进
        partial 摘要;任何看起来正常的业务文件仍走 unknown 门,逼人工确认
        后再放行,避免真产物被静默丢弃却报成功。
        注:.flori/staging 命名空间对 list_file_sizes 不可见(storage 层已隔离),
        因此不会出现在这里。
        """
        partials: dict[str, list[dict]] = {}
        for path in sorted(sizes):
            if path in claimed or _is_runtime_sidecar(path):
                continue
            scope = "job"
            scope_rel = path
            if path.startswith("parts/"):
                segments = path.split("/", 2)
                if len(segments) == 3 and segments[1] in part_index_map:
                    scope = f"part:{segments[1]}"
                    scope_rel = segments[2]
                else:
                    artifacts.unknown_paths.append(path)
                    continue
            if scope in failed_scopes and _is_incomplete_artifact(scope_rel):
                partials.setdefault(scope, []).append(
                    {"path": scope_rel, "size_bytes": int(sizes[path])}
                )
            elif f"{job_id}:{path}" in self.allowed_unknown:
                artifacts.approved_unknown_paths.append(path)
            else:
                artifacts.unknown_paths.append(path)
        for entries in partials.values():
            entries.sort(key=lambda item: item["path"])
            del entries[MAX_PARTIAL_OUTPUT_ENTRIES:]
        return partials

    async def _collect_job(self, state: _BusinessState, job: sqlite3.Row) -> _JobArtifacts:
        job_id = job["id"]
        artifacts = _JobArtifacts(job_id=job_id)
        part_rows = state.parts[job_id]
        part_ids = [row["id"] for row in part_rows]
        # Part 清单必须连续有序(§2.5-1):缺口/重复直接拒绝。
        for position, row in enumerate(part_rows, start=1):
            if int(row["part_index"]) != position:
                raise BackupError(
                    f"job {job_id}: part_index sequence broken at position {position}"
                )
        part_index_map = {row["id"]: int(row["part_index"]) for row in part_rows}
        artifacts.ai_config, artifacts.ai_config_complete = await self._check_job_json(
            job_id, part_ids,
        )

        step_rows = state.steps[job_id]
        eligible_manifests: dict[str, dict] = {}
        for row in step_rows:
            scope_key, step = row["scope_key"], row["step"]
            if row["status"] in ("done", "skipped", "failed"):
                artifacts.terminal_steps += 1
            status, payload = await self._collect_step(job_id, scope_key, step, part_index_map)
            key = execution_step_key(scope_key, step)
            if status == "eligible":
                digest, manifest = payload
                artifacts.step_results[key] = digest
                eligible_manifests[key] = manifest
                for entry in manifest["outputs"]:
                    artifacts.blob_digests.add(entry["sha256"])
            elif status == "excluded":
                artifacts.exclusions.append(
                    ManifestExclusion(job_id, scope_key, step, payload)
                )
            elif row["status"] in ("done", "skipped"):
                # DB 说完成但没有 manifest:覆盖率缺口,计入报告供 04 backfill 对账。
                self.manifests_missing += 1
                artifacts.missing_manifests.append(key)

        # 编排面写、任何 step manifest 都认领不到的路径,只能在这里按名字点出来。
        # term_map.json 由 scheduler 从 glossary + 集合 terms.json 派生(scheduler/
        # effects.py _export_term_map),提交与 rerun 时各重算一次;所有权归 scheduler,
        # 与 notes_fts5 同一划分,因此不进快照,恢复后由 scheduler 重新导出。
        # 这里只声明"它不是未知残留",不给它建 blob。
        claimed: set[str] = set(ORCHESTRATION_CLAIMED_PATHS)
        for pid in part_ids:
            claimed.update(f"parts/{pid}/{rel}" for rel in ORCHESTRATION_CLAIMED_PATHS)
        for manifest in eligible_manifests.values():
            pid = manifest["scope"]["part_id"]
            prefix = f"parts/{pid}/" if pid else ""
            claimed.update(prefix + entry["path"] for entry in manifest["outputs"])

        failed_rows = [row for row in step_rows if row["status"] == "failed"]
        failed_scopes = {row["scope_key"] for row in failed_rows}
        sizes = await self.storage.list_file_sizes(job_id)
        partials = self._classify_residue(
            artifacts, job_id, sizes, claimed, part_index_map, failed_scopes,
        )

        # 同一 scope 的残留只挂最后一次失败:否则多步失败会重复承载同一份清单。
        latest_failure: dict[str, sqlite3.Row] = {}
        for row in failed_rows:
            current = latest_failure.get(row["scope_key"])
            if current is None or (row["finished_at"] or "") >= (current["finished_at"] or ""):
                latest_failure[row["scope_key"]] = row
        for row in failed_rows:
            owns_partials = latest_failure.get(row["scope_key"]) is row
            digest = self._build_failure_event(
                job_id, row, partials if owns_partials else {},
            )
            if digest is not None:
                artifacts.failures.append(digest)
        return artifacts

    def _build_failure_event(
        self, job_id: str, row: sqlite3.Row, partials: Mapping[str, list[dict]],
    ) -> str | None:
        """job_steps 失败行合成不可变 failure_event(§2.4B 现存最佳证据)。

        真 append-only step_failure_events 表属 P2b。exec_id 由 body 全部非派生
        字段的 canonical 摘要合成,与 record digest 一一对应:任一字段变化都得到
        新事件,同一事实重复备份得到同一 record。时间戳全缺或不可解析的行无法
        构成合法事件,记入报告后放弃,不让审计缺口拖垮整次备份。
        """
        scope_key, step = row["scope_key"], row["step"]
        finished = _normalize_utc_timestamp(row["finished_at"])
        started = _normalize_utc_timestamp(row["started_at"])
        for outcome in (finished, started):
            if outcome.normalized_naive:
                self.report["normalized_naive_timestamps"] += 1
        failed_at = finished.value or started.value
        if failed_at is None:
            reason = "unparsable_timestamp" if (
                finished.unparsable or started.unparsable
            ) else "no_timestamp"
            self.report["jobs"].setdefault(job_id, {}).setdefault(
                "failure_rows_skipped", [],
            ).append({"scope_key": scope_key, "step": step, "reason": reason})
            return None
        body: dict = {
            "job_id": job_id,
            "scope_key": scope_key,
            "step": step,
            "failed_at": failed_at,
            "partial_outputs_discarded": True,
        }
        if started.value:
            body["started_at"] = started.value
        retries = row["retries"]
        if retries is not None:
            body["attempt"] = int(retries) + 1
        duration = row["duration_sec"]
        if duration is not None and float(duration) >= 0:
            body["duration_sec"] = float(duration)
        if row["pool"]:
            body["worker_class"] = row["pool"]
        if row["error"]:
            body["sanitized_message"] = str(row["error"])[:MAX_AUDIT_TEXT_CHARS]
        refs = sorted(
            set(self.ai_usage_index.get((job_id, step), ()))
            | set(self.ai_usage_index.get((job_id, execution_step_key(scope_key, step)), ()))
        )
        if refs:
            body["ai_usage_refs"] = refs
        scoped_partials = partials.get(scope_key)
        if scoped_partials:
            body["partial_outputs"] = scoped_partials
        # exec_id 覆盖上面全部字段(不含它自己),保证事件身份 = 事件内容。
        body["exec_id"] = "dbfail_" + hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest()[:24]
        return self._put_record("failure_event", body)


    def _ensure_monthly_anchor(self, snapshot_digest: str) -> str | None:
        """按月建保留锚点(§2.14-2 保留集合的一部分)。

        没有锚点时,超出 receipt 窗口的历史 snapshot 及其独有 blob 会全部进入
        可删清单,库里实际只剩最新一个恢复点。锚点只在当月首次备份时创建,
        已存在不覆盖:它要钉住的是"这个月最早的那个可恢复状态"。
        """
        name = "monthly-" + self.now_fn()[:7]
        try:
            validate_ref_name(name)
            if name in self.repository.list_refs():
                return None
            self.repository.set_ref(name, snapshot_digest)
        except RepositoryError as exc:
            _log.warning("monthly_anchor_failed", ref=name, error=str(exc))
            return None
        return name

    def _external_source_stats(self, state: _BusinessState) -> tuple[int, list[str]]:
        count = 0
        roots: set[str] = set()
        for rows in state.parts.values():
            for row in rows:
                ref = row["source_ref"]
                if not ref:
                    continue
                try:
                    parsed = parse_source_ref(ref)
                except SourceReferenceError as exc:
                    raise BackupError(
                        f"part {row['id']}: invalid source_ref {ref!r}: {exc}"
                    ) from exc
                count += 1
                roots.add(parsed.root_id)
        return count, sorted(roots)

    def _vendor_source_blob(self, row: sqlite3.Row, library: SourceLibrary) -> str:
        """把一个受控 NAS 源完整校验后收进 CAS,且拒绝读中替换。"""
        source_ref = row["source_ref"]
        expected_digest = row["source_digest"]
        expected_size = row["size_bytes"]
        if not expected_digest or expected_size is None:
            raise BackupError(
                f"part {row['id']}: vendor-media requires source_digest and size_bytes"
            )
        try:
            snapshot = library.verify(source_ref, expected_digest, int(expected_size))
            root = library.roots[snapshot.reference.root_id]
            fd = library._open_relative(root, snapshot.reference.relative_path)
        except (SourceReferenceError, SourceIdentityMismatch, OSError) as exc:
            raise BackupError(f"part {row['id']}: cannot verify source media: {exc}") from exc
        spool = self.repository.tmp_dir / f"spool-{secrets.token_hex(16)}"
        hasher = hashlib.sha256()
        total = 0
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise BackupError(f"part {row['id']}: source media is not a safe regular file")
            with open(spool, "xb") as writer:
                while chunk := os.read(fd, 4 * 1024 * 1024):
                    hasher.update(chunk)
                    total += len(chunk)
                    writer.write(chunk)
                after = os.fstat(fd)
                writer.flush()
                os.fsync(writer.fileno())
            fingerprint = lambda value: (
                value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns,
            )
            if fingerprint(before) != fingerprint(after):
                raise BackupError(f"part {row['id']}: source media changed during ingest")
            observed = f"sha256:{hasher.hexdigest()}"
            if observed != snapshot.digest or total != snapshot.size_bytes:
                raise BackupError(f"part {row['id']}: source media identity changed")
            # verify 的缓存路径仍会重开源文件并核对指纹,封住路径在 close 后被替换。
            library.verify(source_ref, snapshot.digest, snapshot.size_bytes)
            put = self.repository.adopt_blob_file(spool)
            if put.digest != snapshot.digest:
                raise BackupError(f"part {row['id']}: vendored blob digest drift")
            if put.created:
                self.stats["blobs_created"] += 1
                self.published_blobs.add(put.digest)
            else:
                self.stats["blobs_reused"] += 1
            self.stats["blob_bytes_total"] += put.size_bytes
            self.stats["blob_bytes_rehashed"] += put.size_bytes
            return put.digest
        finally:
            os.close(fd)
            spool.unlink(missing_ok=True)

    def _vendor_source_media(self, state: _BusinessState) -> dict[str, str]:
        if not self.vendor_media:
            return {}
        try:
            library = SourceLibrary.from_env()
        except SourceReferenceError as exc:
            raise BackupError(f"vendor-media source roots are invalid: {exc}") from exc
        vendored: dict[str, str] = {}
        for job_id in sorted(state.parts):
            for row in state.parts[job_id]:
                if row["source_ref"]:
                    vendored[row["id"]] = self._vendor_source_blob(row, library)
        return vendored

    async def execute(self) -> tuple[dict, dict, dict]:
        """跑完采集并发布 snapshot 与 ref;返回 (snapshot 信息, stats, report)。"""
        state, db_user_version, categories = self._load_state()
        self.report["table_categories"] = categories
        ledger_digests = self._build_ledger_records(state)

        job_group: list[str] = []
        part_group: list[str] = []
        step_group: list[str] = []
        failure_group: list[str] = []
        relation_digests: list[str] = []
        blob_refs: set[str] = set()
        config_digests, config_blobs = await self._collect_collection_configs(state)
        ledger_digests.extend(config_digests)
        blob_refs.update(config_blobs)
        global_config_digests, global_config_blobs, user_config_complete = (
            self._collect_user_configs()
        )
        ledger_digests.extend(global_config_digests)
        blob_refs.update(global_config_blobs)
        vendored_source_blobs = self._vendor_source_media(state)
        blob_refs.update(vendored_source_blobs.values())
        exclusions: list[ManifestExclusion] = []
        unapproved_unknown_total = 0
        unknown_omitted_total = 0
        terminal_total = 0
        ai_config_jobs: list[str] = []
        ai_config_complete = True

        for job in state.jobs:
            job_id = job["id"]
            core_digest = self._put_record("job_core", _serialize_job_core(job))
            job_group.append(core_digest)
            relation: dict = {"job_id": job_id, "core": core_digest}
            if job["collection_id"]:
                # revision 是"这条用户状态基于目标的哪个前值"的凭据(§2.9-6):
                # import 侧只有在目标现值仍等于它时才允许 --apply-user-state 覆盖,
                # 否则说明目标在备份之后又被人改过,盲目覆盖会吞掉那次修改。
                user_state = self._put_record("job_user_state", {
                    "job_id": job_id,
                    "collection_id": job["collection_id"],
                    "revision": canonical_digest({
                        "job_id": job_id, "collection_id": job["collection_id"],
                    }),
                })
                job_group.append(user_state)
                relation["user_state"] = user_state
            part_digests = [
                self._put_record(
                    "part_core",
                    _serialize_part_core(
                        row, source_blob=vendored_source_blobs.get(row["id"]),
                    ),
                )
                for row in state.parts[job_id]
            ]
            part_group.extend(part_digests)
            relation["parts"] = part_digests

            artifacts = await self._collect_job(state, job)
            step_group.extend(artifacts.step_results.values())
            failure_group.extend(artifacts.failures)
            blob_refs.update(artifacts.blob_digests)
            relation["step_results"] = dict(sorted(artifacts.step_results.items()))
            relation["failures"] = sorted(artifacts.failures)
            # 每 Job 一条关系 record:P3 可按 Job diff 定位冲突,不必解整张摘要。
            relation_digests.append(self._put_record("job_relation", relation))
            exclusions.extend(artifacts.exclusions)
            unapproved_unknown_total += len(artifacts.unknown_paths)
            unknown_omitted_total += (
                len(artifacts.unknown_paths) + len(artifacts.approved_unknown_paths)
            )
            terminal_total += artifacts.terminal_steps
            if artifacts.ai_config is not None:
                ai_config_record, ai_config_blob = self._collect_job_ai_config(
                    job_id, artifacts.ai_config,
                )
                ledger_digests.append(ai_config_record)
                blob_refs.add(ai_config_blob)
                ai_config_jobs.append(job_id)
            ai_config_complete = ai_config_complete and artifacts.ai_config_complete

            job_report = self.report["jobs"].setdefault(job_id, {})
            job_report.update({
                "step_results": len(artifacts.step_results),
                "failures": len(artifacts.failures),
                "terminal_steps": artifacts.terminal_steps,
                "missing_manifests": artifacts.missing_manifests,
                "exclusions": [
                    {"scope_key": item.scope_key, "step": item.step, "reason": item.reason}
                    for item in artifacts.exclusions
                ],
                "unknown_paths": artifacts.unknown_paths,
                "approved_unknown_paths": artifacts.approved_unknown_paths,
            })

        job_group.extend(relation_digests)
        if unapproved_unknown_total and not self.allow_unknown:
            candidates = [
                f"{job_id}:{path}"
                for job_id, entry in sorted(self.report["jobs"].items())
                for path in entry.get("unknown_paths", ())
            ]
            self.report["unknown_exception_candidates"] = candidates
            raise BackupError(
                f"{unapproved_unknown_total} unknown storage paths are neither manifest outputs, "
                f"runtime sidecars nor recognizable partial downloads (§5.2.23): "
                f"{candidates[:5]}; approve them via --allow-unknown-file after review"
            )
        if unapproved_unknown_total:
            self.report["unknown_accepted_without_review"] = unapproved_unknown_total

        external_parts, nas_roots = self._external_source_stats(state)
        unresolved_nas_roots = [] if not external_parts or self.vendor_media else nas_roots
        media_self_contained = not unresolved_nas_roots
        # 全部文本字节都经过 streaming scanner；精确 allowlist 只改变命中处置，
        # 不截断后续扫描。例外单列 readiness reason，不把 scan_complete 说成 false。
        secret_scan_complete = True
        readiness_reasons: list[str] = []
        if unknown_omitted_total:
            # 全局放行和逐路径审阅都只批准生成诊断快照,不证明被省略字节可重建。
            # 未收录的业务字节绝不包装成可清库恢复的完整闭包。
            readiness_reasons.append("unknown_artifacts_omitted")
        if self.manifests_missing:
            readiness_reasons.append("missing_step_manifests")
        if exclusions:
            readiness_reasons.append("excluded_step_manifests")
        if not user_config_complete:
            readiness_reasons.append("user_config_incomplete")
        if not ai_config_complete:
            readiness_reasons.append("ai_config_incomplete")
        if self.secret_exceptions:
            readiness_reasons.append("secret_scan_exceptions")
        if not media_self_contained:
            readiness_reasons.append("external_media_dependencies")
        readiness_reasons.sort()
        # relations_digest 退化为 per-job relation record 的聚合。
        relations_digest = canonical_digest(sorted(relation_digests))
        snapshot_body = {
            "format": SNAPSHOT_FORMAT,
            "repository_format": REPOSITORY_FORMAT,
            "source": {
                "app_version": self.app_version,
                "db_user_version": db_user_version,
                "manifest_format": SOURCE_MANIFEST_FORMAT,
            },
            "selector": {
                "partial": self.job_ids is not None,
                "job_ids": sorted(set(self.job_ids)) if self.job_ids is not None else [],
            },
            "records": {
                "jobs": sorted(set(job_group)),
                "parts": sorted(set(part_group)),
                "step_results": sorted(set(step_group)),
                "failures": sorted(set(failure_group)),
                "business_ledgers": sorted(set(ledger_digests)),
            },
            "blob_refs": sorted(blob_refs),
            "relations_digest": relations_digest,
            # 放行清单进 snapshot digest:操作者批准的例外必须跟着快照走,
            # 不能只留在本地 result JSON 里,让下游以为这是一次全净备份。
            "policy": {
                "successful_artifacts_only": True,
                "secrets_included": bool(self.secret_exceptions),
                "secret_scan_exceptions": sorted(self.secret_exceptions),
                "runtime_state_included": False,
            },
            "completeness": {
                "terminal_steps": terminal_total,
                "manifests_seen": self.manifests_seen,
                "manifests_missing": self.manifests_missing,
                "manifests_excluded": len(exclusions),
                "ai_config_complete": ai_config_complete,
                "user_config_complete": user_config_complete,
                "secret_scan_complete": secret_scan_complete,
                "media_self_contained": media_self_contained,
                "external_media_roots": unresolved_nas_roots,
                "portable_ready": not readiness_reasons,
                "readiness_reasons": readiness_reasons,
            },
        }
        try:
            snapshot = self.repository.put_snapshot(
                snapshot_body, record_cache=self.record_cache,
            )
        except RepositoryError as exc:
            raise BackupError(f"snapshot rejected: {exc}") from exc

        excluded_reasons: dict[str, int] = {}
        for item in exclusions:
            reason = item.reason.split(":", 1)[0]
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
        if ai_config_jobs:
            self.report["jobs_with_job_ai_config"] = ai_config_jobs
        if self.secret_exceptions:
            self.report["secret_blob_exceptions"] = sorted(self.secret_exceptions)
        self.stats.update({
            # receipt 里也留一份计数:报告是本地 result JSON,receipt 才是仓库内的账。
            # 键名避开 "secret" 字样:receipt 整体过 secret-name 扫描,会被误判。
            "blob_scan_exceptions": len(self.secret_exceptions),
            "blob_scans_truncated": 0,
            "jobs": len(state.jobs),
            "parts": sum(len(rows) for rows in state.parts.values()),
            "step_results": len(set(step_group)),
            "failure_events": len(set(failure_group)),
            "records_total": self.records_total,
            "manifests_seen": self.manifests_seen,
            "manifests_missing": self.manifests_missing,
            "terminal_steps": terminal_total,
            "manifests_excluded": len(exclusions),
            "excluded_reasons": excluded_reasons,
            "external_source_parts": external_parts,
            "nas_source_roots": nas_roots,
            "vendored_source_parts": len(vendored_source_blobs),
            "portable_ready": not readiness_reasons,
            "unknown_paths": unknown_omitted_total,
            "partial_snapshot": self.job_ids is not None,
        })
        self.repository.set_ref(self.ref, snapshot.digest)
        anchor = self._ensure_monthly_anchor(snapshot.digest)
        if anchor:
            self.report["monthly_anchor"] = anchor
        return (
            {"digest": snapshot.digest, "created": snapshot.created},
            self.stats,
            self.report,
        )


def _load_unknown_allowlist(path: Path | None) -> frozenset[str]:
    """已审批例外清单:每行一个 job_id:path,# 起注释;精确匹配,不支持通配。"""
    if path is None:
        return frozenset()
    if not path.is_file():
        raise BackupError(f"unknown allowlist {path} not found")
    entries: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if ":" not in text:
            raise BackupError(
                f"{path}:{lineno}: entry must be '<job_id>:<path>', got {text!r}"
            )
        entries.add(text)
    return frozenset(entries)


def _last_terminal_receipt(
    repository: ContentRepository, run_id: str,
) -> tuple[str, dict] | None:
    """同 run_id 的最后一条终态 receipt(§2.8-4);in_progress 不算结论。"""
    for receipt_id, body in reversed(repository.find_receipts(run_id)):
        if body["outcome"] in _TERMINAL_RECEIPT_OUTCOMES:
            return receipt_id, body
    return None


def _storage_request_identity(storage) -> dict[str, str]:
    """生成不含凭据的存储来源身份,只进入请求摘要而不落明文 receipt。"""
    identity = {
        "backend": f"{type(storage).__module__}.{type(storage).__qualname__}",
    }
    for attribute in ("jobs_dir", "_bucket", "_endpoint", "_base_url"):
        value = getattr(storage, attribute, None)
        if value is not None:
            identity[attribute.lstrip("_")] = str(value)
    return identity


def _backup_request_digest(
    *,
    db_path: Path,
    storage,
    ref: str,
    job_ids: Sequence[str] | None,
    source_instance: str | None,
    app_version: str,
    allow_unknown: bool,
    allowed_unknown: frozenset[str],
    allowed_secret_blobs: frozenset[str],
    full_rehash: bool,
    user_config_dir: Path | None,
    user_config_source_id: str | None,
    vendor_media: bool,
    consistency_retries: int,
) -> str:
    body = {
        "format": "flori-content-backup-request/v1",
        "db_path": str(Path(db_path).resolve()),
        "storage": _storage_request_identity(storage),
        "ref": ref,
        "job_ids": sorted(set(job_ids)) if job_ids is not None else None,
        "source_instance": source_instance,
        "app_version": app_version,
        "allow_unknown": allow_unknown,
        "unknown_allowlist": sorted(allowed_unknown),
        "blob_scan_allowlist": sorted(allowed_secret_blobs),
        "full_rehash": full_rehash,
        "user_config_dir": (
            str(Path(user_config_dir).resolve()) if user_config_dir is not None else None
        ),
        "user_config_source_id": user_config_source_id,
        "vendor_media": vendor_media,
        "consistency_retries": consistency_retries,
    }
    return canonical_digest(body)


async def run_backup(
    *,
    db_path: Path,
    storage,
    repository: ContentRepository,
    run_id: str,
    app_version: str,
    ref: str = "latest",
    job_ids: Sequence[str] | None = None,
    source_instance: str | None = None,
    allow_unknown: bool = False,
    unknown_allowlist: Path | None = None,
    secret_blob_allowlist: Path | None = None,
    full_rehash: bool = False,
    user_config_dir: Path | None = None,
    user_config_source_id: str | None = None,
    vendor_media: bool = False,
    consistency_retries: int = DEFAULT_CONSISTENCY_RETRIES,
    now_fn: Callable[[], str] | None = None,
    work_dir: Path | None = None,
) -> BackupResult:
    """执行一次只读便携备份;失败抛 BackupError 且 refs 保持旧值。

    同一 run_id 已有成功 receipt 且其 ref 已就位时直接返回原 snapshot(§2.8-4);
    ref 未就位(上次在 set_ref 前后中断)则走完整路径补设,不空转。
    局部备份(job_ids)禁止写默认 latest:局部快照不代表系统全貌。
    """
    if consistency_retries < 1:
        raise BackupError("consistency_retries must be >= 1")
    # ref 名先于取锁校验:非法名不该先占锁再失败。
    try:
        validate_ref_name(ref)
    except RepositoryError as exc:
        raise BackupError(str(exc)) from exc
    if job_ids is not None and ref == "latest":
        raise BackupError(
            "partial backup (job filter) must not write the default 'latest' ref; "
            "pass an explicit ref name"
        )
    now = now_fn or _default_now
    allowed_unknown = _load_unknown_allowlist(unknown_allowlist)
    allowed_secret_blobs = _load_unknown_allowlist(secret_blob_allowlist)
    request_digest = _backup_request_digest(
        db_path=db_path,
        storage=storage,
        ref=ref,
        job_ids=job_ids,
        source_instance=source_instance,
        app_version=app_version,
        allow_unknown=allow_unknown,
        allowed_unknown=allowed_unknown,
        allowed_secret_blobs=allowed_secret_blobs,
        full_rehash=full_rehash,
        user_config_dir=user_config_dir,
        user_config_source_id=user_config_source_id,
        vendor_media=vendor_media,
        consistency_retries=consistency_retries,
    )

    terminal = _last_terminal_receipt(repository, run_id)
    if terminal is not None and terminal[1]["outcome"] == "success":
        receipt_id, body = terminal
        previous_request = body.get("request_digest")
        if previous_request != request_digest:
            detail = "missing request digest" if previous_request is None else "different request digest"
            raise BackupError(
                f"run_id {run_id!r} already completed with a {detail}; "
                "reuse is allowed only for the identical canonical request"
            )
        digest = body["snapshot_digest"]
        # 成功回执必须蕴含 ref 已就位;不成立说明上次在 set_ref 附近中断,补跑。
        ref_ok = repository.has_snapshot(digest)
        if ref_ok:
            try:
                ref_ok = repository.get_ref(ref) == digest
            except RepositoryError:
                ref_ok = False
        if ref_ok:
            return BackupResult(
                snapshot_digest=digest,
                receipt_id=receipt_id,
                hit_existing_snapshot=True,
                reused_run=True,
                stats=dict(body.get("stats", {})),
                report={"reused_run": True},
                request_digest=request_digest,
            )

    with repository.write_lock(f"backup-{run_id}"):
        repository.clean_tmp()
        # 进行中标记(§2.8-4 三态):崩溃后可据此判断上次是否跑到一半。
        repository.write_receipt({
            "run_id": run_id, "observed_at": now(), "outcome": "in_progress",
            "request_digest": request_digest,
        })
        with tempfile.TemporaryDirectory(
            prefix="flori-content-backup-", dir=str(work_dir) if work_dir else None,
        ) as work:
            run = _BackupRun(
                db_path=Path(db_path),
                storage=storage,
                repository=repository,
                run_id=run_id,
                app_version=app_version,
                ref=ref,
                job_ids=job_ids,
                allow_unknown=allow_unknown,
                allowed_unknown=allowed_unknown,
                allowed_secret_blobs=allowed_secret_blobs,
                user_config_dir=user_config_dir,
                vendor_media=vendor_media,
                full_rehash=full_rehash,
                consistency_retries=consistency_retries,
                work_dir=Path(work),
                now_fn=now,
            )
            try:
                snapshot, stats, report = await run.execute()
            except BaseException as exc:
                _write_failure_receipt(repository, run_id, request_digest, now, exc)
                raise
            # 终态 receipt 在 set_ref 之后:成功回执必须蕴含 ref 已就位。
            receipt_id = repository.write_receipt({
                "run_id": run_id,
                "observed_at": now(),
                "outcome": "success",
                "snapshot_digest": snapshot["digest"],
                "request_digest": request_digest,
                "hit_existing_snapshot": not snapshot["created"],
                "stats": stats,
                **({"source_instance": source_instance} if source_instance else {}),
            })
            return BackupResult(
                snapshot_digest=snapshot["digest"],
                receipt_id=receipt_id,
                hit_existing_snapshot=not snapshot["created"],
                reused_run=False,
                stats=stats,
                report=report,
                request_digest=request_digest,
            )


def _write_failure_receipt(
    repository: ContentRepository, run_id: str, request_digest: str,
    now_fn: Callable[[], str], exc: BaseException,
) -> None:
    """尽力而为的失败 receipt(§2.8-4 恢复协议线索);它自己的失败绝不掩盖原始错误。"""
    try:
        repository.write_receipt({
            "run_id": run_id,
            "observed_at": now_fn(),
            "outcome": "failed",
            "request_digest": request_digest,
            "error": str(exc)[:2000],
        })
    except Exception as receipt_exc:  # noqa: BLE001
        _log.warning(
            "content_backup_failure_receipt_failed",
            run_id=run_id, error=str(receipt_exc),
        )



# --verify 的覆盖边界:仓库自洽性,不是业务正确性。
VERIFY_SCOPE = (
    "repository self-consistency only: blob/record/snapshot digests, canonical "
    "bytes, ref targets and reference closure. It does NOT prove the snapshot "
    "captured the right business state; use the backup report and the §5.2.23 "
    "coverage review for that."
)


def _open_or_create_repository(path: Path) -> ContentRepository:
    if (path / "repository.json").is_file():
        return ContentRepository.open(path)
    return ContentRepository.create(path)


def _emit_result(payload: dict, result_file: ResultDestination | None) -> None:
    emit_result(payload, result_file)


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器;独立出来是为了让脚本发出的 argv 能喂进真解析器对账。"""
    parser = argparse.ArgumentParser(prog="content-backup", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="创建/增量便携内容备份")
    backup.add_argument("--repo", required=True)
    backup.add_argument("--db", required=True)
    backup.add_argument("--jobs-dir", default="/data/jobs", help="本地存储根;走 MinIO 时忽略")
    backup.add_argument("--ref", default="latest")
    backup.add_argument("--job", action="append", dest="jobs")
    backup.add_argument("--run-id", default=None)
    backup.add_argument("--app-version", default=None)
    backup.add_argument("--source-instance", default=None)
    backup.add_argument(
        "--allow-unknown", action="store_true",
        help="放行全部未知路径(默认关,报告会警示;优先用 --allow-unknown-file)",
    )
    backup.add_argument(
        "--allow-unknown-file", default=None,
        help="已审批例外清单,每行 <job_id>:<path>,精确匹配",
    )
    backup.add_argument(
        "--allow-secret-blob-file", default=None,
        help="文本 blob 密钥扫描的例外清单(逐行 job_id:path);审阅后才放行",
    )
    backup.add_argument(
        "--full-rehash", action="store_true",
        help="文本输出每轮都会重读并扫描;本选项额外重读二进制CAS,用于全介质"
             "摘要与位腐蚀审计,不作为密钥扫描基线",
    )
    backup.add_argument(
        "--user-config-dir", default="/data/prompts",
        help="运行态可变用户配置根;仅收 prompts/profiles/styles/templates 允许路径",
    )
    backup.add_argument(
        "--user-config-source-id", default=None, help=argparse.SUPPRESS,
    )
    backup.add_argument(
        "--vendor-media", action="store_true",
        help="校验 source_ref/source_digest/size_bytes 后把外部 NAS 源媒体收入 CAS",
    )
    backup.add_argument("--work-dir", default=None, help="DB 副本等大临时文件的落盘目录")
    backup.add_argument("--retries", type=int, default=DEFAULT_CONSISTENCY_RETRIES)
    backup.add_argument("--result-file", default=None)

    verify = sub.add_parser("verify", help=f"校验仓库自洽性({VERIFY_SCOPE})")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--result-file", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "verify":
        try:
            args.result_file = prepare_result_destination(args.result_file, args.repo)
        except ResultFileError as exc:
            emit_result({"ok": False, "error": str(exc)}, None)
            return 2
        try:
            repository = ContentRepository.open(Path(args.repo))
            report = repository.scrub()
        except (RepositoryError, PolicyError) as exc:
            _emit_result(
                {"ok": False, "scope": VERIFY_SCOPE, "error": str(exc)}, args.result_file,
            )
            return 1
        payload = {
            "ok": report.ok,
            "scope": VERIFY_SCOPE,
            "checked": {
                "blobs": report.checked_blobs,
                "records": report.checked_records,
                "snapshots": report.checked_snapshots,
                "refs": report.checked_refs,
                "receipts": report.checked_receipts,
            },
            "issues": [
                {"kind": issue.kind, "path": issue.path, "detail": issue.detail}
                for issue in report.issues
            ],
        }
        _emit_result(payload, args.result_file)
        return 0 if report.ok else 1

    from .storage import create_storage
    from .version import FLORI_VERSION

    db_file = Path(args.db)
    jobs_root = Path(args.jobs_dir)
    config_root = Path(args.user_config_dir) if args.user_config_dir else None
    work_root = Path(args.work_dir) if args.work_dir else None
    try:
        source_roots = tuple(source_roots_from_env().values())
        container_data_root = Path("/data") if Path("/data") in db_file.parents else None
        data_roots = tuple(path for path in (
            container_data_root,
            jobs_root,
            config_root,
            *source_roots,
        ) if path is not None)
        output_roots = tuple(path for path in (Path(args.repo), work_root) if path is not None)
        ensure_output_roots_disjoint(output_roots, data_roots)
        if work_root is not None:
            ensure_output_roots_disjoint((work_root,), (Path(args.repo),))
        args.result_file = prepare_result_destination(
            args.result_file,
            args.repo,
            protected_roots=(*data_roots, *((work_root,) if work_root else ())),
            protected_files=(db_file,),
        )
    except (ResultFileError, SourceReferenceError) as exc:
        emit_result({"ok": False, "error": str(exc)}, None)
        return 2

    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    if args.jobs is not None and args.ref == "latest":
        _emit_result({
            "ok": False, "run_id": run_id,
            "error": "--job requires an explicit --ref: a partial snapshot must not "
                     "overwrite 'latest'",
        }, args.result_file)
        return 2
    # 存在性 preflight 在容器内做:宿主与容器看到的是不同挂载视图。
    if not db_file.is_file():
        _emit_result(
            {"ok": False, "run_id": run_id, "error": f"database not found: {db_file}"},
            args.result_file,
        )
        return 1
    try:
        repository = _open_or_create_repository(Path(args.repo))
        result = asyncio.run(run_backup(
            db_path=db_file,
            storage=create_storage(Path(args.jobs_dir)),
            repository=repository,
            run_id=run_id,
            app_version=args.app_version or FLORI_VERSION,
            ref=args.ref,
            job_ids=args.jobs,
            source_instance=args.source_instance,
            allow_unknown=args.allow_unknown,
            unknown_allowlist=Path(args.allow_unknown_file) if args.allow_unknown_file else None,
            secret_blob_allowlist=(
                Path(args.allow_secret_blob_file) if args.allow_secret_blob_file else None
            ),
            full_rehash=args.full_rehash,
            user_config_dir=Path(args.user_config_dir) if args.user_config_dir else None,
            user_config_source_id=args.user_config_source_id,
            vendor_media=args.vendor_media,
            consistency_retries=args.retries,
            work_dir=Path(args.work_dir) if args.work_dir else None,
        ))
    except (BackupError, RepositoryError, PolicyError) as exc:
        _emit_result({"ok": False, "run_id": run_id, "error": str(exc)}, args.result_file)
        return 1
    _emit_result({
        "ok": True,
        "run_id": run_id,
        "snapshot_digest": result.snapshot_digest,
        "receipt_id": result.receipt_id,
        "hit_existing_snapshot": result.hit_existing_snapshot,
        "reused_run": result.reused_run,
        "stats": result.stats,
        "report": result.report,
        "request_digest": result.request_digest,
    }, args.result_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
