"""SQLite 数据库层。"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import html
import json
import os
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import structlog

from .concepts import norm_related as _norm_related
from .models import (
    AIUsage,
    Collection,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_PROVIDER,
    Job,
    JobStatus,
    Step,
    StepStatus,
    Worker,
)
from .migrations import (
    Migration,
    UnsupportedSchemaVersionError,
    assert_schema_compatible,
    current_schema_version,
    migration_steps,
    run_migrations,
    validate_registry,
)
from .ids import lineage_key_of as _lineage_key_of
from .status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    STALE,
    compute_worker_status,
)
from .study import (
    MAX_SQLITE_INTEGER,
    STUDY_STATUSES,
    StudyConflictError,
    StudyFaultInjector,
    StudyNotFoundError,
    canonical_utc_iso,
    datetime_to_epoch_us,
    require_aware_utc,
    review_request_fingerprint,
    schedule_next_review,
    utc_now,
    validate_review_request,
)
from .study_suggestions import (
    MAX_GENERATED_CARDS,
    StudySuggestionConflictError,
    StudySuggestionFaultInjector,
    StudySuggestionNotFoundError,
    canonical_json,
    content_fingerprint,
    knowledge_fingerprint,
    operation_payload,
    parse_ai_suggestions,
    payload_fingerprint,
    require_external_request_id,
    require_identifier,
    require_plain_int,
    require_revision,
    resolve_study_suggestion_prompt,
    sha256_text,
    study_suggestion_generator_fingerprint,
    validate_study_suggestion_prompt_snapshot,
    validate_card_content,
    validate_operation_items,
)
from .repositories.runtime import DatabaseRuntime as _DatabaseRuntime

# schema 版本只从不可变迁移清单读取。
SCHEMA_VERSION = current_schema_version()

# SQLite INTEGER 是有符号 64 位整数.Prompt 版本从 1 开始,这组边界同时供
# API schema 和 DB 绑定前防御使用.
PROMPT_VERSION_MIN = 1
PROMPT_VERSION_MAX = (1 << 63) - 1
PROMPT_VERSION_EXCLUSIVE_MAX = 1 << 63


class PromptVersionExhaustedError(ValueError):
    """Prompt 历史已用完 SQLite 可表示的正整数版本."""


class ConceptNotFoundError(LookupError):
    """概念不存在，不能创建 occurrence 或定义版本。"""


class ConceptConflictError(RuntimeError):
    """概念版本、锁修订或证据集合已在并发中变化。"""


class ConceptEvidenceError(ValueError):
    """概念来源证据不存在、失效或不属于该概念身份。"""


def _valid_prompt_version(version: object) -> bool:
    """DB 绑定前校验 Prompt 版本;bool 和整数子类也不作为版本."""
    return (
        type(version) is int
        and PROMPT_VERSION_MIN <= version <= PROMPT_VERSION_MAX
    )


_SQLITE_HEADER = b"SQLite format 3\x00"
_WAL_MAGIC = frozenset({0x377F0682, 0x377F0683})
_WAL_FORMAT_VERSION = 3_007_000
_WAL_INDEX_PAGE_SIZE = 32_768
_ROLLBACK_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9 \xa1c\xd7"


class _ProbeFilesChanged(UnsupportedSchemaVersionError):
    """连接前副本采集期间源文件变化,允许有限次重新取稳定状态。"""


class _WalIndexValidationError(UnsupportedSchemaVersionError):
    """WAL-index advisory 结构无效,不影响 WAL recovery 真相。"""


def _header_user_version(page_one: bytes) -> int:
    if len(page_one) < 64 or not page_one.startswith(_SQLITE_HEADER):
        raise ValueError("SQLite page-1 header 非法")
    return struct.unpack(">I", page_one[60:64])[0]


def _wal_checksum(
    payload: bytes,
    checksum: tuple[int, int] = (0, 0),
    *,
    byteorder: str,
) -> tuple[int, int]:
    """按 SQLite wal.c 的 8-byte rolling checksum 计算连续校验值。"""
    if len(payload) % 8:
        raise ValueError("WAL checksum payload 未按 8 字节对齐")
    first, second = checksum
    for offset in range(0, len(payload), 8):
        word_one = int.from_bytes(payload[offset : offset + 4], byteorder)
        word_two = int.from_bytes(payload[offset + 4 : offset + 8], byteorder)
        first = (first + word_one + second) & 0xFFFFFFFF
        second = (second + word_two + first) & 0xFFFFFFFF
    return first, second


def _validate_wal_index(
    payload: bytes,
    *,
    wal_header: bytes,
    page_size: int,
    checksum_byteorder: str,
    file_size: int | None = None,
) -> tuple[int, int, tuple[int, int]]:
    """验证 WAL-index 双 header 与 checkpoint 边界。"""
    measured_size = len(payload) if file_size is None else file_size
    if (
        len(payload) < 136
        or measured_size < 136
        or measured_size % _WAL_INDEX_PAGE_SIZE
    ):
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory 大小非法"
        )
    header = payload[:48]
    if header != payload[48:96]:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory 双 header 不一致"
        )

    def native_uint(offset: int, size: int = 4) -> int:
        return int.from_bytes(header[offset : offset + size], sys.byteorder)

    if native_uint(0) != _WAL_FORMAT_VERSION or header[4:8] != b"\x00" * 4:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory version 或 padding 非法"
        )
    if header[12] != 1:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory 尚未初始化"
        )
    expected_big_end = 1 if checksum_byteorder == "big" else 0
    if header[13] != expected_big_end:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory checksum 字节序与 WAL 不一致"
        )
    index_page_size = native_uint(14, 2)
    if index_page_size == 1:
        index_page_size = 65_536
    if index_page_size != page_size:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory page_size 与 WAL 不一致"
        )
    if header[32:40] != wal_header[16:24]:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory salt 与 WAL 不一致"
        )
    calculated = _wal_checksum(header[:40], byteorder=sys.byteorder)
    stored = (native_uint(40), native_uint(44))
    if calculated != stored:
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory header checksum 非法"
        )

    mx_frame = native_uint(16)
    database_pages = native_uint(20)
    frame_checksum = (native_uint(24), native_uint(28))
    n_backfill = int.from_bytes(payload[96:100], sys.byteorder)
    n_backfill_attempted = int.from_bytes(payload[128:132], sys.byteorder)
    if not (n_backfill <= n_backfill_attempted <= mx_frame):
        raise _WalIndexValidationError(
            "SQLite WAL-index advisory checkpoint 边界非法"
        )
    return mx_frame, database_pages, frame_checksum


def _validate_wal_bytes(
    database: Path,
    wal: Path,
    shm_header: bytes | None = None,
    shm_size: int | None = None,
) -> tuple[int, int, int]:
    """验证 current-salt 连续前缀,返回最后 committed WAL 字节边界。"""
    with database.open("rb") as stream:
        database_header = stream.read(100)
    if not database_header.startswith(_SQLITE_HEADER):
        raise UnsupportedSchemaVersionError("SQLite WAL 对应主库 header 非法")
    if database_header[18:20] != b"\x02\x02":
        raise UnsupportedSchemaVersionError(
            "SQLite WAL 对应主库未声明 WAL 模式，拒绝写性打开"
        )
    with wal.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise UnsupportedSchemaVersionError(
                "SQLite WAL header 不完整，拒绝写性打开"
            )
        magic, format_version, page_size = struct.unpack(">III", header[:12])
        if magic not in _WAL_MAGIC:
            raise UnsupportedSchemaVersionError(
                "SQLite WAL magic 非法，拒绝写性打开"
            )
        if format_version != _WAL_FORMAT_VERSION:
            raise UnsupportedSchemaVersionError(
                "SQLite WAL format version 非法，拒绝写性打开"
            )
        stored_page_size = struct.unpack(">H", database_header[16:18])[0]
        if stored_page_size == 1:
            stored_page_size = 65536
        if (
            page_size < 512
            or page_size > 65536
            or page_size & (page_size - 1)
            or page_size != stored_page_size
        ):
            raise UnsupportedSchemaVersionError(
                "SQLite WAL page_size 非法，拒绝写性打开"
            )
        byteorder = "big" if magic == 0x377F0683 else "little"
        checksum = _wal_checksum(header[:24], byteorder=byteorder)
        stored_header_checksum = struct.unpack(">II", header[24:32])
        if checksum != stored_header_checksum:
            raise UnsupportedSchemaVersionError(
                "SQLite WAL header checksum 非法，拒绝写性打开"
            )
        frame_size = 24 + page_size
        wal_size = wal.stat().st_size
        if (wal_size - 32) % frame_size:
            raise UnsupportedSchemaVersionError(
                "SQLite WAL trailing bytes 未组成完整 frame，拒绝写性打开"
            )
        physical_frames = (wal_size - 32) // frame_size
        index_state = None
        if shm_header is not None:
            try:
                index_state = _validate_wal_index(
                    shm_header,
                    wal_header=header,
                    page_size=page_size,
                    checksum_byteorder=byteorder,
                    file_size=shm_size,
                )
            except _WalIndexValidationError as exc:
                _log.warning(
                    "sqlite_wal_index_advisory_invalid",
                    reason=str(exc),
                )

        salts = header[16:24]
        frame_count = 0
        commit_count = 0
        last_commit_frame = 0
        index_boundary: tuple[int, tuple[int, int]] | None = None
        stale_tail = False
        for frame_index in range(1, physical_frames + 1):
            frame_header = stream.read(24)
            if len(frame_header) != 24:
                raise UnsupportedSchemaVersionError(
                    "SQLite WAL trailing frame header 不完整，拒绝写性打开"
                )
            page = stream.read(page_size)
            if len(page) != page_size:
                raise UnsupportedSchemaVersionError(
                    "SQLite WAL trailing frame page 不完整，拒绝写性打开"
                )
            page_number, database_pages = struct.unpack(">II", frame_header[:8])
            frame_salts = frame_header[8:16]
            if stale_tail:
                if page_number == 0 or frame_salts == salts:
                    raise UnsupportedSchemaVersionError(
                        "SQLite WAL 旧代物理尾结构非法，拒绝写性打开"
                    )
                continue
            if frame_salts != salts:
                if page_number == 0:
                    raise UnsupportedSchemaVersionError(
                        "SQLite WAL frame salt 非法，拒绝写性打开"
                    )
                stale_tail = True
                continue
            if page_number == 0:
                raise UnsupportedSchemaVersionError(
                    "SQLite WAL frame page number 非法，拒绝写性打开"
                )
            checksum = _wal_checksum(
                frame_header[:8] + page,
                checksum,
                byteorder=byteorder,
            )
            if checksum != struct.unpack(">II", frame_header[16:24]):
                raise UnsupportedSchemaVersionError(
                    "SQLite WAL frame checksum 非法，拒绝写性打开"
                )
            frame_count += 1
            if database_pages:
                commit_count += 1
                last_commit_frame = frame_index
                if index_state is not None and frame_index == index_state[0]:
                    index_boundary = (database_pages, checksum)
        if index_state is not None:
            index_frame, index_pages, index_checksum = index_state
            if index_frame > last_commit_frame or (
                index_frame
                and (
                    index_boundary is None
                    or index_boundary[0] != index_pages
                    or index_boundary[1] != index_checksum
                )
            ):
                _log.warning(
                    "sqlite_wal_index_advisory_stale",
                    index_mx_frame=index_frame,
                    wal_commit_frame=last_commit_frame,
                )
    logical_size = 32 + last_commit_frame * frame_size
    return frame_count, commit_count, logical_size


def _optional_file_signature(path: Path) -> tuple[int, int, int, int] | None:
    if path.is_symlink():
        raise UnsupportedSchemaVersionError(
            f"SQLite sidecar 不得是符号链接: {path.name}"
        )
    if not path.exists():
        return None
    if not path.is_file():
        raise UnsupportedSchemaVersionError(
            f"SQLite sidecar 不是普通文件: {path.name}"
        )
    return _file_snapshot_signature(path)


def _assert_file_signatures(
    expected: dict[Path, tuple[int, int, int, int] | None],
) -> None:
    current = {path: _optional_file_signature(path) for path in expected}
    if current != expected:
        raise _ProbeFilesChanged(
            "SQLite DB/WAL/SHM 在连接前探测期间发生变化，拒绝写性打开"
        )


def _assert_optional_regular_file(path: Path) -> None:
    """SHM 内容可随运行时变化，但路径不得变成链接或特殊文件。"""
    _optional_file_signature(path)


def _read_wal_index_advisory(path: Path) -> tuple[bytes, int] | None:
    """只读 WAL-index 固定前缀；内容不可读时仍以 WAL 作恢复真相。"""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UnsupportedSchemaVersionError(
                f"SQLite sidecar 不是普通文件: {path.name}"
            )
        if info.st_size == 0:
            return b"", 0
        return os.read(descriptor, 136), info.st_size
    except UnsupportedSchemaVersionError:
        raise
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsupportedSchemaVersionError(
                f"SQLite sidecar 不得是符号链接: {path.name}"
            ) from exc
        _log.warning(
            "sqlite_wal_index_advisory_unreadable",
            error_type=type(exc).__name__,
            errno=exc.errno,
        )
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _committed_wal_user_version(path: Path) -> int:
    """机械验 WAL 后在隔离副本交给 SQLite 判定最后 committed state。"""
    wal_path = path.with_name(path.name + "-wal")
    shm_path = path.with_name(path.name + "-shm")
    last_change: BaseException | None = None
    try:
        for attempt in range(3):
            try:
                wal_signature = _optional_file_signature(wal_path)
                _assert_optional_regular_file(shm_path)
                signatures = {
                    path: _optional_file_signature(path),
                    wal_path: wal_signature,
                }
                if wal_signature is None or wal_signature[2] == 0:
                    with path.open("rb") as stream:
                        version = _header_user_version(stream.read(100))
                    _assert_file_signatures(signatures)
                    _assert_optional_regular_file(shm_path)
                    return version
                with tempfile.TemporaryDirectory(
                    prefix="flori-sqlite-wal-probe-"
                ) as temporary:
                    copied_database = Path(temporary) / path.name
                    copied_wal = copied_database.with_name(
                        copied_database.name + "-wal"
                    )
                    shm_advisory = _read_wal_index_advisory(shm_path)
                    shm_header, shm_size = (
                        shm_advisory if shm_advisory is not None else (None, None)
                    )
                    shutil.copy2(path, copied_database)
                    shutil.copy2(wal_path, copied_wal)
                    _assert_file_signatures(signatures)
                    _assert_optional_regular_file(shm_path)
                    _frames, _commits, logical_size = _validate_wal_bytes(
                        copied_database,
                        copied_wal,
                        shm_header,
                        shm_size,
                    )
                    with copied_wal.open("r+b") as stream:
                        stream.truncate(logical_size)
                    connection = sqlite3.connect(str(copied_database))
                    try:
                        version = int(
                            connection.execute("PRAGMA user_version").fetchone()[0]
                        )
                        integrity = connection.execute(
                            "PRAGMA integrity_check"
                        ).fetchone()
                    finally:
                        connection.close()
                    if not integrity or integrity[0] != "ok":
                        raise UnsupportedSchemaVersionError(
                            "SQLite WAL 恢复副本 integrity_check 失败: "
                            f"{integrity}"
                        )
                    _assert_file_signatures(signatures)
                    _assert_optional_regular_file(shm_path)
                    return version
            except (_ProbeFilesChanged, FileNotFoundError) as exc:
                last_change = exc
                if attempt < 2:
                    # 正常写事务会短暂改动 WAL；退避后仍须取得稳定副本，否则 fail-closed。
                    time.sleep(0.01 * (attempt + 1))
                continue
        raise UnsupportedSchemaVersionError(
            "SQLite DB/WAL 无法取得稳定副本，拒绝写性打开"
        ) from last_change
    except UnsupportedSchemaVersionError:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise UnsupportedSchemaVersionError(
            f"SQLite WAL 无法安全预恢复，拒绝写性打开: {exc}"
        ) from exc


def _file_snapshot_signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _validate_rollback_journal_header(path: Path, journal: Path) -> None:
    """只核 journal 头部边界；page recovery 仍完全交给 SQLite 副本。"""
    with path.open("rb") as stream:
        database_header = stream.read(100)
    with journal.open("rb") as stream:
        header = stream.read(32)
    if len(header) < 28 or header[:8] != _ROLLBACK_JOURNAL_MAGIC:
        raise UnsupportedSchemaVersionError(
            "SQLite rollback journal 非法或非 hot 状态，拒绝写性打开"
        )
    if not database_header.startswith(_SQLITE_HEADER):
        raise UnsupportedSchemaVersionError(
            "SQLite rollback journal 对应主库 header 非法，拒绝写性打开"
        )
    records, _nonce, database_pages, sector_size, page_size = struct.unpack(
        ">IIIII", header[8:28]
    )
    stored_page_size = struct.unpack(">H", database_header[16:18])[0]
    if stored_page_size == 1:
        stored_page_size = 65536
    if records == 0 or (
        database_pages == 0
        or sector_size < 512
        or sector_size > 65536
        or sector_size & (sector_size - 1)
        or page_size != stored_page_size
        or page_size < 512
        or page_size > 65536
        or page_size & (page_size - 1)
        or journal.stat().st_size < sector_size + page_size + 4
    ):
        raise UnsupportedSchemaVersionError(
            "SQLite rollback journal header 边界非法，拒绝写性打开"
        )


def _rollback_journal_user_version(path: Path) -> int | None:
    """在隔离副本恢复 hot journal，避免探测阶段改写真实 DB。"""
    journal = path.with_name(path.name + "-journal")
    journal_signature = _optional_file_signature(journal)
    if journal_signature is None or journal_signature[2] == 0:
        return None
    wal = path.with_name(path.name + "-wal")
    wal_signature = _optional_file_signature(wal)
    shm = path.with_name(path.name + "-shm")
    shm_signature = _optional_file_signature(shm)
    if wal_signature is not None and wal_signature[2]:
        raise UnsupportedSchemaVersionError(
            "SQLite 同时存在 WAL 与 rollback journal，拒绝写性打开"
        )
    try:
        _validate_rollback_journal_header(path, journal)
        signatures = {
            path: _optional_file_signature(path),
            journal: journal_signature,
            wal: wal_signature,
            shm: shm_signature,
        }
        with tempfile.TemporaryDirectory(prefix="flori-sqlite-probe-") as temporary:
            copied_database = Path(temporary) / path.name
            copied_journal = copied_database.with_name(
                copied_database.name + "-journal"
            )
            shutil.copy2(path, copied_database)
            shutil.copy2(journal, copied_journal)
            _assert_file_signatures(signatures)
            connection = sqlite3.connect(str(copied_database))
            try:
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                connection.close()
            if not integrity or integrity[0] != "ok":
                raise UnsupportedSchemaVersionError(
                    f"SQLite rollback journal 恢复副本 integrity_check 失败: {integrity}"
                )
            if copied_journal.exists() and copied_journal.stat().st_size:
                with copied_journal.open("rb") as stream:
                    recovered_magic = stream.read(len(_ROLLBACK_JOURNAL_MAGIC))
                if recovered_magic == _ROLLBACK_JOURNAL_MAGIC:
                    raise UnsupportedSchemaVersionError(
                        "SQLite rollback journal 副本未完成恢复，拒绝写性打开"
                    )
            _assert_file_signatures(signatures)
            return version
    except UnsupportedSchemaVersionError:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise UnsupportedSchemaVersionError(
            f"SQLite rollback journal 无法安全预恢复，拒绝写性打开: {exc}"
        ) from exc


def _probe_schema_version_without_sqlite(path: Path) -> int | None:
    """连接前探测 recovery 后版本；真实 DB 仍保留 copy 到 connect 的 TOCTOU。"""
    sidecar_signatures = {
        suffix: _optional_file_signature(path.with_name(path.name + suffix))
        for suffix in ("-wal", "-journal", "-shm")
    }
    if not path.exists() or path.stat().st_size == 0:
        active_sidecars = [
            path.name + suffix
            for suffix, signature in sidecar_signatures.items()
            if signature is not None and signature[2]
        ]
        if active_sidecars:
            raise UnsupportedSchemaVersionError(
                f"SQLite 主库为空但存在非空 sidecar，拒绝写性打开: {active_sidecars}"
            )
        return 0
    journal_version = _rollback_journal_user_version(path)
    if journal_version is not None:
        return journal_version
    with path.open("rb") as stream:
        header = stream.read(100)
    if not header.startswith(_SQLITE_HEADER):
        return None
    try:
        _header_user_version(header)
    except ValueError:
        return None
    return _committed_wal_user_version(path)


_JOB_UPDATABLE = {
    "status", "title", "progress_pct", "error", "updated_at",
    "meta", "style_tags", "domain", "source", "collection_id",
    "published_at",
    "lineage_key", "is_current", "source_digest", "pipeline_digest", "parent_job_id",
}
_STEP_UPDATABLE = {
    "status", "input_hash", "worker_id", "started_at", "finished_at",
    "duration_sec", "meta", "error", "retries",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_log = structlog.get_logger(component="db")


@lru_cache(maxsize=1)
def _fernet():
    """凭证 at-rest 加密的 Fernet 实例(按 FLORI_SECRET_KEY 缓存)。

    key 取自环境变量 FLORI_SECRET_KEY(urlsafe-base64 的 32 字节 Fernet key)。
    未设/为空 → 返回 None(凭证退回明文存储,向后兼容)。cryptography 在此惰性
    导入,缺库或 key 非法时返回 None,使本模块在无该依赖/未配 key 时仍可正常 import
    与运行(其它 DB 用法与测试不受影响)。"""
    key = (os.environ.get("FLORI_SECRET_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet  # 惰性导入:缺库也不影响模块 import
        return Fernet(key.encode())
    except Exception as e:  # 库缺失 / key 非法 → 退回明文(不阻断启动)
        _log.warning("credential_fernet_init_failed", error=str(e)[:200])
        return None


_PLAINTEXT_CRED_WARNED = False


def _warn_plaintext_credentials_once() -> None:
    """无 Fernet key 时存凭证仅警告一次,提示设 FLORI_SECRET_KEY 以加密 at-rest。"""
    global _PLAINTEXT_CRED_WARNED
    if not _PLAINTEXT_CRED_WARNED:
        _PLAINTEXT_CRED_WARNED = True
        _log.warning(
            "credentials_stored_plaintext",
            hint="set FLORI_SECRET_KEY (a Fernet key) to encrypt app_credentials at rest",
        )


def _clean_search_query(q: str) -> str:
    """归一化检索输入;空字节不能进入 SQLite 字符串绑定。"""
    return " ".join((q or "").replace("\x00", "").split())


def _fts_match_query(q: str) -> str:
    """把用户查询串包成 fts5 安全的双引号短语,防 MATCH 语法注入。
    内部双引号转义为两个双引号;空白折叠;空查询返回空串(调用方按无结果处理)。"""
    # 剔除空字节(null byte):sqlite3 绑定含 \x00 的串会抛 "unterminated string";它也非有效检索词。
    cleaned = _clean_search_query(q)
    if not cleaned:
        return ""
    escaped = cleaned.replace('"', '""')
    return f'"{escaped}"'


def _two_cjk_query(q: str) -> str | None:
    """返回恰好两个 CJK 字符的短查询,其他查询仍由 FTS5 处理。"""
    cleaned = _clean_search_query(q)
    if len(cleaned) != 2:
        return None
    if all("\u4e00" <= char <= "\u9fff" for char in cleaned):
        return cleaned
    return None


def _substring_snippet(body: str, title: str, needle: str) -> str:
    """为两字 CJK fallback 生成安全高亮摘要。"""
    body_text = body or ""
    title_text = title or ""
    source = body_text if needle in body_text else title_text
    if not source:
        return ""
    index = source.find(needle)
    if index < 0:
        index = 0
    start = max(0, index - 60)
    end = min(len(source), index + len(needle) + 60)
    prefix = "…" if start else ""
    suffix = "…" if end < len(source) else ""
    escaped = html.escape(source[start:end])
    escaped_needle = html.escape(needle)
    highlighted = escaped.replace(
        escaped_needle, f"<mark>{escaped_needle}</mark>"
    )
    return f"{prefix}{highlighted}{suffix}"


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _normalized_body_sha256(text: str) -> str:
    """按稳定换行与 Unicode 形态计算 chunk 指纹。"""
    normalized = unicodedata.normalize(
        "NFC", (text or "").replace("\r\n", "\n")
    )
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    return _sha256_text(normalized)


def _chunk_note_body(
    body: str, *, max_chars: int = 1400, overlap: int = 120
) -> list[dict]:
    """把笔记正文切成问答证据块。按段落聚合,超长段落滑窗切分;返回 char offset 便于回看。"""
    text = body or ""
    if not text.strip():
        return []

    paragraphs: list[tuple[int, int, str]] = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        stripped = raw.strip()
        if stripped:
            start = text.find(raw, pos)
            if start < 0:
                start = pos
            end = start + len(raw)
            paragraphs.append((start, end, raw.rstrip("\n")))
            pos = end
        else:
            pos += len(raw)

    if not paragraphs:
        return []

    chunks: list[dict] = []
    cur_parts: list[str] = []
    cur_start = paragraphs[0][0]
    cur_end = paragraphs[0][0]
    section = ""
    cur_section = ""

    def emit() -> None:
        nonlocal cur_parts, cur_start, cur_end, cur_section
        body_text = "\n".join(p for p in cur_parts if p).strip()
        if body_text:
            chunks.append({
                "body": body_text,
                "section": cur_section,
                "char_start": cur_start,
                "char_end": cur_end,
            })
        cur_parts = []

    for start, end, para in paragraphs:
        if para.lstrip().startswith("#"):
            # 新 heading 开启新证据段;先结算旧 section,避免跨节归属。
            if cur_parts:
                emit()
            section = para.lstrip("#").strip() or section
        if len(para) > max_chars:
            emit()
            step = max(1, max_chars - overlap)
            for off in range(0, len(para), step):
                part = para[off : off + max_chars].strip()
                if not part:
                    continue
                chunks.append({
                    "body": part,
                    "section": section,
                    "char_start": start + off,
                    "char_end": min(start + off + len(part), end),
                })
            cur_start = end
            cur_end = end
            cur_section = section
            continue
        projected = sum(len(p) for p in cur_parts) + len(cur_parts) + len(para)
        if cur_parts and projected > max_chars:
            emit()
            cur_start = start
            cur_section = section
        elif not cur_parts:
            cur_start = start
            cur_section = section
        cur_parts.append(para)
        cur_end = end

    emit()
    return chunks


def _canonical_ids_from_evidence_json(value: object) -> list[str]:
    """读取当前 chunk 快照中的 canonical ID；畸形存量 JSON 安全降级为空。"""
    try:
        payload = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    raw = payload.get("canonical_evidence_ids") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(
        item
        for item in raw
        if (
            type(item) is str
            and len(item) == 67
            and item.startswith("ce_")
            and all(char in "0123456789abcdef" for char in item[3:])
        )
    ))


_MAX_NOTE_EVIDENCE_PROJECTION = 20


def _concept_source_set(evidence_ids: list[str]) -> tuple[str, str]:
    """把证据 ID 集合归一为可复算 JSON 和 fingerprint。"""
    if not isinstance(evidence_ids, list) or any(
        not isinstance(item, str) or not item.startswith("ce_")
        for item in evidence_ids
    ):
        raise ConceptEvidenceError("source evidence ids 必须是 canonical evidence ID 列表")
    canonical_ids = sorted(set(evidence_ids))
    payload = json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":"))
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _concept_definition_version_id(
    *, domain: str, term: str, version: int, input_hash: str | None, actor: str
) -> str:
    payload = json.dumps(
        {
            "domain": domain,
            "term": term,
            "version": version,
            "input_hash": input_hash,
            "actor": actor,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cdv_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_sha256(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} 必须是小写 sha256")
    return value


def _parse_dt(s: str | None) -> datetime | None:
    """解析 ISO 时间串为 aware-UTC。旧库里存的 naive 串补上 UTC tzinfo,
    避免与 aware 的 now() 相减时崩 'can't subtract offset-naive and offset-aware'。"""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    def __init__(self, db_path: Path | str):
        self._runtime = _DatabaseRuntime(
            db_path,
            schema_version=SCHEMA_VERSION,
            migration_steps=migration_steps,
            probe_schema_version=_probe_schema_version_without_sqlite,
        )

    @property
    def _path(self) -> Path:
        return self._runtime.path

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._runtime.connection

    @property
    def _lock(self):
        return self._runtime.lock

    @_lock.setter
    def _lock(self, value) -> None:
        # 故障注入测试会替换锁；runtime 仍是唯一状态持有者。
        self._runtime.lock = value

    def init_schema(self) -> None:
        self._runtime.init_schema(self)

    def _migration_steps(self) -> tuple[Migration, ...]:
        return self._runtime.migration_steps()

    def _has_user_schema(self) -> bool:
        return self._runtime.has_user_schema()

    def _create_migration_backup(self, from_version: int) -> Path:
        """为非空库保留升级前一致快照，同版迁移重试时原子刷新。"""
        return self._runtime.create_migration_backup(from_version)

    def schema_version(self) -> int:
        """当前库的 schema 版本(PRAGMA user_version)。供备份兼容/未来迁移判断。"""
        return self._runtime.schema_version()

    def close(self) -> None:
        self._runtime.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # Job

    def create_job(self, job: Job) -> None:
        # lineage_key 缺省由 id 反推(去时间戳),保证同源快照归一组。
        return _DatabaseAggregates.create_job(
            self,
            job,
        )

    def get_job(self, job_id: str) -> Job | None:
        return _JobsReadRepository.get_job(self, job_id)

    def jobs_brief(self, job_ids: list[str]) -> dict[str, dict]:
        """批量取作业简要(队列 / worker 历史 enrich 用):
        {job_id: {title, content_type, domain, status, pipeline}}。pipeline 供运行中 task 解析 step→pool。
        一次 IN 查询避免 N+1;去重保序、跳空 id;SQLite 变量上限按 500 分批。"""
        return _JobsReadRepository.jobs_brief(self, job_ids)

    def list_jobs(
        self,
        status: str | None = None,
        collection_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        domain: str | None = None,
        source: str | None = None,
        uncategorized: bool = False,
        current_only: bool = True,
    ) -> tuple[int, list[Job]]:
        return _JobsReadRepository.list_jobs(
            self,
            status,
            collection_id,
            limit,
            offset,
            domain,
            source,
            uncategorized,
            current_only,
        )

    def lineage_versions(self, job_id: str) -> list[Job]:
        """同一 lineage(同源内容)的所有快照,按 created_at 倒序(供详情页历史版本跳转)。
        若该 job 无 lineage_key(旧库未回填)则只返它自己。"""
        return _JobsReadRepository.lineage_versions(self, job_id)

    def promote_lineage_current(self, lineage_key: str) -> None:
        """若某 lineage 当前无 current(如 current 被删),把剩余最新 created_at 的一版提为 current。
        幂等:已有 current 则不动。"""
        return _DatabaseAggregates.promote_lineage_current(
            self,
            lineage_key,
        )

    def lineage_counts(self, lineage_keys: list[str]) -> dict[str, int]:
        """批量取各 lineage 的快照总数(供列表「N 个历史版本」提示)。一次 IN 查询。"""
        return _JobsReadRepository.lineage_counts(self, lineage_keys)

    def count_jobs_by_status(self, collection_id: str | None = None) -> dict[str, int]:
        """一次 GROUP BY 取各状态计数(替代多次 list_jobs(limit=0) 的 COUNT+空 SELECT)。
        传 collection_id 则只统计该集合,供集合详情页 status_counts 用。"""
        return _JobsReadRepository.count_jobs_by_status(self, collection_id)

    def job_facets(self) -> dict[str, dict]:
        """全量 jobs 按 source / domain / status 的计数,供前端过滤 chip 显示(后端聚合,非客户端基于已加载)。"""
        return _JobsReadRepository.job_facets(self)

    def glossary_for_job(self, job_id: str, domain: str | None = None) -> list[dict]:
        """反查:occurrences 含该 job_id 的概念(LIKE 粗筛 + 精确过滤防子串误命中),
        供内容详情·概念 tab。rejected(已驳回)不返回。"""
        return _JobsReadRepository.glossary_for_job(self, job_id, domain)

    def update_job(self, job_id: str, **fields) -> None:
        return _DatabaseAggregates.update_job(
            self,
            job_id,
            **fields,
        )

    def _strip_occurrences_for_jobs(self, job_ids: list[str]) -> None:
        """从 glossary.occurrences 摘除指向这些 job 的出现(保留概念与定义)。
        调用方须已持锁且在同一事务内;本方法只 execute,不 commit。"""
        return _JobsRepository._strip_occurrences_for_jobs_in_tx(
            self,
            self._conn,
            job_ids,
        )

    def _detach_study_sources_locked(self, job_ids: list[str]) -> None:
        """删源前保留学习审计事实,调用方负责事务和锁."""
        return _JobsRepository._detach_study_sources_locked_in_tx(
            self,
            self._conn,
            job_ids,
        )

    def delete_job_cascade(
        self, job_id: str, collection_id: str | None = None, item_id: str | None = None
    ) -> None:
        """原子删 job:jobs 行 + FTS 索引 + ai_usage 行 + 集合计数 -1 + 摘除 glossary.occurrences 里的 job_id
        +(订阅 job)清 ingested_items 该条。全部单事务,避免两次 commit 间崩溃留孤儿。
        job_steps 经 FK ON DELETE CASCADE 连带删除。
        item_id:订阅来源 job 的去重键(从 job.meta['source_item_id'] 取);传了才清 ingested_items
        → 该条下轮订阅枚举可重新入库(彻底删除)。"""
        return _DatabaseAggregates.delete_job_cascade(
            self,
            job_id,
            collection_id,
            item_id,
        )

    # Step

    def upsert_step(self, step: Step) -> None:
        return self._runtime.run_transaction(
            self,
            _JobsRepository.upsert_step_in_tx,
            (step,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_steps(self, job_id: str) -> list[Step]:
        return _JobsRepository.get_steps(
            self,
            job_id,
        )

    def delete_step(self, job_id: str, step_name: str) -> None:
        """删单个步骤行(供 resubmit 对齐:删去当前 pipeline 不再有的步,避免 DB 残留旧步)。"""
        return self._runtime.run_transaction(
            self,
            _JobsRepository.delete_step_in_tx,
            (job_id, step_name),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def update_step(
        self, job_id: str, step_name: str, *, only_if_active: bool = False, **fields
    ) -> None:
        """更新步骤行。only_if_active=True 时仅在当前状态非终态(done/skipped)才写,
        防成功步被迟到的失败上报覆盖(done→failed 不一致)。"""
        return self._runtime.run_transaction(
            self,
            _JobsRepository.update_step_in_tx,
            (job_id, step_name),
            {"only_if_active": only_if_active, **fields},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    # Worker

    def upsert_worker(self, worker: Worker) -> None:
        # ON CONFLICT DO UPDATE 而非 INSERT OR REPLACE:REPLACE 是整行删重建,会把不在
        # 列清单里的中心配置列(desired_config/cfg_rev)清零——worker 每次重注册都会走到
        # 这里,页面下发的配置绝不能被重启冲掉。
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.upsert_worker_in_tx,
            (worker,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_worker(
        self,
        worker_id: str,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> Worker | None:
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.get_worker_in_tx,
            (worker_id, online_window_sec, stale_window_sec),
            {},
            begin_immediate=False,
            commit_on_success=False,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def list_workers(
        self,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> list[Worker]:
        """列出所有 worker,状态由后端按心跳新鲜度统一算出(online-idle/busy、
        offline、stale,paused 为管理员叠加)。越过 stale 窗口的持久化为信号,
        供 GC 回收僵尸 worker。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.list_workers_in_tx,
            (online_window_sec, stale_window_sec),
            {},
            begin_immediate=False,
            commit_on_success=False,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def _apply_status(
        self,
        w: Worker,
        online_window_sec: int,
        stale_window_sec: int,
        now: datetime | None = None,
    ) -> None:
        """把 worker 的存量字段折算成对外公共状态,并对 stale 持久化(不动心跳)。
        管理员叠加位(paused)来自独立的 admin_status 列;运行时 status 列只供 busy/idle + GC。"""
        return _WorkersRepository._apply_status(
            self,
            w,
            online_window_sec,
            stale_window_sec,
            now,
        )

    def set_worker_status(self, worker_id: str, status: str) -> None:
        """仅更新 worker 状态,不触碰 last_heartbeat(用于标记僵尸为 offline)。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.set_worker_status_in_tx,
            (worker_id, status),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def set_worker_admin_status(self, worker_id: str, admin_status: str) -> None:
        """仅更新管理员暂停叠加位("" / "paused"),不触碰运行时 status / 心跳。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.set_worker_admin_status_in_tx,
            (worker_id, admin_status),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def increment_worker_stats(
        self,
        worker_id: str,
        completed: int = 0,
        failed: int = 0,
        duration: float = 0.0,
    ) -> None:
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.increment_worker_stats_in_tx,
            (worker_id, completed, failed, duration),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def set_worker_desired_config(self, worker_id: str, config: dict) -> int:
        """写中心期望配置并 cfg_rev+1(单调);返回新 rev,worker 不存在返回 -1。
        config 只存显式指定的键(pools/concurrency/tags/reject_tags),worker 端按键应用。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.set_worker_desired_config_in_tx,
            (worker_id, config),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_worker_desired_config(self, worker_id: str) -> tuple[dict | None, int]:
        """读中心期望配置;(None, 0) = 未配置/worker 不存在(worker 端视为尊重自报)。"""
        return _WorkersRepository.get_worker_desired_config(
            self,
            worker_id,
        )

    def update_worker_heartbeat(
        self,
        worker_id: str,
        status: str | None = None,
        current_job: str | None = None,
        current_step: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        """刷新 worker 在 DB 中的 last_heartbeat(及可选的 status / 当前任务)。

        心跳与状态变更必须写回 DB,否则 /api/workers 读到的 last_heartbeat
        永远停在注册时刻,前端会在 30s 后把所有 worker 判成 offline。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.update_worker_heartbeat_in_tx,
            (worker_id, status, current_job, current_step, concurrency),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def delete_worker(self, worker_id: str) -> None:
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.delete_worker_in_tx,
            (worker_id,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def list_running_steps(self) -> list[Step]:
        """所有 status=running 的 step(= 正在执行的 task),按开始时间倒序。
        队列页「运行中」分组的权威来源:step 行自带 pool/worker_id/started_at,无需依赖 worker 心跳派生。"""
        return _WorkersRepository.list_running_steps(
            self,
        )

    def list_worker_tasks(self, worker_id: str, limit: int = 50) -> list[Step]:
        """该 worker 的 task 执行历史(task = 某作业的某步骤的一次执行,按最近开始时间倒序;每条 = 一个 step 记录)。"""
        return _WorkersRepository.list_worker_tasks(
            self,
            worker_id,
            limit,
        )

    # Worker Token

    def upsert_worker_token(
        self,
        token_hash: str,
        worker_id: str,
        pools: list[str],
        tags: list[str],
        created_at: datetime,
        revoked: bool = False,
        revoke_existing: bool = False,
    ) -> None:
        """登记一枚 per-worker token(仅存 sha256 hash),pools/tags 限定其授权范围。

        revoke_existing=True 用于首次 bootstrap/recreate,先吊销该 worker 旧 token,保证同一
        worker 同时只有一枚 active token。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.upsert_worker_token_in_tx,
            (token_hash, worker_id, pools, tags, created_at, revoked, revoke_existing),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_worker_token_by_hash(self, token_hash: str) -> dict | None:
        """按 token hash 查 token 行,未命中返回 None;revoked 折算成 bool。"""
        return _WorkersRepository.get_worker_token_by_hash(
            self,
            token_hash,
        )

    def revoke_worker_token(self, worker_id: str) -> None:
        """吊销某 worker 名下全部 token(删 worker 时连带,使其心跳/认领立即 401)。"""
        return self._runtime.run_transaction(
            self,
            _WorkersRepository.revoke_worker_token_in_tx,
            (worker_id,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def list_worker_tokens(self) -> list[dict]:
        return _WorkersRepository.list_worker_tokens(
            self,
        )

    # App Credentials

    def set_credential(self, key: str, value: str) -> None:
        """存/覆盖一条应用级凭证(如 B站 cookie JSON),按 key 幂等 upsert。

        设了 FLORI_SECRET_KEY 时以 Fernet token 加密落库;未设则存明文(向后兼容)
        并一次性告警(建议设 key 以 at-rest 加密)。"""
        return self._runtime.run_transaction(
            self,
            _CredentialsRepository.set_credential_in_tx,
            (key, value),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_credential(self, key: str) -> str | None:
        """读一条凭证值,未命中返回 None。

        有 Fernet key 时尝试解密;遇 InvalidToken(历史明文行,或换了 key 的旧 token)
        透传原始串(legacy passthrough)。无 key 则直接返回原始串。任何情况都不因坏值崩。"""
        return _CredentialsRepository.get_credential(
            self,
            key,
        )

    def delete_credential(self, key: str) -> None:
        """删一条凭证(如登出清除 B站 cookie)。"""
        return self._runtime.run_transaction(
            self,
            _CredentialsRepository.delete_credential_in_tx,
            (key,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    # AI Usage

    def record_ai_usage(self, usage: AIUsage) -> bool:
        return self._runtime.run_transaction(
            self,
            _TelemetryRepository.record_ai_usage_in_tx,
            (usage,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=False,
            rollback_on_error=False,
        )

    def record_ai_task_log(self, log: dict) -> bool:
        """落一条独立 AI task 的白盒审计(对应 DAG 的 output/ai_logs/{step}.jsonl;AI task 无 job_dir 故入库)。
        log = 索引列(task_id/exec_id/step_name/domain/provider/model/ok/error/各 token/cost/duration/num_turns)
        + record(全量审计 dict,存进 record_json)+ created_at。best-effort,不让审计失败影响主流程。"""
        return self._runtime.run_transaction(
            self,
            _TelemetryRepository.record_ai_task_log_in_tx,
            (log,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=False,
            rollback_on_error=False,
        )

    def get_ai_task_logs(self, task_id: str) -> list[dict]:
        """读某 AI task 的白盒审计(供查看端点);最近在前。"""
        return _TelemetryRepository.get_ai_task_logs(
            self,
            task_id,
        )

    def get_latest_ai_task_log(self, task_id: str) -> dict | None:
        """返回独立 AI task 最近一条持久审计,并解析 record 供 TTL 丢失恢复."""
        return _TelemetryRepository.get_latest_ai_task_log(
            self,
            task_id,
        )

    # Prompt Overrides

    @staticmethod
    def _norm_override_key(scope: str, domain: str | None) -> tuple[str, str]:
        """归一 (scope, domain):scope 非 'domain' 一律按 'global' 处理且 domain='';
        'domain' scope 须有非空 domain。返回 (scope, domain) 供主键统一(避免 NULL 破唯一)。"""
        return _PromptsRepository._norm_override_key(
            scope,
            domain,
        )

    def set_prompt_override(
        self,
        scope: str,
        domain: str | None,
        pipeline: str,
        step: str,
        content: str,
        mode: str = "overwrite",
        note: str | None = None,
    ) -> int:
        """存某步的 prompt 覆盖,带版本管理(类 Grafana save)。返回激活版本号。
        - 该 (scope,domain,pipeline,step) 此前无任何覆盖 → 首版 v1(mode 忽略)。
        - mode='overwrite'(默认)→ 更新当前激活版本历史行的 content(+note,留空则保留原 note),
          主表 content/version 不变(version 仍指激活版本)。
        - mode='new' → 新版本 version=max(历史)+1,历史表插一条,主表指向新版本(成为激活)。
        content 不做空判断(空判断/删除由上层 delete_prompt_override 负责)。"""
        return self._runtime.run_transaction(
            self,
            _PromptsRepository.set_prompt_override_in_tx,
            (scope, domain, pipeline, step, content, mode, note),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def list_prompt_override_versions(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> list[dict]:
        """该 (scope,domain,pipeline,step) 的全部历史版本元信息(不含 content),version 升序。"""
        return _PromptsRepository.list_prompt_override_versions(
            self,
            scope,
            domain,
            pipeline,
            step,
        )

    def get_prompt_override_version(
        self, scope: str, domain: str | None, pipeline: str, step: str, version: int
    ) -> dict | None:
        """读某历史版本(含 content),未命中返回 None。"""
        return _PromptsRepository.get_prompt_override_version(
            self,
            scope,
            domain,
            pipeline,
            step,
            version,
        )

    def delete_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> None:
        """删某步的 prompt 覆盖(恢复默认)——连同其全部历史版本一并删。无则 no-op。"""
        return self._runtime.run_transaction(
            self,
            _PromptsRepository.delete_prompt_override_in_tx,
            (scope, domain, pipeline, step),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def deactivate_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> None:
        """停用某步覆盖(恢复内置默认)——非破坏:只删主表 prompt_overrides 那一行(激活指针),
        prompt_override_versions 全部历史版本完整保留(下拉里仍能看到 v1/v2…,可重新激活)。
        删指针后 resolve_prompt_overrides 返回空 → 派发回内置默认。无指针则 no-op。
        注:version 列 NOT NULL 不可空,故用删激活行而非置 NULL 表达停用。"""
        return self._runtime.run_transaction(
            self,
            _PromptsRepository.deactivate_prompt_override_in_tx,
            (scope, domain, pipeline, step),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def set_active_prompt_version(
        self, scope: str, domain: str | None, pipeline: str, step: str, version: int
    ) -> bool:
        """把激活指针指向某历史版本(re-activate):主表 content/version 同步成该版本,
        下次派发即用它。该版本不存在于 prompt_override_versions → 返回 False(不动);成功 True。
        主表此前可能无行(已 deactivate 状态)——直接 INSERT OR REPLACE 重建激活指针。"""
        return self._runtime.run_transaction(
            self,
            _PromptsRepository.set_active_prompt_version_in_tx,
            (scope, domain, pipeline, step, version),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> dict | None:
        """读单条 prompt 覆盖,未命中返回 None。"""
        return _PromptsRepository.get_prompt_override(
            self,
            scope,
            domain,
            pipeline,
            step,
        )

    def list_prompt_overrides(self) -> list[dict]:
        """全量 prompt 覆盖(供设置页标记哪些步已有覆盖)。"""
        return _PromptsRepository.list_prompt_overrides(
            self,
        )

    def resolve_prompt_overrides(
        self, pipeline: str, domain: str | None
    ) -> dict[str, dict]:
        """派发注入用:给定 job 的 pipeline + domain,返回 {step: {content, version}} 解析结果。
        domain 覆盖优先于 global;同一步两者都有则取 domain(连同其版本号)。job 创建时(api 有 DB)
        调用,结果写 job.json.prompt_overrides 随 job 下发(含激活版本号快照),worker step_base 读取
        (pure worker 无 DB)。空 content 视为无覆盖被过滤。
        注:worker _injected_prompt_override 兼容 dict 与存量纯字符串两种 job.json 形态。"""
        return _PromptsRepository.resolve_prompt_overrides(
            self,
            pipeline,
            domain,
        )

    def get_usage_summary(
        self, job_id: str | None = None, since: str | None = None
    ) -> dict:
        return _TelemetryRepository.get_usage_summary(
            self,
            job_id,
            since,
        )

    def get_usage_aggregate(self) -> dict:
        """全量 AI 用量聚合(供 /api/usage + 系统状态展示):累计 token/缓存/成本 + 平均缓存命中率
        + 按 model 分。命中率 = cache_read /(input + cache_read + cache_creation)。"""
        return _TelemetryRepository.get_usage_aggregate(
            self,
        )

    def list_usage_by_job(self, job_id: str) -> list[dict]:
        """该 job 的逐次 AI 调用明细(供 job 详情按步展示:in/out/cache/命中率/cost/耗时/轮数/worker)。
        命中率 = cache_read /(input + cache_read + cache_creation)。"""
        return _TelemetryRepository.list_usage_by_job(
            self,
            job_id,
        )

    def throughput_since(self, since_iso: str) -> dict:
        """近窗口吞吐:since_iso 之后进入终态的 job 计数(done/failed)。用 updated_at 近似终态时刻,
        rerun 改 updated_at 会重复计入但属罕见;利用 idx_jobs_status。"""
        return _TelemetryRepository.throughput_since(
            self,
            since_iso,
        )

    # Collection

    def _row_to_collection(self, r: sqlite3.Row) -> Collection:
        return _DatabaseRowMappersExtra._row_to_collection(
            self,
            r,
        )

    def create_collection(self, collection: Collection) -> None:
        return self._runtime.run_transaction(
            self,
            _CollectionsRepository.create_collection_in_tx,
            (collection,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_collection(self, collection_id: str) -> Collection | None:
        return _CollectionsRepository.get_collection(
            self,
            collection_id,
        )

    def list_collections(self, domain: str | None = None) -> list[Collection]:
        return _CollectionsRepository.list_collections(
            self,
            domain,
        )

    def find_collection_by_source(self, source_type: str, source_id: str) -> Collection | None:
        """按来源找订阅集合(建订阅前去重;一个来源全局唯一对应一个订阅集合)。"""
        return _CollectionsRepository.find_collection_by_source(
            self,
            source_type,
            source_id,
        )

    def list_subscription_collections(self, enabled_only: bool = False) -> list[Collection]:
        """订阅集合(source_type 非空);enabled_only 时仅自动追更开启的。周期同步用。"""
        return _CollectionsRepository.list_subscription_collections(
            self,
            enabled_only,
        )

    def update_collection(
        self,
        collection_id: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        sync_enabled: bool | None = None,
    ) -> None:
        """更新集合可变字段(name/description/tags/订阅自动追更开关),None 表示不动。"""
        return self._runtime.run_transaction(
            self,
            _CollectionsRepository.update_collection_in_tx,
            (collection_id, name, description, tags, sync_enabled),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def delete_collection(self, collection_id: str, purge: bool = False) -> None:
        """删集合两模式。默认解绑:名下 job 的 collection_id 置 NULL(保留 job)。
        purge=True:连名下 job 一起删(jobs 行 + FTS 行 + 摘除各 job 的 glossary.occurrences;
        注:产物/MinIO 清理走既有 job 删除路径)。
        两种都清该集合 ingested_items(便于重订阅重新入库)。FTS 索引行同步处理,避免悬空行。"""
        return _DatabaseAggregates.delete_collection(
            self,
            collection_id,
            purge,
        )

    def mark_collection_synced(self, collection_id: str, dt: datetime) -> None:
        """订阅集合同步成功后记录 last_synced_at,并置 last_sync_status=ok、清除错误。"""
        return self._runtime.run_transaction(
            self,
            _CollectionsRepository.mark_collection_synced_in_tx,
            (collection_id, dt),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def set_sync_status(
        self, collection_id: str, status: str | None, error: str | None = None
    ) -> None:
        """更新订阅集合的同步状态(syncing/ok/error/None)。error 仅 status=error 时存,其余清空。"""
        return self._runtime.run_transaction(
            self,
            _CollectionsRepository.set_sync_status_in_tx,
            (collection_id, status, error),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def domain_exists(self, domain: str) -> bool:
        """领域键是否已被使用(jobs/collections/glossary 任一有行)。用于 rename 防撞。"""
        return _CollectionsRepository.domain_exists(
            self,
            domain,
        )

    def rename_domain(self, old: str, new: str) -> dict[str, int]:
        """把领域键 old 原子改成 new(领域是派生键,散在 jobs/collections/glossary + notes_fts5 冗余列)。
        一个事务内迁移所有引用,任一失败回滚。返回各表迁移行数。调用方须先校验 new 合法且不冲突。"""
        return _DatabaseAggregates.rename_domain(
            self,
            old,
            new,
        )

    # Domain(领域是派生视图:来自 jobs ∪ collections ∪ glossary 的 distinct domain)

    def list_domains(self) -> list[dict]:
        """领域总览:每个 domain 的 集合数/内容数/概念数/订阅数/最近活跃(派生,无 domains 表)。"""
        return _CollectionsRepository.list_domains(
            self,
        )

    def domain_top_terms(self, domain: str, limit: int = 30) -> list[dict]:
        """领域工作台语义栏:该 domain 的术语(含候选 suggested,各带 status;rejected 除外),
        按来源数(佐证强度代理)降序。候选数另由 suggested_count 单独提示;前端可按 status 区分展示。"""
        return _CollectionsRepository.domain_top_terms(
            self,
            domain,
            limit,
        )

    def concept_timeline(self, domain: str, granularity: str = "month") -> dict:
        """概念时间线:把该 domain 各概念的 occurrences 经 job_id→源内容发布时间映射,按粒度分桶计数。
        分桶时间用 COALESCE(published_at, created_at):优先源内容在平台的发布/更新时间("这个概念
        在世界上何时出现"),无已知发布时间的 job 回退入库时间(created_at),不丢计数。
        granularity: day(YYYY-MM-DD) / week(YYYY-Www) / month(YYYY-MM)。无 glossary/job 时返回空。"""
        return _CollectionsRepository.concept_timeline(
            self,
            domain,
            granularity,
        )

    def concept_occurrence_dates(self, domain: str) -> dict[str, list[str]]:
        """概念趋势雷达基础数据:该 domain 各概念的每条 occurrence 经 job_id→源内容时间映射,
        返回 {term: [iso_date, ...]}(每个 occurrence 一个时间点,可重复)。时间口径与 concept_timeline
        一致:COALESCE(published_at, created_at)("这个概念在世界上何时出现",无发布时间回退入库时间)。
        无映射到时间的 occurrence 略过(不计入)。供 radar 服务按窗口切片算飙升/新出现,纯数据无业务策略。"""
        return _CollectionsRepository.concept_occurrence_dates(
            self,
            domain,
        )

    def domain_topics(self, domain: str) -> list[dict]:
        """领域内主题(可浏览标签) = 该 domain 所有 job 的 style_tags distinct + 计数。"""
        return _CollectionsRepository.domain_topics(
            self,
            domain,
        )

    def ingested_bvids(self) -> set[str]:
        """已入库的 B站 BV 号集合(从 jobs.url 提取),供订阅同步去重。
        通用去重走 ingested_items 表(见 ingested_item_ids/mark_ingested),按
        (collection_id, item_id) 去重;本方法只作存量 bili 数据的兜底回填——同步首跑时
        可把它的结果并入某集合的 ingested 集合,避免已入库的 B站视频被重复建 job。"""
        return _CollectionsRepository.ingested_bvids(
            self,
        )

    def ingested_item_ids(self, collection_id: str) -> set[str]:
        """某集合(订阅)已入库过的 item_id 集合,供 source-adapter 通用去重。
        item_id 含义随来源而定(B站=bvid、youtube=videoId、rss=entry id 等)。"""
        return _CollectionsRepository.ingested_item_ids(
            self,
            collection_id,
        )

    def mark_ingested(self, collection_id: str, item_id: str) -> None:
        """登记某集合已入库 item_id(幂等:重复 mark 不报错),同步成功后调。"""
        return self._runtime.run_transaction(
            self,
            _CollectionsRepository.mark_ingested_in_tx,
            (collection_id, item_id),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def increment_collection_count(self, collection_id: str, delta: int) -> None:
        """维护集合的 job_count:建/删 job 时增减;负值不下穿 0。"""
        return self._runtime.run_transaction(
            self,
            _CollectionsRepository.increment_collection_count_in_tx,
            (collection_id, delta),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    # Glossary

    def _validate_concept_source_evidence_locked(
        self,
        *,
        domain: str,
        term: str,
        evidence_ids: list[str],
    ) -> tuple[str, str]:
        """验证定义来源都已精确挂到该概念，调用方负责事务与锁。"""
        return _ConceptsRepository._validate_concept_source_evidence_locked_in_tx(
            self,
            self._conn,
            domain=domain,
            term=term,
            evidence_ids=evidence_ids,
        )

    def _definition_row_locked(self, definition_version_id: str | None) -> sqlite3.Row | None:
        return _ConceptsRepository._definition_row_locked_in_tx(
            self,
            self._conn,
            definition_version_id,
        )

    def _insert_definition_version_locked(
        self,
        *,
        domain: str,
        term: str,
        definition: str,
        source_evidence_ids_json: str,
        source_set_fingerprint: str,
        strategy: str,
        provider: str | None,
        model: str | None,
        prompt_hash: str | None,
        input_hash: str | None,
        supersedes_version_id: str | None,
        actor: str,
        created_at: str,
    ) -> sqlite3.Row:
        """只追加 definition history；调用方同事务切 current pointer。"""
        return _ConceptsRepository._insert_definition_version_locked_in_tx(
            self,
            self._conn,
            domain=domain,
            term=term,
            definition=definition,
            source_evidence_ids_json=source_evidence_ids_json,
            source_set_fingerprint=source_set_fingerprint,
            strategy=strategy,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            input_hash=input_hash,
            supersedes_version_id=supersedes_version_id,
            actor=actor,
            created_at=created_at,
        )

    def _create_initial_definition_locked(
        self,
        *,
        domain: str,
        term: str,
        definition: str,
        strategy: str,
        actor: str,
        created_at: str,
    ) -> sqlite3.Row:
        """为新建或曾删除后重建的概念追加首个 current version。"""
        return _ConceptsRepository._create_initial_definition_locked_in_tx(
            self,
            self._conn,
            domain=domain,
            term=term,
            definition=definition,
            strategy=strategy,
            actor=actor,
            created_at=created_at,
        )

    def upsert_concept_occurrence(
        self,
        *,
        domain: str,
        term: str,
        job_id: str,
        evidence_id: str,
    ) -> bool:
        """精确绑定 concept/job/evidence；重复 completion 返回 False。"""
        return _DatabaseAggregates.upsert_concept_occurrence(
            self,
            domain=domain,
            term=term,
            job_id=job_id,
            evidence_id=evidence_id,
        )

    def replace_concept_occurrences_for_job(
        self,
        *,
        domain: str,
        term: str,
        job_id: str,
        evidence_ids: list[str],
    ) -> bool:
        """原子替换单 concept/job 的证据集合；完全相同返回 False。"""
        return _DatabaseAggregates.replace_concept_occurrences_for_job(
            self,
            domain=domain,
            term=term,
            job_id=job_id,
            evidence_ids=evidence_ids,
        )

    def replace_job_concept_occurrences(
        self,
        *,
        domain: str,
        job_id: str,
        mapping: dict[str, list[str]],
    ) -> bool:
        """原子对账一个 job 的全部 concept/evidence 映射，移除消失概念。"""
        return _DatabaseAggregates.replace_job_concept_occurrences(
            self,
            domain=domain,
            job_id=job_id,
            mapping=mapping,
        )

    def list_concept_occurrences(
        self,
        domain: str,
        term: str,
        *,
        include_invalid: bool = False,
    ) -> list[dict]:
        """返回正规化 occurrence；默认排除 stale/missing canonical evidence。"""
        return _ConceptsRepository.list_concept_occurrences(
            self,
            domain,
            term,
            include_invalid=include_invalid,
        )

    def remove_concept_occurrence(
        self,
        *,
        domain: str,
        term: str,
        job_id: str,
        evidence_id: str,
    ) -> bool:
        """只删除指定四元组，不影响同 job 的其他证据。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.remove_concept_occurrence_in_tx,
            (),
            {"domain": domain, "term": term, "job_id": job_id, "evidence_id": evidence_id},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def append_concept_definition_version(
        self,
        *,
        domain: str,
        term: str,
        definition: str,
        evidence_ids: list[str],
        strategy: str,
        actor: str,
        expected_current_version_id: str,
        expected_lock_revision: int,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        input_hash: str | None = None,
        allow_locked: bool = False,
        allow_same_source_set: bool = False,
    ) -> dict:
        """append + current pointer CAS；source set 未变时默认幂等 no-op。"""
        return _DatabaseAggregates.append_concept_definition_version(
            self,
            domain=domain,
            term=term,
            definition=definition,
            evidence_ids=evidence_ids,
            strategy=strategy,
            actor=actor,
            expected_current_version_id=expected_current_version_id,
            expected_lock_revision=expected_lock_revision,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            input_hash=input_hash,
            allow_locked=allow_locked,
            allow_same_source_set=allow_same_source_set,
        )

    def current_concept_definition(self, domain: str, term: str) -> dict | None:
        return _ConceptsRepository.current_concept_definition(
            self,
            domain,
            term,
        )

    def list_concept_definition_versions(
        self,
        domain: str,
        term: str,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        return _ConceptsRepository.list_concept_definition_versions(
            self,
            domain,
            term,
            limit=limit,
        )

    def count_concept_definition_versions(self, domain: str, term: str) -> int:
        return _ConceptsRepository.count_concept_definition_versions(
            self,
            domain,
            term,
        )

    def set_concept_definition_lock(
        self,
        *,
        domain: str,
        term: str,
        locked: bool,
        expected_current_version_id: str,
        expected_lock_revision: int,
    ) -> dict:
        """以 current version + lock revision 做 lock/unlock CAS。"""
        return _DatabaseAggregates.set_concept_definition_lock(
            self,
            domain=domain,
            term=term,
            locked=locked,
            expected_current_version_id=expected_current_version_id,
            expected_lock_revision=expected_lock_revision,
        )

    def update_glossary_definition_cas(
        self,
        *,
        domain: str,
        term: str,
        definition: str | None,
        related: list | None,
        expected_current_version_id: str | None,
        expected_lock_revision: int | None,
        actor: str,
    ) -> dict:
        """人工定义与 related 在同一事务追加版本并 CAS 切换。"""
        return _DatabaseAggregates.update_glossary_definition_cas(
            self,
            domain=domain,
            term=term,
            definition=definition,
            related=related,
            expected_current_version_id=expected_current_version_id,
            expected_lock_revision=expected_lock_revision,
            actor=actor,
        )

    def upsert_glossary_term(
        self,
        domain: str,
        term: str,
        definition: str = "",
        related: list | None = None,
        status: str = "accepted",
        *,
        create_only: bool = False,
    ) -> None:
        """写入/覆盖一条术语(手动维护入口):按 (domain, term) 幂等 upsert,
        保留已有 occurrences,覆盖 definition/related/status。
        related 元素可为字符串或 {term, rel},落库前归一为 [{term, rel}]。"""
        return _DatabaseAggregates.upsert_glossary_term(
            self,
            domain,
            term,
            definition,
            related,
            status,
            create_only=create_only,
        )

    def add_glossary_suggestion(
        self,
        domain: str,
        term: str,
        job_id: str,
        content_type: str = "",
        location: str | None = None,
        definition: str = "",
        zh_name: str = "",
    ) -> None:
        """采集候选概念(resolve-then-merge):先按 (domain, term) 精确匹配,
        再经 shared.concepts.resolve 用归一键撞现有实体的 term/zh_name/aliases——
        「量化(Quantization)」「多头注意力」等变体挂到既有实体(occurrence 按 job_id 去重,
        新变体名进 aliases),而不是各建一条。都未命中才新建(主名规则见 primary_fields:
        英文为 term、中文进 zh_name)。定义/译名仅补空不覆盖,绝不降级已 accepted 的条目。
        生命周期:命中 rejected 实体 → 整条跳过(驳回后不再重复建议);suggested 实体
        的 occurrence 覆盖 ≥2 个不同 job → 自动晋升 accepted。"""
        return _DatabaseAggregates.add_glossary_suggestion(
            self,
            domain,
            term,
            job_id,
            content_type,
            location,
            definition,
            zh_name,
        )

    _STATUS_RANK = {"accepted": 2, "suggested": 1, "rejected": 0}

    def merge_glossary_terms(self, domain: str, src_term: str, dst_term: str) -> dict:
        """把 src 实体并入 dst,供存量清洗与前端"合并到已有词条"共用:
        occurrences 并集按 job_id 去重(dst 先)、definition 取更长者、zh_name 补空、
        src 的 term/zh_name/aliases 全部入 dst.aliases(可逆留痕)、status 取更高档
        (accepted > suggested > rejected)、is_topic/definition_locked 取或、related 并集。
        然后删 src 行。任一行不存在或 src==dst 抛 ValueError。返回合并后的行 dict。"""
        return _DatabaseAggregates.merge_glossary_terms(
            self,
            domain,
            src_term,
            dst_term,
        )

    def add_glossary_relations(self, domain: str, term: str, relations: list[dict]) -> int:
        """给该概念并入关系边,供采集链与补边脚本共用:按目标 term 去重(先到先得,
        不覆盖已有 rel),自指跳过。行不存在返回 0(调用方应先 resolve 到主名)。返回新增边数。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.add_glossary_relations_in_tx,
            (domain, term, relations),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def glossary_term_rows(self, domain: str) -> list[dict]:
        """术语一致性 L1 导出用:该域词条的 (term, zh_name, definition, aliases) 轻量行。
        rejected 不导出(驳回件不该再注入翻译);aliases 供导出层把英文别名也映射到同一译名。"""
        return _ConceptsRepository.glossary_term_rows(
            self,
            domain,
        )

    def set_glossary_zh_name(self, domain: str, term: str, zh_name: str) -> bool:
        """backfill/人工定准写译名;返回是否更新(不存在的词条返回 False)。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.set_glossary_zh_name_in_tx,
            (domain, term, zh_name),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def get_glossary_term(self, domain: str, term: str) -> dict | None:
        """读单条术语,未命中返回 None。"""
        return _ConceptsRepository.get_glossary_term(
            self,
            domain,
            term,
        )

    def list_glossary(
        self, domain: str | None = None, status: str | None = None,
        q: str | None = None,
    ) -> list[dict]:
        """列术语,可按 domain / status 过滤 + q 检索(term/zh_name/aliases 子串,
        大小写不敏感),按 term 升序。status 未指定时默认排除 rejected。驳回件
        只在显式 status='rejected' 时可见)。"""
        return _ConceptsRepository.list_glossary(
            self,
            domain,
            status,
            q,
        )

    def get_job_titles(self, job_ids: list[str]) -> dict[str, str]:
        """批量取 job 标题(概念详情出现处 enrich 用):{job_id: title},缺 title 的 job 不返回。"""
        return _ConceptsRepository.get_job_titles(
            self,
            job_ids,
        )

    def accept_glossary_term(self, domain: str, term: str) -> None:
        """采纳候选术语:status -> 'accepted'。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.accept_glossary_term_in_tx,
            (domain, term),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def reject_glossary_term(self, domain: str, term: str) -> bool:
        """驳回概念:status -> 'rejected'。行保留——采集链 resolve 命中 rejected 直接
        跳过,同名/变体不会再被重复建议;各消费面(列表/图谱/雷达/term_map)默认排除。
        命中返回 True,无该行返回 False(供路由判 404)。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.reject_glossary_term_in_tx,
            (domain, term),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def set_glossary_watched(self, domain: str, term: str, watched: bool) -> bool:
        """置概念 watch 标记。命中返回 True,无该行返回 False(供路由判 404)。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.set_glossary_watched_in_tx,
            (domain, term, watched),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def set_glossary_topic(self, domain: str, term: str, is_topic: bool) -> bool:
        """置该词 is_topic(主题概念标记)。命中返回 True,无该行返回 False(供路由判 404)。"""
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.set_glossary_topic_in_tx,
            (domain, term, is_topic),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    def list_topic_concepts(self, domain: str) -> list[dict]:
        """该 domain 中标为主题概念(is_topic=1,rejected 除外)的列表,按出现数降序;
        每项含 term/definition/occurrence_count/related/is_topic。空则 []。"""
        return _ConceptsRepository.list_topic_concepts(
            self,
            domain,
        )

    def delete_glossary_term(self, domain: str, term: str) -> None:
        return self._runtime.run_transaction(
            self,
            _ConceptsRepository.delete_glossary_term_in_tx,
            (domain, term),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )

    # Notes 全文索引 (FTS5)

    def list_unindexed_done_jobs(self, limit: int = 100) -> list[Job]:
        """返回尚无任何全文索引的当前已完成 job,供 scheduler 幂等补账。"""
        return _SearchRepository.list_unindexed_done_jobs(
            self,
            limit,
        )

    def index_job_notes(
        self,
        job_id: str,
        note_type: str,
        title: str,
        body: str,
        content_type: str = "",
        domain: str = "",
        collection_id: str = "",
        supersede_note_types: list[str] | None = None,
        canonical_evidence: list[dict] | None = None,
    ) -> None:
        """原子替换某 job/note_type 的全文与证据块索引,失败时保留旧版本。"""
        return _DatabaseAggregates.index_job_notes(
            self,
            job_id,
            note_type,
            title,
            body,
            content_type,
            domain,
            collection_id,
            supersede_note_types,
            canonical_evidence,
        )

    def _replace_note_chunks_locked(
        self,
        *,
        job_id: str,
        note_type: str,
        title: str,
        body: str,
        content_type: str = "",
        domain: str = "",
        collection_id: str = "",
    ) -> None:
        """重建某 job/note_type 的证据块索引。调用方须已持锁,并负责 commit。"""
        return _SearchRepository._replace_note_chunks_locked_in_tx(
            self,
            self._conn,
            job_id=job_id,
            note_type=note_type,
            title=title,
            body=body,
            content_type=content_type,
            domain=domain,
            collection_id=collection_id,
        )

    def _replace_canonical_evidence_locked(
        self,
        *,
        job_id: str,
        note_type: str,
        records: list[dict],
    ) -> None:
        """原子替换当前证据集合；旧 ID 留存并失效，不随 chunk 删除。"""
        return _SearchRepository._replace_canonical_evidence_locked_in_tx(
            self,
            self._conn,
            job_id=job_id,
            note_type=note_type,
            records=records,
        )

    def canonical_evidence_database_states(
        self,
        evidence_ids: list[str],
    ) -> dict[str, dict]:
        """批量重算 DB 侧有效性；文件 SHA 由 resolver 在同一批次继续验证。"""
        return _SearchRepository.canonical_evidence_database_states(
            self,
            evidence_ids,
        )

    def canonical_evidence_ids_for_job(
        self,
        job_id: str,
        note_type: str | None = None,
    ) -> list[str]:
        """从当前 chunk 快照返回稳定 ID；失效 ID 仍交 resolver 显式投影。"""
        return _SearchRepository.canonical_evidence_ids_for_job(
            self,
            job_id,
            note_type,
        )

    def canonical_evidence_ids_for_source_segments(
        self,
        *,
        job_id: str,
        note_type: str,
        source_segment_ids: list[str],
    ) -> dict[str, list[str]]:
        """把 source segment 映到当前 note snapshot 的 canonical IDs。"""
        return _SearchRepository.canonical_evidence_ids_for_source_segments(
            self,
            job_id=job_id,
            note_type=note_type,
            source_segment_ids=source_segment_ids,
        )

    def canonical_evidence_ids_for_notes(
        self, refs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[str]]:
        """批量返回检索笔记的当前证据 ID，避免按结果逐条查询。"""
        return _SearchRepository.canonical_evidence_ids_for_notes(
            self,
            refs,
        )

    def set_canonical_evidence_states(self, states: list[dict]) -> None:
        """原子落下 resolver 结论；状态不参与 ID，可随当前文件事实变化。"""
        return self._runtime.run_transaction(
            self,
            _SearchRepository.set_canonical_evidence_states_in_tx,
            (states,),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=True,
        )

    @staticmethod
    def _row_to_canonical_evidence(row: sqlite3.Row) -> dict:
        return _DatabaseRowMappersExtra._row_to_canonical_evidence(
            row,
        )

    def search_notes(
        self,
        q: str,
        collection_id: str | None = None,
        domain: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """全文检索笔记;2 字 CJK 用参数化 instr,3+ 字符用 FTS5。"""
        return _SearchRepository.search_notes(
            self,
            q,
            collection_id,
            domain,
            content_type,
            limit,
            offset,
        )

    def search_note_chunks(
        self,
        q: str,
        collection_id: str | None = None,
        domain: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """全文检索问答证据块;2 字 CJK 兼容路径与公开 filter 语义一致。"""
        return _SearchRepository.search_note_chunks(
            self,
            q,
            collection_id,
            domain,
            content_type,
            limit,
            offset,
        )

    # Evidence-backed study suggestions

    def create_study_suggestion_batch(
        self,
        *,
        request_id: str,
        domain: str,
        job_ids: list[str] | None = None,
        concept_terms: list[str] | None = None,
        max_cards: int = 10,
        provider: str = DEFAULT_AI_PROVIDER,
        model: str = DEFAULT_AI_MODEL,
        prompt_snapshot: dict[str, object] | None = None,
        deadline_seconds: int = 1_800,
    ) -> dict:
        """在一个快照事务中固化候选的 chunk 和 concept 输入."""
        return _DatabaseAggregates.create_study_suggestion_batch(
            self,
            request_id=request_id,
            domain=domain,
            job_ids=job_ids,
            concept_terms=concept_terms,
            max_cards=max_cards,
            provider=provider,
            model=model,
            prompt_snapshot=prompt_snapshot,
            deadline_seconds=deadline_seconds,
        )

    def get_study_suggestion_batch(self, batch_id: str) -> dict | None:
        return _StudyRepository.get_study_suggestion_batch(
            self,
            batch_id,
        )

    def list_study_suggestion_batches_for_reconcile(
        self,
        *,
        statuses: tuple[str, ...] = ("pending_enqueue", "queued"),
        limit: int = 200,
    ) -> list[dict]:
        """按持久状态列出待投递/收割批次,供任意 Scheduler 副本幂等对账."""
        return _StudyRepository.list_study_suggestion_batches_for_reconcile(
            self,
            statuses=statuses,
            limit=limit,
        )

    def mark_study_suggestion_batch_queued(
        self,
        batch_id: str,
        *,
        task_id: str,
        expected_revision: int,
    ) -> dict:
        return _DatabaseAggregates.mark_study_suggestion_batch_queued(
            self,
            batch_id,
            task_id=task_id,
            expected_revision=expected_revision,
        )

    def fail_study_suggestion_batch(
        self,
        batch_id: str,
        *,
        task_id: str,
        expected_revision: int,
        error_code: str,
        error_message: str,
    ) -> dict:
        return _DatabaseAggregates.fail_study_suggestion_batch(
            self,
            batch_id,
            task_id=task_id,
            expected_revision=expected_revision,
            error_code=error_code,
            error_message=error_message,
        )

    def retry_study_suggestion_batch(
        self,
        batch_id: str,
        *,
        request_id: str,
        expected_revision: int,
        deadline_seconds: int = 1_800,
    ) -> dict:
        return _DatabaseAggregates.retry_study_suggestion_batch(
            self,
            batch_id,
            request_id=request_id,
            expected_revision=expected_revision,
            deadline_seconds=deadline_seconds,
        )

    def materialize_study_suggestions(
        self,
        batch_id: str,
        *,
        task_id: str,
        result: object,
    ) -> list[dict]:
        """严格校验 AI 输出并原子物化整批候选."""
        return _DatabaseAggregates.materialize_study_suggestions(
            self,
            batch_id,
            task_id=task_id,
            result=result,
        )

    def list_study_suggestions(
        self,
        *,
        batch_id: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        return _StudyRepository.list_study_suggestions(
            self,
            batch_id=batch_id,
            domain=domain,
            status=status,
            limit=limit,
            offset=offset,
        )

    def get_study_suggestion(self, suggestion_id: str) -> dict | None:
        return _StudyRepository.get_study_suggestion(
            self,
            suggestion_id,
        )

    def apply_study_suggestion_operations(
        self,
        *,
        request_id: str,
        batch_id: str,
        items: object,
        fault_injector: StudySuggestionFaultInjector | None = None,
    ) -> dict:
        """在一个 IMMEDIATE 事务中编辑,接受或拒绝最多 100 项."""
        return _DatabaseAggregates.apply_study_suggestion_operations(
            self,
            request_id=request_id,
            batch_id=batch_id,
            items=items,
            fault_injector=fault_injector,
        )

    def get_study_mastery(self, *, domain: str | None = None) -> dict:
        """按每卡最后一次真实评分聚合 canonical concept 掌握度."""
        return _StudyRepository.get_study_mastery(
            self,
            domain=domain,
        )

    # Study cards / SRS

    def create_study_card(
        self,
        *,
        card_id: str,
        domain: str,
        front: str,
        back: str,
        explanation: str = "",
        card_type: str = "basic",
        job_id: str | None = None,
        concept_term: str | None = None,
        evidence: object | None = None,
        status: str = "active",
        source: str = "manual",
        due_at: datetime | str | None = None,
    ) -> dict:
        """创建学习卡片。active 卡片同步初始化复习状态,使新卡立即进入 due 队列。"""
        return _DatabaseAggregates.create_study_card(
            self,
            card_id=card_id,
            domain=domain,
            front=front,
            back=back,
            explanation=explanation,
            card_type=card_type,
            job_id=job_id,
            concept_term=concept_term,
            evidence=evidence,
            status=status,
            source=source,
            due_at=due_at,
        )

    def get_study_card(self, card_id: str) -> dict | None:
        return _StudyRepository.get_study_card(
            self,
            card_id,
        )

    def list_study_cards(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        return _StudyRepository.list_study_cards(
            self,
            domain=domain,
            status=status,
            q=q,
            limit=limit,
            offset=offset,
        )

    def list_due_study_cards(
        self,
        *,
        domain: str | None = None,
        now: datetime | str | None = None,
        now_iso: str | None = None,
        limit: int = 50,
    ) -> tuple[int, list[dict]]:
        return _StudyRepository.list_due_study_cards(
            self,
            domain=domain,
            now=now,
            now_iso=now_iso,
            limit=limit,
        )

    def set_study_card_status(
        self,
        card_id: str,
        status: str,
        *,
        expected_revision: int,
    ) -> dict:
        return _DatabaseAggregates.set_study_card_status(
            self,
            card_id,
            status,
            expected_revision=expected_revision,
        )

    def delete_study_card(self, card_id: str) -> bool:
        return _DatabaseAggregates.delete_study_card(
            self,
            card_id,
        )

    def record_study_review(
        self,
        *,
        request_id: str,
        card_id: str,
        grade: str,
        expected_revision: int,
        response_ms: int | None = None,
        reviewed_at: datetime | str | None = None,
        fault_injector: StudyFaultInjector | None = None,
    ) -> dict:
        """在一个 IMMEDIATE 事务内完成幂等检查,CAS,调度和日志."""
        return _DatabaseAggregates.record_study_review(
            self,
            request_id=request_id,
            card_id=card_id,
            grade=grade,
            expected_revision=expected_revision,
            response_ms=response_ms,
            reviewed_at=reviewed_at,
            fault_injector=fault_injector,
        )

    def get_study_stats(
        self,
        *,
        domain: str | None = None,
        now: datetime | str | None = None,
    ) -> dict:
        """单次 CTE 从已提交事实聚合卡片,到期,评分和留存统计."""
        return _StudyRepository.get_study_stats(
            self,
            domain=domain,
            now=now,
        )

    # Private

    def _study_suggestion_monotonic_now_locked(
        self,
        batch_ids: list[str],
        wall_time: datetime | str,
    ) -> datetime:
        """在持有写事务时把墙钟钳制到整本建议账本的全局前态之后."""
        return _StudyRepository._study_suggestion_monotonic_now_locked_in_tx(
            self,
            self._conn,
            batch_ids,
            wall_time,
        )

    @staticmethod
    def _study_suggestion_lifecycle_operation_payload(
        *,
        operation_kind: str,
        batch_id: str,
        task_id: str,
        attempt: int,
        expected_revision: int,
        details: dict[str, object] | None = None,
    ) -> tuple[str, str, str]:
        """为一次 batch 状态迁移生成稳定幂等键和 canonical request."""
        return _StudyRepository._study_suggestion_lifecycle_operation_payload(
            operation_kind=operation_kind,
            batch_id=batch_id,
            task_id=task_id,
            attempt=attempt,
            expected_revision=expected_revision,
            details=details,
        )

    def _study_suggestion_lifecycle_replay_matches_current(
        self,
        *,
        request_id: str,
        batch_id: str,
        replay: dict | None,
        current: dict,
    ) -> bool:
        """从 lifecycle outcome 继续重放 identity 变化后核对 current row."""
        return _StudyRepository._study_suggestion_lifecycle_replay_matches_current_in_tx(
            self,
            self._conn,
            request_id=request_id,
            batch_id=batch_id,
            replay=replay,
            current=current,
        )

    def _study_suggestion_operation_replay_locked(
        self,
        request_id: str,
        request_fingerprint: str,
    ) -> dict | None:
        return _StudyRepository._study_suggestion_operation_replay_locked_in_tx(
            self,
            self._conn,
            request_id,
            request_fingerprint,
        )

    def _insert_study_suggestion_operation_locked(
        self,
        *,
        request_id: str,
        request_fingerprint: str,
        operation_kind: str,
        batch_id: str,
        request_json: str,
        outcome: dict,
        created_at: str,
    ) -> None:
        return _StudyRepository._insert_study_suggestion_operation_locked_in_tx(
            self,
            self._conn,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            operation_kind=operation_kind,
            batch_id=batch_id,
            request_json=request_json,
            outcome=outcome,
            created_at=created_at,
        )

    def _record_study_identity_transition_locked(
        self,
        *,
        batch_ids: list[str],
        transition_kind: str,
        source_domain: str,
        target_domain: str,
        source_concept: str | None,
        target_concept: str | None,
        created_at: str,
        impacts: dict[str, dict[str, list[str]]],
    ) -> None:
        """把 canonical identity 变化写入既有不可变操作账本."""
        return _StudyRepository._record_study_identity_transition_locked(
            self,
            batch_ids=batch_ids,
            transition_kind=transition_kind,
            source_domain=source_domain,
            target_domain=target_domain,
            source_concept=source_concept,
            target_concept=target_concept,
            created_at=created_at,
            impacts=impacts,
        )

    def _study_identity_transition_impacts_locked(
        self,
        *,
        batch_ids: list[str],
        transition_kind: str,
        source_concept: str | None,
    ) -> dict[str, dict[str, list[str]]]:
        """在 identity 写入前冻结实际受影响的输入和已物化候选集合."""
        return _StudyRepository._study_identity_transition_impacts_locked_in_tx(
            self,
            self._conn,
            batch_ids=batch_ids,
            transition_kind=transition_kind,
            source_concept=source_concept,
        )

    @staticmethod
    def _row_to_study_suggestion_batch(row: sqlite3.Row) -> dict:
        return _DatabaseRowMappersExtra._row_to_study_suggestion_batch(
            row,
        )

    def _list_study_suggestions_locked(
        self,
        *,
        batch_id: str | None,
        domain: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict]]:
        return _StudyRepository._list_study_suggestions_locked_in_tx(
            self,
            self._conn,
            batch_id=batch_id,
            domain=domain,
            status=status,
            limit=limit,
            offset=offset,
        )

    def _row_to_study_suggestion_locked(self, row: sqlite3.Row) -> dict:
        return _DatabaseRowMappersExtra._row_to_study_suggestion_locked(
            self,
            row,
        )

    def _assert_study_suggestion_evidence_current_locked(
        self,
        suggestion: sqlite3.Row,
    ) -> list[dict]:
        return _StudyRepository._assert_study_suggestion_evidence_current_locked_in_tx(
            self,
            self._conn,
            suggestion,
        )

    def _study_suggestion_evidence_state_locked(
        self,
        evidence: sqlite3.Row,
        *,
        expected_domain: str,
    ) -> tuple[str, str | None, str]:
        """从当前 job 和 chunk 重算证据状态,不信任缓存的 status."""
        return _StudyRepository._study_suggestion_evidence_state_locked_in_tx(
            self,
            self._conn,
            evidence,
            expected_domain=expected_domain,
        )

    def _assert_study_suggestion_evidence_row_current_locked(
        self,
        evidence: sqlite3.Row,
        *,
        expected_domain: str,
    ) -> None:
        return _StudyRepository._assert_study_suggestion_evidence_row_current_locked(
            self,
            evidence,
            expected_domain=expected_domain,
        )

    def _study_card_content_duplicate_locked(
        self,
        *,
        domain: str,
        card_type: str,
        front: str,
        back: str,
        explanation: str,
    ) -> bool:
        return _StudyRepository._study_card_content_duplicate_locked_in_tx(
            self,
            self._conn,
            domain=domain,
            card_type=card_type,
            front=front,
            back=back,
            explanation=explanation,
        )

    def _revalidate_study_suggestion_evidence_locked(
        self,
        *,
        job_id: str,
        note_type: str | None = None,
    ) -> None:
        """job 或 chunk 变化后更新可变有效性,快照始终不改."""
        return _StudyRepository._revalidate_study_suggestion_evidence_locked_in_tx(
            self,
            self._conn,
            job_id=job_id,
            note_type=note_type,
        )

    def _row_to_study_card(self, row: sqlite3.Row) -> dict:
        return _DatabaseRowMappersExtra._row_to_study_card(
            self,
            row,
        )

    def _row_to_glossary(self, row: sqlite3.Row) -> dict:
        return _DatabaseRowMappersExtra._row_to_glossary(
            self,
            row,
        )

    @staticmethod
    def _row_to_concept_definition_version(row: sqlite3.Row) -> dict:
        return _DatabaseRowMappersExtra._row_to_concept_definition_version(
            row,
        )

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return _DatabaseRowMappers.job(row, parse_datetime=_parse_dt)

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        return _DatabaseRowMappersExtra._row_to_step(
            self,
            row,
        )

    def _row_to_worker(self, row: sqlite3.Row) -> Worker:
        return _DatabaseRowMappersExtra._row_to_worker(
            self,
            row,
        )

    def _maintenance_glossary_rows(self) -> list[dict]:
        return _MaintenanceRepository.glossary_rows(self)

    def _maintenance_glossary_zh_name(
        self, domain: str, term: str
    ) -> dict | None:
        return _MaintenanceRepository.glossary_zh_name(self, domain, term)

    def _maintenance_credential_keys(self) -> list[str]:
        return _MaintenanceRepository.credential_keys(self)

from .repositories.aggregates import DatabaseAggregates as _DatabaseAggregates
from .repositories.collections import CollectionsRepository as _CollectionsRepository
from .repositories.concepts import ConceptsRepository as _ConceptsRepository
from .repositories.credentials import CredentialsRepository as _CredentialsRepository
from .repositories.jobs import JobsRepository as _JobsRepository
from .repositories.mappers_extra import DatabaseRowMappersExtra as _DatabaseRowMappersExtra
from .repositories.prompts import PromptsRepository as _PromptsRepository
from .repositories.search import SearchRepository as _SearchRepository
from .repositories.study import StudyRepository as _StudyRepository
from .repositories.telemetry import TelemetryRepository as _TelemetryRepository
from .repositories.workers import WorkersRepository as _WorkersRepository
from .repositories.jobs import JobsReadRepository as _JobsReadRepository
from .repositories.mappers import DatabaseRowMappers as _DatabaseRowMappers
from .repositories.maintenance import MaintenanceRepository as _MaintenanceRepository
