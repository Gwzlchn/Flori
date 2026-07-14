"""SQLite 数据库层。"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import structlog

from .concepts import norm_related as _norm_related
from .models import AIUsage, Collection, Job, JobStatus, Step, StepStatus, Worker
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

# schema 版本只从不可变迁移清单读取。
SCHEMA_VERSION = current_schema_version()

# SQLite INTEGER 是有符号 64 位整数.Prompt 版本从 1 开始,这组边界同时供
# API schema 和 DB 绑定前防御使用.
PROMPT_VERSION_MIN = 1
PROMPT_VERSION_MAX = (1 << 63) - 1
PROMPT_VERSION_EXCLUSIVE_MAX = 1 << 63


class PromptVersionExhaustedError(ValueError):
    """Prompt 历史已用完 SQLite 可表示的正整数版本."""


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
        for _attempt in range(3):
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


def _fts_match_query(q: str) -> str:
    """把用户查询串包成 fts5 安全的双引号短语,防 MATCH 语法注入。
    内部双引号转义为两个双引号;空白折叠;空查询返回空串(调用方按无结果处理)。"""
    # 剔除空字节(null byte):sqlite3 绑定含 \x00 的串会抛 "unterminated string";它也非有效检索词。
    cleaned = " ".join((q or "").replace("\x00", "").split())
    if not cleaned:
        return ""
    escaped = cleaned.replace('"', '""')
    return f'"{escaped}"'


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
        # 先固定并校验代码迁移集。清单分叉时不得创建目录、锁或 SQLite sidecar。
        validate_registry(migration_steps())
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.parent / f".{self._path.name}.migration.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # 同一把跨进程锁也覆盖 probe -> connect -> WAL 切换,避免另一启动
            # 进程把这里的瞬态 rollback journal 误判为 crash 残留.
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            probed_version = _probe_schema_version_without_sqlite(self._path)
            if probed_version is not None and probed_version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"SQLite user_version={probed_version} 高于当前程序上限 "
                    f"{SCHEMA_VERSION}，已在连接前拒绝"
                )
            self._conn = sqlite3.connect(
                str(self._path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            try:
                # 未知新 schema 必须在 WAL 切换等任何写性 PRAGMA 之前拒绝。
                assert_schema_compatible(
                    self._conn,
                    minimum_version=0,
                    maximum_version=SCHEMA_VERSION,
                )
            except BaseException:
                self._conn.close()
                raise
            # 多进程各开连接写同一文件,撞 SQLITE_BUSY 时等待而非立刻报错.
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        # RLock(可重入):写方法持锁 execute+commit,序列化对单一共享连接的写访问。
        # 读方法多数直接走单一共享连接(check_same_thread=False),依赖 C 层(GIL + SQLite
        # 单条语句)的原子性而不额外持锁;少数"多条读+组装"的复合读(如 get_job/list_jobs)
        # 持锁,序列化读游标迭代与另一线程 commit,避免见到半提交态。WAL+busy_timeout 负责
        # 跨连接竞争。可重入以便持锁方法内部再调其它持锁读不自死锁。
        self._lock = threading.RLock()

    def init_schema(self) -> None:
        # 三个后端进程会同时启动，文件锁覆盖读版本、快照和整条迁移。
        lock_path = self._path.parent / f".{self._path.name}.migration.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with self._lock:
                before = self.schema_version()
                if before < SCHEMA_VERSION and self._has_user_schema():
                    self._create_migration_backup(before)
                run_migrations(self._conn, self._migration_steps())
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _migration_steps(self) -> tuple[Migration, ...]:
        return migration_steps()

    def _has_user_schema(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations' LIMIT 1"
        ).fetchone()
        return row is not None

    def _create_migration_backup(self, from_version: int) -> Path:
        """为非空库保留升级前一致快照，同版迁移重试时原子刷新。"""
        backup_dir = self._path.parent / "migration-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / (
            f"{self._path.stem}.pre-v{from_version}-to-v{SCHEMA_VERSION}.db"
        )
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        destination: sqlite3.Connection | None = None
        try:
            destination = sqlite3.connect(str(temporary))
            self._conn.backup(destination, pages=256, sleep=0.01)
            destination.commit()
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            copied_version = int(
                destination.execute("PRAGMA user_version").fetchone()[0]
            )
            if integrity != ("ok",) or copied_version != from_version:
                raise sqlite3.DatabaseError(
                    f"迁移前快照校验失败: integrity={integrity}, "
                    f"user_version={copied_version}"
                )
            destination.close()
            destination = None
            source_mode = stat.S_IMODE(self._path.stat().st_mode)
            os.chmod(temporary, source_mode)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(backup_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if destination is not None:
                destination.close()
            temporary.unlink(missing_ok=True)
            raise
        return target

    def schema_version(self) -> int:
        """当前库的 schema 版本(PRAGMA user_version)。供备份兼容/未来迁移判断。"""
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        # 持锁关闭,确保没有线程正在使用连接。
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # Job

    def create_job(self, job: Job) -> None:
        # lineage_key 缺省由 id 反推(去时间戳),保证同源快照归一组。
        lineage = job.lineage_key or _lineage_key_of(job.id)
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs
                   (id, content_type, pipeline, collection_id, url, title,
                    domain, source, style_tags, status, progress_pct, meta,
                    published_at, created_at, updated_at, error,
                    lineage_key, is_current, source_digest, pipeline_digest, parent_job_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.id,
                    job.content_type,
                    job.pipeline,
                    job.collection_id,
                    job.url,
                    job.title,
                    job.domain,
                    job.source,
                    json.dumps(job.style_tags, ensure_ascii=False),
                    job.status.value if isinstance(job.status, JobStatus) else job.status,
                    job.progress_pct,
                    json.dumps(job.meta, ensure_ascii=False),
                    job.published_at.isoformat() if job.published_at else None,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.error,
                    lineage,
                    1 if job.is_current else 0,
                    job.source_digest,
                    job.pipeline_digest,
                    job.parent_job_id,
                ),
            )
            # 新快照设为 current → 同 lineage 其余降为历史(列表/KB 默认只显 current)。
            if job.is_current and lineage:
                self._conn.execute(
                    "UPDATE jobs SET is_current=0 WHERE lineage_key=? AND id!=?",
                    (lineage, job.id),
                )
            self._conn.commit()

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def jobs_brief(self, job_ids: list[str]) -> dict[str, dict]:
        """批量取作业简要(队列 / worker 历史 enrich 用):
        {job_id: {title, content_type, domain, status, pipeline}}。pipeline 供运行中 task 解析 step→pool。
        一次 IN 查询避免 N+1;去重保序、跳空 id;SQLite 变量上限按 500 分批。"""
        ids = [j for j in dict.fromkeys(job_ids) if j]
        if not ids:
            return {}
        out: dict[str, dict] = {}
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT id, title, content_type, domain, status, pipeline FROM jobs WHERE id IN ({ph})",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[r["id"]] = {
                        "title": r["title"], "content_type": r["content_type"],
                        "domain": r["domain"], "status": r["status"],
                        "pipeline": r["pipeline"],
                    }
        return out

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
        where_parts: list[str] = []
        params: list = []
        # 默认按 lineage 归组只返 current 快照(同一内容的历史版不平铺;经版本跳转看历史)。
        if current_only:
            where_parts.append("is_current=1")
        if status:
            where_parts.append("status=?")
            params.append(status)
        if uncategorized:           # 未归类:无所属集合(侧栏「未归类」分组)
            where_parts.append("collection_id IS NULL")
        elif collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if source:
            where_parts.append("source=?")
            params.append(source)

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM jobs {where}", params
            ).fetchone()[0]

            rows = self._conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return total, [self._row_to_job(r) for r in rows]

    def lineage_versions(self, job_id: str) -> list[Job]:
        """同一 lineage(同源内容)的所有快照,按 created_at 倒序(供详情页历史版本跳转)。
        若该 job 无 lineage_key(旧库未回填)则只返它自己。"""
        with self._lock:
            row = self._conn.execute("SELECT lineage_key FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return []
            lk = row["lineage_key"]
            if not lk:
                one = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                return [self._row_to_job(one)] if one else []
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE lineage_key=? ORDER BY created_at DESC", (lk,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def promote_lineage_current(self, lineage_key: str) -> None:
        """若某 lineage 当前无 current(如 current 被删),把剩余最新 created_at 的一版提为 current。
        幂等:已有 current 则不动。"""
        if not lineage_key:
            return
        with self._lock:
            has = self._conn.execute(
                "SELECT 1 FROM jobs WHERE lineage_key=? AND is_current=1 LIMIT 1", (lineage_key,)
            ).fetchone()
            if has:
                return
            latest = self._conn.execute(
                "SELECT id FROM jobs WHERE lineage_key=? ORDER BY created_at DESC LIMIT 1",
                (lineage_key,),
            ).fetchone()
            if latest:
                self._conn.execute(
                    "UPDATE jobs SET is_current=1 WHERE id=?", (latest["id"],)
                )
                self._conn.commit()

    def lineage_counts(self, lineage_keys: list[str]) -> dict[str, int]:
        """批量取各 lineage 的快照总数(供列表「N 个历史版本」提示)。一次 IN 查询。"""
        keys = [k for k in dict.fromkeys(lineage_keys) if k]
        if not keys:
            return {}
        out: dict[str, int] = {}
        with self._lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT lineage_key, COUNT(*) AS n FROM jobs WHERE lineage_key IN ({ph}) GROUP BY lineage_key",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[r["lineage_key"]] = r["n"]
        return out

    def count_jobs_by_status(self, collection_id: str | None = None) -> dict[str, int]:
        """一次 GROUP BY 取各状态计数(替代多次 list_jobs(limit=0) 的 COUNT+空 SELECT)。
        传 collection_id 则只统计该集合,供集合详情页 status_counts 用。"""
        where = "WHERE collection_id=?" if collection_id else ""
        params = (collection_id,) if collection_id else ()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs {where} GROUP BY status",
                params,
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def job_facets(self) -> dict[str, dict]:
        """全量 jobs 按 source / domain / status 的计数,供前端过滤 chip 显示(后端聚合,非客户端基于已加载)。"""
        def grp(col: str) -> dict:
            with self._lock:
                return {
                    r[0]: r[1]
                    for r in self._conn.execute(
                        f"SELECT {col}, COUNT(*) FROM jobs GROUP BY {col}"
                    ).fetchall()
                    if r[0] is not None
                }
        return {"source": grp("source"), "domain": grp("domain"), "status": grp("status")}

    def glossary_for_job(self, job_id: str, domain: str | None = None) -> list[dict]:
        """反查:occurrences 含该 job_id 的概念(LIKE 粗筛 + 精确过滤防子串误命中),
        供内容详情·概念 tab。rejected(已驳回)不返回。"""
        sql = "SELECT * FROM glossary WHERE status != 'rejected' AND occurrences LIKE ?"
        params: list = [f'%"{job_id}"%']
        if domain:
            sql += " AND domain=?"
            params.append(domain)
        out: list[dict] = []
        for r in self._conn.execute(sql, params):
            g = self._row_to_glossary(r)
            occs = g.get("occurrences") or []
            hit = [o for o in occs if isinstance(o, dict) and o.get("job_id") == job_id]
            if hit:
                g["job_occurrences"] = hit       # 该 job 命中的位置(首次出现等)
                out.append(g)
        return out

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        invalid = set(fields.keys()) - _JOB_UPDATABLE
        if invalid:
            raise ValueError(f"Invalid job columns: {invalid}")
        fields["updated_at"] = _now_iso()
        if "style_tags" in fields:
            fields["style_tags"] = json.dumps(fields["style_tags"], ensure_ascii=False)
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        if "status" in fields and isinstance(fields["status"], JobStatus):
            fields["status"] = fields["status"].value
        if "published_at" in fields and isinstance(fields["published_at"], datetime):
            fields["published_at"] = fields["published_at"].isoformat()

        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [job_id]
        # FTS 行冗余存 title/domain/collection_id,这几项变更要同步,否则检索元数据漂移。
        fts_sync = {k: fields[k] for k in ("title", "domain", "collection_id") if k in fields}
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {set_clause} WHERE id=?", values
            )
            if fts_sync:
                fts_clause = ", ".join(f"{k}=?" for k in fts_sync)
                self._conn.execute(
                    f"UPDATE notes_fts5 SET {fts_clause} WHERE job_id=?",
                    [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                )
                self._conn.execute(
                    f"UPDATE note_chunks SET {fts_clause} WHERE job_id=?",
                    [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                )
                self._conn.execute(
                    f"UPDATE note_chunks_fts5 SET {fts_clause} WHERE job_id=?",
                    [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                )
            self._conn.commit()

    def _strip_occurrences_for_jobs(self, job_ids: list[str]) -> None:
        """从 glossary.occurrences 摘除指向这些 job 的出现(保留概念与定义)。
        调用方须已持锁且在同一事务内;本方法只 execute,不 commit。"""
        for job_id in job_ids:
            # glossary.occurrences=[{job_id,...}],摘掉指向已删 job 的出现。
            rows = self._conn.execute(
                "SELECT domain, term, occurrences FROM glossary WHERE occurrences LIKE ?",
                (f'%"{job_id}"%',),
            ).fetchall()
            for r in rows:
                try:
                    occs = json.loads(r["occurrences"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
                kept = [o for o in occs if o.get("job_id") != job_id]
                if len(kept) != len(occs):
                    self._conn.execute(
                        "UPDATE glossary SET occurrences=? WHERE domain=? AND term=?",
                        (json.dumps(kept, ensure_ascii=False), r["domain"], r["term"]),
                    )

    def delete_job_cascade(
        self, job_id: str, collection_id: str | None = None, item_id: str | None = None
    ) -> None:
        """原子删 job:jobs 行 + FTS 索引 + ai_usage 行 + 集合计数 -1 + 摘除 glossary.occurrences 里的 job_id
        +(订阅 job)清 ingested_items 该条。全部单事务,避免两次 commit 间崩溃留孤儿。
        job_steps 经 FK ON DELETE CASCADE 连带删除。
        item_id:订阅来源 job 的去重键(从 job.meta['source_item_id'] 取);传了才清 ingested_items
        → 该条下轮订阅枚举可重新入库(彻底删除)。"""
        with self._lock:
            self._conn.execute("DELETE FROM notes_fts5 WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM note_chunks WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM note_chunks_fts5 WHERE job_id=?", (job_id,))
            # ai_usage 无外键,不会随 jobs 行 CASCADE,须显式删,否则 token/费用行成永久悬挂孤儿。
            self._conn.execute("DELETE FROM ai_usage WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            if collection_id:
                self._conn.execute(
                    "UPDATE collections SET job_count = MAX(0, job_count - 1) WHERE id=?",
                    (collection_id,),
                )
                if item_id:
                    self._conn.execute(
                        "DELETE FROM ingested_items WHERE collection_id=? AND item_id=?",
                        (collection_id, item_id),
                    )
            self._strip_occurrences_for_jobs([job_id])
            self._conn.commit()

    # Step

    def upsert_step(self, step: Step) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO job_steps
                   (job_id, step, status, pool, input_hash, worker_id,
                    started_at, finished_at, duration_sec, meta, error, retries)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    step.job_id,
                    step.name,
                    step.status.value if isinstance(step.status, StepStatus) else step.status,
                    step.pool,
                    step.input_hash,
                    step.worker_id,
                    step.started_at.isoformat() if step.started_at else None,
                    step.finished_at.isoformat() if step.finished_at else None,
                    step.duration_sec,
                    json.dumps(step.meta, ensure_ascii=False) if step.meta else None,
                    step.error,
                    step.retries,
                ),
            )
            self._conn.commit()

    def get_steps(self, job_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE job_id=? ORDER BY step", (job_id,)
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def delete_step(self, job_id: str, step_name: str) -> None:
        """删单个步骤行(供 resubmit 对齐:删去当前 pipeline 不再有的步,避免 DB 残留旧步)。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM job_steps WHERE job_id=? AND step=?", (job_id, step_name)
            )
            self._conn.commit()

    def update_step(
        self, job_id: str, step_name: str, *, only_if_active: bool = False, **fields
    ) -> None:
        """更新步骤行。only_if_active=True 时仅在当前状态非终态(done/skipped)才写,
        防成功步被迟到的失败上报覆盖(done→failed 不一致)。"""
        if not fields:
            return
        invalid = set(fields.keys()) - _STEP_UPDATABLE
        if invalid:
            raise ValueError(f"Invalid step columns: {invalid}")
        if "status" in fields and isinstance(fields["status"], StepStatus):
            fields["status"] = fields["status"].value
        if "meta" in fields and isinstance(fields["meta"], dict):
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        if "started_at" in fields and isinstance(fields["started_at"], datetime):
            fields["started_at"] = fields["started_at"].isoformat()
        if "finished_at" in fields and isinstance(fields["finished_at"], datetime):
            fields["finished_at"] = fields["finished_at"].isoformat()

        set_clause = ", ".join(f"{k}=?" for k in fields)
        where = "job_id=? AND step=?"
        values = list(fields.values()) + [job_id, step_name]
        if only_if_active:
            where += " AND status NOT IN ('done','skipped')"
        with self._lock:
            self._conn.execute(
                f"UPDATE job_steps SET {set_clause} WHERE {where}",
                values,
            )
            self._conn.commit()

    # Worker

    def upsert_worker(self, worker: Worker) -> None:
        # ON CONFLICT DO UPDATE 而非 INSERT OR REPLACE:REPLACE 是整行删重建,会把不在
        # 列清单里的中心配置列(desired_config/cfg_rev)清零——worker 每次重注册都会走到
        # 这里,页面下发的配置绝不能被重启冲掉。
        with self._lock:
            self._conn.execute(
                """INSERT INTO workers
                   (id, type, pools, tags, reject_tags, hostname, gpu_name,
                    gpu_memory_mb, concurrency, remote_addr, status, admin_status,
                    current_job, current_step,
                    tasks_completed, tasks_failed, total_duration_sec,
                    first_seen, started_at, last_heartbeat, admin_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     type=excluded.type, pools=excluded.pools, tags=excluded.tags,
                     reject_tags=excluded.reject_tags, hostname=excluded.hostname,
                     gpu_name=excluded.gpu_name, gpu_memory_mb=excluded.gpu_memory_mb,
                     concurrency=excluded.concurrency, remote_addr=excluded.remote_addr,
                     status=excluded.status, admin_status=excluded.admin_status,
                     current_job=excluded.current_job, current_step=excluded.current_step,
                     tasks_completed=excluded.tasks_completed, tasks_failed=excluded.tasks_failed,
                     total_duration_sec=excluded.total_duration_sec,
                     first_seen=excluded.first_seen, started_at=excluded.started_at,
                     last_heartbeat=excluded.last_heartbeat, admin_note=excluded.admin_note""",
                (
                    worker.id,
                    worker.type,
                    json.dumps(worker.pools),
                    json.dumps(sorted(worker.tags)),
                    json.dumps(sorted(worker.reject_tags)),
                    worker.hostname,
                    worker.gpu_name,
                    worker.gpu_memory_mb,
                    worker.concurrency,
                    worker.remote_addr,
                    worker.status,
                    worker.admin_status,
                    worker.current_job,
                    worker.current_step,
                    worker.tasks_completed,
                    worker.tasks_failed,
                    worker.total_duration_sec,
                    worker.first_seen.isoformat(),
                    worker.started_at.isoformat() if worker.started_at else None,
                    worker.last_heartbeat.isoformat() if worker.last_heartbeat else None,
                    worker.admin_note,
                ),
            )
            self._conn.commit()

    def get_worker(
        self,
        worker_id: str,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if row is None:
            return None
        w = self._row_to_worker(row)
        self._apply_status(w, online_window_sec, stale_window_sec)
        return w

    def list_workers(
        self,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> list[Worker]:
        """列出所有 worker,状态由后端按心跳新鲜度统一算出(online-idle/busy、
        offline、stale,paused 为管理员叠加)。越过 stale 窗口的持久化为信号,
        供 GC 回收僵尸 worker。"""
        rows = self._conn.execute("SELECT * FROM workers").fetchall()
        workers = [self._row_to_worker(r) for r in rows]
        now = datetime.now(timezone.utc)
        for w in workers:
            self._apply_status(w, online_window_sec, stale_window_sec, now=now)
        return workers

    def _apply_status(
        self,
        w: Worker,
        online_window_sec: int,
        stale_window_sec: int,
        now: datetime | None = None,
    ) -> None:
        """把 worker 的存量字段折算成对外公共状态,并对 stale 持久化(不动心跳)。
        管理员叠加位(paused)来自独立的 admin_status 列;运行时 status 列只供 busy/idle + GC。"""
        public = compute_worker_status(
            last_heartbeat=w.last_heartbeat,
            current_job=w.current_job,
            admin_status=w.admin_status,
            now=now,
            online_window_sec=online_window_sec,
            stale_window_sec=stale_window_sec,
        )
        if public == STALE and w.status != STALE:
            self.set_worker_status(w.id, STALE)
        w.status = public

    def set_worker_status(self, worker_id: str, status: str) -> None:
        """仅更新 worker 状态,不触碰 last_heartbeat(用于标记僵尸为 offline)。"""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET status=? WHERE id=?", (status, worker_id),
            )
            self._conn.commit()

    def set_worker_admin_status(self, worker_id: str, admin_status: str) -> None:
        """仅更新管理员暂停叠加位("" / "paused"),不触碰运行时 status / 心跳。"""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET admin_status=? WHERE id=?",
                (admin_status, worker_id),
            )
            self._conn.commit()

    def increment_worker_stats(
        self,
        worker_id: str,
        completed: int = 0,
        failed: int = 0,
        duration: float = 0.0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE workers SET
                   tasks_completed = tasks_completed + ?,
                   tasks_failed = tasks_failed + ?,
                   total_duration_sec = total_duration_sec + ?
                   WHERE id=?""",
                (completed, failed, duration, worker_id),
            )
            self._conn.commit()

    def set_worker_desired_config(self, worker_id: str, config: dict) -> int:
        """写中心期望配置并 cfg_rev+1(单调);返回新 rev,worker 不存在返回 -1。
        config 只存显式指定的键(pools/concurrency/tags/reject_tags),worker 端按键应用。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT cfg_rev FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
            if cur is None:
                return -1
            rev = (cur["cfg_rev"] or 0) + 1
            self._conn.execute(
                "UPDATE workers SET desired_config=?, cfg_rev=? WHERE id=?",
                (json.dumps(config), rev, worker_id),
            )
            self._conn.commit()
            return rev

    def get_worker_desired_config(self, worker_id: str) -> tuple[dict | None, int]:
        """读中心期望配置;(None, 0) = 未配置/worker 不存在(worker 端视为尊重自报)。"""
        row = self._conn.execute(
            "SELECT desired_config, cfg_rev FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if row is None or not row["desired_config"]:
            return None, (row["cfg_rev"] or 0) if row else 0
        try:
            return json.loads(row["desired_config"]), row["cfg_rev"] or 0
        except (ValueError, TypeError):
            return None, row["cfg_rev"] or 0

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
        fields = {"last_heartbeat": datetime.now(timezone.utc).isoformat()}
        if status is not None:
            fields["status"] = status
        if current_job is not None:
            fields["current_job"] = current_job or None
        if current_step is not None:
            fields["current_step"] = current_step or None
        if concurrency is not None:
            fields["concurrency"] = max(1, int(concurrency))
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [worker_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE workers SET {set_clause} WHERE id=?",
                values,
            )
            self._conn.commit()

    def delete_worker(self, worker_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM workers WHERE id=?", (worker_id,))
            self._conn.commit()

    def list_running_steps(self) -> list[Step]:
        """所有 status=running 的 step(= 正在执行的 task),按开始时间倒序。
        队列页「运行中」分组的权威来源:step 行自带 pool/worker_id/started_at,无需依赖 worker 心跳派生。"""
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE status=? ORDER BY started_at DESC",
            (StepStatus.RUNNING.value,),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def list_worker_tasks(self, worker_id: str, limit: int = 50) -> list[Step]:
        """该 worker 的 task 执行历史(task = 某作业的某步骤的一次执行,按最近开始时间倒序;每条 = 一个 step 记录)。"""
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE worker_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (worker_id, limit),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

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
        with self._lock:
            if revoke_existing:
                self._conn.execute(
                    "UPDATE worker_tokens SET revoked=1 WHERE worker_id=?",
                    (worker_id,),
                )
            self._conn.execute(
                """INSERT OR REPLACE INTO worker_tokens
                   (token_hash, worker_id, pools, tags, created_at, revoked)
                   VALUES (?,?,?,?,?,?)""",
                (
                    token_hash,
                    worker_id,
                    json.dumps(list(pools)),
                    json.dumps(list(tags)),
                    created_at.isoformat(),
                    1 if revoked else 0,
                ),
            )
            self._conn.commit()

    def get_worker_token_by_hash(self, token_hash: str) -> dict | None:
        """按 token hash 查 token 行,未命中返回 None;revoked 折算成 bool。"""
        row = self._conn.execute(
            "SELECT * FROM worker_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        return {
            "token_hash": row["token_hash"],
            "worker_id": row["worker_id"],
            "pools": json.loads(row["pools"]),
            "tags": json.loads(row["tags"]),
            "created_at": _parse_dt(row["created_at"]),
            "last_used": _parse_dt(row["last_used"]),
            "revoked": bool(row["revoked"]),
        }

    def revoke_worker_token(self, worker_id: str) -> None:
        """吊销某 worker 名下全部 token(删 worker 时连带,使其心跳/认领立即 401)。"""
        with self._lock:
            self._conn.execute(
                "UPDATE worker_tokens SET revoked=1 WHERE worker_id=?", (worker_id,)
            )
            self._conn.commit()

    def list_worker_tokens(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM worker_tokens ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "token_hash": r["token_hash"],
                "worker_id": r["worker_id"],
                "pools": json.loads(r["pools"]),
                "tags": json.loads(r["tags"]),
                "created_at": _parse_dt(r["created_at"]),
                "last_used": _parse_dt(r["last_used"]),
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ]

    # App Credentials

    def set_credential(self, key: str, value: str) -> None:
        """存/覆盖一条应用级凭证(如 B站 cookie JSON),按 key 幂等 upsert。

        设了 FLORI_SECRET_KEY 时以 Fernet token 加密落库;未设则存明文(向后兼容)
        并一次性告警(建议设 key 以 at-rest 加密)。"""
        f = _fernet()
        if f is not None:
            stored = f.encrypt(value.encode()).decode()
        else:
            _warn_plaintext_credentials_once()
            stored = value
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO app_credentials (key, value, updated_at)
                   VALUES (?,?,?)""",
                (key, stored, _now_iso()),
            )
            self._conn.commit()

    def get_credential(self, key: str) -> str | None:
        """读一条凭证值,未命中返回 None。

        有 Fernet key 时尝试解密;遇 InvalidToken(历史明文行,或换了 key 的旧 token)
        透传原始串(legacy passthrough)。无 key 则直接返回原始串。任何情况都不因坏值崩。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_credentials WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        raw = row["value"]
        if raw is None:
            return None
        f = _fernet()
        if f is None:
            return raw
        try:
            from cryptography.fernet import InvalidToken
            return f.decrypt(raw.encode()).decode()
        except InvalidToken:
            return raw  # 明文遗留行 / 异 key 的 token:原样透传
        except Exception:
            return raw  # 任何意外都不让读凭证崩

    def delete_credential(self, key: str) -> None:
        """删一条凭证(如登出清除 B站 cookie)。"""
        with self._lock:
            self._conn.execute("DELETE FROM app_credentials WHERE key=?", (key,))
            self._conn.commit()

    # AI Usage

    def record_ai_usage(self, usage: AIUsage) -> bool:
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO ai_usage
                       (exec_id, job_id, step, worker_id, provider, model,
                        input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        cost_usd, duration_sec, num_turns, cached, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        usage.exec_id,
                        usage.job_id,
                        usage.step,
                        usage.worker_id,
                        usage.provider,
                        usage.model,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_creation_input_tokens,
                        usage.cache_read_input_tokens,
                        usage.cost_usd,
                        usage.duration_sec,
                        usage.num_turns,
                        1 if usage.cached else 0,
                        usage.created_at.isoformat(),
                    ),
                )
                self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def record_ai_task_log(self, log: dict) -> bool:
        """落一条独立 AI task 的白盒审计(对应 DAG 的 output/ai_logs/{step}.jsonl;AI task 无 job_dir 故入库)。
        log = 索引列(task_id/exec_id/step_name/domain/provider/model/ok/error/各 token/cost/duration/num_turns)
        + record(全量审计 dict,存进 record_json)+ created_at。best-effort,不让审计失败影响主流程。"""
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO ai_task_logs
                       (task_id, exec_id, step_name, domain, provider, model, ok, error,
                        input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                        cost_usd, duration_sec, num_turns, record_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        log.get("task_id"), log.get("exec_id"), log.get("step_name"),
                        log.get("domain"), log.get("provider"), log.get("model"),
                        1 if log.get("ok", True) else 0, log.get("error"),
                        log.get("input_tokens", 0), log.get("output_tokens", 0),
                        log.get("cache_creation_input_tokens", 0), log.get("cache_read_input_tokens", 0),
                        log.get("cost_usd", 0.0), log.get("duration_sec", 0.0), log.get("num_turns", 0),
                        json.dumps(log.get("record", {}), ensure_ascii=False, default=str),
                        log.get("created_at"),
                    ),
                )
                self._conn.commit()
            return True
        except Exception:
            return False

    def get_ai_task_logs(self, task_id: str) -> list[dict]:
        """读某 AI task 的白盒审计(供查看端点);最近在前。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ai_task_logs WHERE task_id=? ORDER BY id DESC", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # Prompt Overrides

    @staticmethod
    def _norm_override_key(scope: str, domain: str | None) -> tuple[str, str]:
        """归一 (scope, domain):scope 非 'domain' 一律按 'global' 处理且 domain='';
        'domain' scope 须有非空 domain。返回 (scope, domain) 供主键统一(避免 NULL 破唯一)。"""
        if scope == "domain" and (domain or "").strip():
            return "domain", domain.strip()
        return "global", ""

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
        scope, dom = self._norm_override_key(scope, domain)
        key = (scope, dom, pipeline, step)
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "SELECT version FROM prompt_overrides WHERE scope=? AND domain=? "
                "AND pipeline=? AND step=?",
                key,
            ).fetchone()
            maxv = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM prompt_override_versions "
                "WHERE scope=? AND domain=? AND pipeline=? AND step=?",
                key,
            ).fetchone()[0]
            if cur is None and maxv == 0:
                version = 1                          # 首版
            elif mode == "new":
                if maxv >= PROMPT_VERSION_MAX:
                    raise PromptVersionExhaustedError(
                        "prompt version reached SQLite INTEGER limit"
                    )
                version = maxv + 1                   # 另存为新版本
            else:                                    # overwrite 当前激活版本
                version = cur["version"] if cur else (maxv or 1)
            # 历史行:overwrite 保留原 created_at/note(note 给定才覆盖);new/首版用 now。
            prev = self._conn.execute(
                "SELECT created_at, note FROM prompt_override_versions WHERE scope=? "
                "AND domain=? AND pipeline=? AND step=? AND version=?",
                (*key, version),
            ).fetchone()
            created_at = prev["created_at"] if prev else now
            eff_note = note if note is not None else (prev["note"] if prev else "")
            self._conn.execute(
                """INSERT OR REPLACE INTO prompt_override_versions
                   (scope, domain, pipeline, step, version, content, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (*key, version, content or "", eff_note or "", created_at),
            )
            self._conn.execute(
                """INSERT OR REPLACE INTO prompt_overrides
                   (scope, domain, pipeline, step, content, version, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (*key, content or "", version, now),
            )
            self._conn.commit()
        return version

    def list_prompt_override_versions(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> list[dict]:
        """该 (scope,domain,pipeline,step) 的全部历史版本元信息(不含 content),version 升序。"""
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, note, created_at FROM prompt_override_versions "
                "WHERE scope=? AND domain=? AND pipeline=? AND step=? ORDER BY version",
                (scope, dom, pipeline, step),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_prompt_override_version(
        self, scope: str, domain: str | None, pipeline: str, step: str, version: int
    ) -> dict | None:
        """读某历史版本(含 content),未命中返回 None。"""
        if not _valid_prompt_version(version):
            return None
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_override_versions WHERE scope=? AND domain=? "
                "AND pipeline=? AND step=? AND version=?",
                (scope, dom, pipeline, step, version),
            ).fetchone()
        return dict(row) if row else None

    def delete_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> None:
        """删某步的 prompt 覆盖(恢复默认)——连同其全部历史版本一并删。无则 no-op。"""
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            self._conn.execute(
                "DELETE FROM prompt_overrides WHERE scope=? AND domain=? AND pipeline=? AND step=?",
                (scope, dom, pipeline, step),
            )
            self._conn.execute(
                "DELETE FROM prompt_override_versions WHERE scope=? AND domain=? "
                "AND pipeline=? AND step=?",
                (scope, dom, pipeline, step),
            )
            self._conn.commit()

    def deactivate_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> None:
        """停用某步覆盖(恢复内置默认)——非破坏:只删主表 prompt_overrides 那一行(激活指针),
        prompt_override_versions 全部历史版本完整保留(下拉里仍能看到 v1/v2…,可重新激活)。
        删指针后 resolve_prompt_overrides 返回空 → 派发回内置默认。无指针则 no-op。
        注:version 列 NOT NULL 不可空,故用删激活行而非置 NULL 表达停用。"""
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            self._conn.execute(
                "DELETE FROM prompt_overrides WHERE scope=? AND domain=? AND pipeline=? AND step=?",
                (scope, dom, pipeline, step),
            )
            self._conn.commit()

    def set_active_prompt_version(
        self, scope: str, domain: str | None, pipeline: str, step: str, version: int
    ) -> bool:
        """把激活指针指向某历史版本(re-activate):主表 content/version 同步成该版本,
        下次派发即用它。该版本不存在于 prompt_override_versions → 返回 False(不动);成功 True。
        主表此前可能无行(已 deactivate 状态)——直接 INSERT OR REPLACE 重建激活指针。"""
        if not _valid_prompt_version(version):
            return False
        scope, dom = self._norm_override_key(scope, domain)
        key = (scope, dom, pipeline, step)
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM prompt_override_versions WHERE scope=? AND domain=? "
                "AND pipeline=? AND step=? AND version=?",
                (*key, version),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                """INSERT OR REPLACE INTO prompt_overrides
                   (scope, domain, pipeline, step, content, version, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (*key, row["content"], version, _now_iso()),
            )
            self._conn.commit()
        return True

    def get_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str
    ) -> dict | None:
        """读单条 prompt 覆盖,未命中返回 None。"""
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_overrides WHERE scope=? AND domain=? AND pipeline=? AND step=?",
                (scope, dom, pipeline, step),
            ).fetchone()
        return dict(row) if row else None

    def list_prompt_overrides(self) -> list[dict]:
        """全量 prompt 覆盖(供设置页标记哪些步已有覆盖)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM prompt_overrides ORDER BY pipeline, step, scope, domain"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_prompt_overrides(
        self, pipeline: str, domain: str | None
    ) -> dict[str, dict]:
        """派发注入用:给定 job 的 pipeline + domain,返回 {step: {content, version}} 解析结果。
        domain 覆盖优先于 global;同一步两者都有则取 domain(连同其版本号)。job 创建时(api 有 DB)
        调用,结果写 job.json.prompt_overrides 随 job 下发(含激活版本号快照),worker step_base 读取
        (pure worker 无 DB)。空 content 视为无覆盖被过滤。
        注:worker _injected_prompt_override 兼容 dict 与存量纯字符串两种 job.json 形态。"""
        dom = (domain or "").strip()
        resolved: dict[str, dict] = {}
        with self._lock:
            # 先 global 铺底,再 domain 覆盖(同步 step domain 优先)。
            rows = self._conn.execute(
                "SELECT scope, domain, step, content, version FROM prompt_overrides "
                "WHERE pipeline=? AND (scope='global' OR (scope='domain' AND domain=?))",
                (pipeline, dom),
            ).fetchall()
        for r in rows:
            if r["scope"] == "global" and r["step"] not in resolved:
                resolved[r["step"]] = {"content": r["content"], "version": r["version"]}
        for r in rows:
            if r["scope"] == "domain":
                resolved[r["step"]] = {"content": r["content"], "version": r["version"]}
        return {k: v for k, v in resolved.items() if (v.get("content") or "").strip()}

    def get_usage_summary(
        self, job_id: str | None = None, since: str | None = None
    ) -> dict:
        where_parts: list[str] = []
        params: list = []
        if job_id:
            where_parts.append("job_id=?")
            params.append(job_id)
        if since:
            where_parts.append("created_at>=?")
            params.append(since)

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        row = self._conn.execute(
            f"""SELECT
                COUNT(*) as calls,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(cost_usd), 0) as total_cost
            FROM ai_usage {where}""",
            params,
        ).fetchone()

        return {
            "calls": row["calls"],
            "total_input_tokens": row["total_input"],
            "total_output_tokens": row["total_output"],
            "total_cost_usd": row["total_cost"],
        }

    def get_usage_aggregate(self) -> dict:
        """全量 AI 用量聚合(供 /api/usage + 系统状态展示):累计 token/缓存/成本 + 平均缓存命中率
        + 按 model 分。命中率 = cache_read /(input + cache_read + cache_creation)。"""
        with self._lock:
            total = self._conn.execute(
                """SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(input_tokens),0) AS in_tok,
                    COALESCE(SUM(output_tokens),0) AS out_tok,
                    COALESCE(SUM(cache_creation_input_tokens),0) AS cc_tok,
                    COALESCE(SUM(cache_read_input_tokens),0) AS cr_tok,
                    COALESCE(SUM(cost_usd),0) AS cost,
                    COALESCE(SUM(num_turns),0) AS turns,
                    COALESCE(SUM(duration_sec),0) AS dur
                FROM ai_usage""",
            ).fetchone()
            rows = self._conn.execute(
                """SELECT provider, model,
                    COUNT(*) AS calls,
                    COALESCE(SUM(input_tokens),0) AS in_tok,
                    COALESCE(SUM(output_tokens),0) AS out_tok,
                    COALESCE(SUM(cache_creation_input_tokens),0) AS cc_tok,
                    COALESCE(SUM(cache_read_input_tokens),0) AS cr_tok,
                    COALESCE(SUM(cost_usd),0) AS cost
                FROM ai_usage GROUP BY provider, model ORDER BY cost DESC""",
            ).fetchall()

        def _hit_rate(in_tok: int, cc: int, cr: int) -> float:
            denom = in_tok + cc + cr
            return round(cr / denom * 100, 1) if denom else 0.0

        return {
            "calls": total["calls"],
            "total_input_tokens": total["in_tok"],
            "total_output_tokens": total["out_tok"],
            "total_cache_creation_tokens": total["cc_tok"],
            "total_cache_read_tokens": total["cr_tok"],
            "total_cost_usd": round(total["cost"], 6),
            "total_num_turns": total["turns"],
            "total_duration_sec": round(total["dur"], 1),
            "cache_hit_rate_pct": _hit_rate(total["in_tok"], total["cc_tok"], total["cr_tok"]),
            "by_model": [
                {
                    "provider": r["provider"], "model": r["model"], "calls": r["calls"],
                    "input_tokens": r["in_tok"], "output_tokens": r["out_tok"],
                    "cache_creation_tokens": r["cc_tok"], "cache_read_tokens": r["cr_tok"],
                    "cost_usd": round(r["cost"], 6),
                    "cache_hit_rate_pct": _hit_rate(r["in_tok"], r["cc_tok"], r["cr_tok"]),
                }
                for r in rows
            ],
        }

    def list_usage_by_job(self, job_id: str) -> list[dict]:
        """该 job 的逐次 AI 调用明细(供 job 详情按步展示:in/out/cache/命中率/cost/耗时/轮数/worker)。
        命中率 = cache_read /(input + cache_read + cache_creation)。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT step, worker_id, provider, model,
                    input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    cost_usd, duration_sec, num_turns, created_at
                FROM ai_usage WHERE job_id=? ORDER BY created_at""",
                (job_id,),
            ).fetchall()
        out = []
        for r in rows:
            denom = r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"]
            hit = round(r["cache_read_input_tokens"] / denom * 100, 1) if denom else 0.0
            out.append({
                "step": r["step"], "worker_id": r["worker_id"],
                "provider": r["provider"], "model": r["model"],
                "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                "cache_creation_tokens": r["cache_creation_input_tokens"],
                "cache_read_tokens": r["cache_read_input_tokens"],
                "cost_usd": round(r["cost_usd"], 6), "duration_sec": r["duration_sec"],
                "num_turns": r["num_turns"], "cache_hit_rate_pct": hit,
            })
        return out

    def throughput_since(self, since_iso: str) -> dict:
        """近窗口吞吐:since_iso 之后进入终态的 job 计数(done/failed)。用 updated_at 近似终态时刻,
        rerun 改 updated_at 会重复计入但属罕见;利用 idx_jobs_status。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, COUNT(*) AS n FROM jobs
                   WHERE status IN ('done','failed') AND updated_at >= ?
                   GROUP BY status""",
                (since_iso,),
            ).fetchall()
        by = {r["status"]: r["n"] for r in rows}
        return {"done": by.get("done", 0), "failed": by.get("failed", 0)}

    # Collection

    def _row_to_collection(self, r: sqlite3.Row) -> Collection:
        return Collection(
            id=r["id"],
            name=r["name"],
            domain=r["domain"],
            description=r["description"],
            tags=json.loads(r["tags"]),
            job_count=r["job_count"],
            source_type=r["source_type"],
            source_id=r["source_id"],
            sync_enabled=bool(r["sync_enabled"]),
            last_synced_at=_parse_dt(r["last_synced_at"]),
            last_sync_status=r["last_sync_status"],
            last_sync_error=r["last_sync_error"],
            created_at=_parse_dt(r["created_at"]),
            updated_at=_parse_dt(r["updated_at"]),
        )

    def create_collection(self, collection: Collection) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO collections
                   (id, name, domain, description, tags, job_count,
                    source_type, source_id, sync_enabled, last_synced_at,
                    last_sync_status, last_sync_error, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    collection.id,
                    collection.name,
                    collection.domain,
                    collection.description,
                    json.dumps(collection.tags, ensure_ascii=False),
                    collection.job_count,
                    collection.source_type,
                    collection.source_id,
                    1 if collection.sync_enabled else 0,
                    collection.last_synced_at.isoformat() if collection.last_synced_at else None,
                    collection.last_sync_status,
                    collection.last_sync_error,
                    collection.created_at.isoformat(),
                    collection.updated_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_collection(self, collection_id: str) -> Collection | None:
        row = self._conn.execute(
            "SELECT * FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        return self._row_to_collection(row) if row else None

    def list_collections(self, domain: str | None = None) -> list[Collection]:
        if domain:
            rows = self._conn.execute(
                "SELECT * FROM collections WHERE domain=?", (domain,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM collections").fetchall()
        return [self._row_to_collection(r) for r in rows]

    def find_collection_by_source(self, source_type: str, source_id: str) -> Collection | None:
        """按来源找订阅集合(建订阅前去重;一个来源全局唯一对应一个订阅集合)。"""
        row = self._conn.execute(
            "SELECT * FROM collections WHERE source_type=? AND source_id=?",
            (source_type, source_id),
        ).fetchone()
        return self._row_to_collection(row) if row else None

    def list_subscription_collections(self, enabled_only: bool = False) -> list[Collection]:
        """订阅集合(source_type 非空);enabled_only 时仅自动追更开启的。周期同步用。"""
        q = "SELECT * FROM collections WHERE source_type IS NOT NULL"
        if enabled_only:
            q += " AND sync_enabled=1"
        return [self._row_to_collection(r) for r in self._conn.execute(q).fetchall()]

    def update_collection(
        self,
        collection_id: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        sync_enabled: bool | None = None,
    ) -> None:
        """更新集合可变字段(name/description/tags/订阅自动追更开关),None 表示不动。"""
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if tags is not None:
            fields["tags"] = json.dumps(tags, ensure_ascii=False)
        if sync_enabled is not None:
            fields["sync_enabled"] = 1 if sync_enabled else 0
        if not fields:
            return
        fields["updated_at"] = _now_iso()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [collection_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE collections SET {set_clause} WHERE id=?", values
            )
            self._conn.commit()

    def delete_collection(self, collection_id: str, purge: bool = False) -> None:
        """删集合两模式。默认解绑:名下 job 的 collection_id 置 NULL(保留 job)。
        purge=True:连名下 job 一起删(jobs 行 + FTS 行 + 摘除各 job 的 glossary.occurrences;
        注:产物/MinIO 清理走既有 job 删除路径)。
        两种都清该集合 ingested_items(便于重订阅重新入库)。FTS 索引行同步处理,避免悬空行。"""
        with self._lock:
            if purge:
                # 先摘除名下 job 的 glossary 出现记录,避免删 job 后留悬空 job_id(与 delete_job_cascade 一致)。
                job_rows = self._conn.execute(
                    "SELECT id FROM jobs WHERE collection_id=?", (collection_id,)
                ).fetchall()
                self._strip_occurrences_for_jobs([r["id"] for r in job_rows])
                self._conn.execute(
                    "DELETE FROM notes_fts5 WHERE collection_id=?", (collection_id,)
                )
                self._conn.execute(
                    "DELETE FROM note_chunks WHERE collection_id=?", (collection_id,)
                )
                self._conn.execute(
                    "DELETE FROM note_chunks_fts5 WHERE collection_id=?", (collection_id,)
                )
                # ai_usage 无外键,须显式删名下各 job 的用量行(与 delete_job_cascade 一致)。
                self._conn.execute(
                    "DELETE FROM ai_usage WHERE job_id IN "
                    "(SELECT id FROM jobs WHERE collection_id=?)",
                    (collection_id,),
                )
                self._conn.execute(
                    "DELETE FROM jobs WHERE collection_id=?", (collection_id,)
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET collection_id=NULL WHERE collection_id=?",
                    (collection_id,),
                )
                self._conn.execute(
                    "UPDATE notes_fts5 SET collection_id='' WHERE collection_id=?",
                    (collection_id,),
                )
                self._conn.execute(
                    "UPDATE note_chunks SET collection_id='' WHERE collection_id=?",
                    (collection_id,),
                )
                self._conn.execute(
                    "UPDATE note_chunks_fts5 SET collection_id='' WHERE collection_id=?",
                    (collection_id,),
                )
            self._conn.execute(
                "DELETE FROM ingested_items WHERE collection_id=?", (collection_id,)
            )
            self._conn.execute(
                "DELETE FROM collections WHERE id=?", (collection_id,)
            )
            self._conn.commit()

    def mark_collection_synced(self, collection_id: str, dt: datetime) -> None:
        """订阅集合同步成功后记录 last_synced_at,并置 last_sync_status=ok、清除错误。"""
        with self._lock:
            self._conn.execute(
                """UPDATE collections
                   SET last_synced_at=?, last_sync_status='ok', last_sync_error=NULL,
                       updated_at=? WHERE id=?""",
                (dt.isoformat(), _now_iso(), collection_id),
            )
            self._conn.commit()

    def set_sync_status(
        self, collection_id: str, status: str | None, error: str | None = None
    ) -> None:
        """更新订阅集合的同步状态(syncing/ok/error/None)。error 仅 status=error 时存,其余清空。"""
        err = (error or "")[:500] if status == "error" else None
        with self._lock:
            self._conn.execute(
                """UPDATE collections
                   SET last_sync_status=?, last_sync_error=?, updated_at=? WHERE id=?""",
                (status, err, _now_iso(), collection_id),
            )
            self._conn.commit()

    def domain_exists(self, domain: str) -> bool:
        """领域键是否已被使用(jobs/collections/glossary 任一有行)。用于 rename 防撞。"""
        with self._lock:
            for tbl in ("jobs", "collections", "glossary"):
                if self._conn.execute(
                    f"SELECT 1 FROM {tbl} WHERE domain=? LIMIT 1", (domain,)
                ).fetchone():
                    return True
        return False

    def rename_domain(self, old: str, new: str) -> dict[str, int]:
        """把领域键 old 原子改成 new(领域是派生键,散在 jobs/collections/glossary + notes_fts5 冗余列)。
        一个事务内迁移所有引用,任一失败回滚。返回各表迁移行数。调用方须先校验 new 合法且不冲突。"""
        with self._lock:
            try:
                n_jobs = self._conn.execute(
                    "UPDATE jobs SET domain=? WHERE domain=?", (new, old)
                ).rowcount
                n_coll = self._conn.execute(
                    "UPDATE collections SET domain=? WHERE domain=?", (new, old)
                ).rowcount
                n_gloss = self._conn.execute(
                    "UPDATE glossary SET domain=? WHERE domain=?", (new, old)
                ).rowcount
                self._conn.execute(
                    "UPDATE notes_fts5 SET domain=? WHERE domain=?", (new, old)
                )
                self._conn.execute(
                    "UPDATE note_chunks SET domain=? WHERE domain=?", (new, old)
                )
                self._conn.execute(
                    "UPDATE note_chunks_fts5 SET domain=? WHERE domain=?", (new, old)
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"jobs": n_jobs, "collections": n_coll, "glossary": n_gloss}

    # Domain(领域是派生视图:来自 jobs ∪ collections ∪ glossary 的 distinct domain)

    def list_domains(self) -> list[dict]:
        """领域总览:每个 domain 的 集合数/内容数/概念数/订阅数/最近活跃(派生,无 domains 表)。"""
        domains: set[str] = set()
        for tbl in ("jobs", "collections", "glossary"):
            for r in self._conn.execute(
                f"SELECT DISTINCT domain FROM {tbl} WHERE domain IS NOT NULL AND domain<>''"
            ):
                domains.add(r[0])

        def grp(sql: str) -> dict:
            return {r[0]: r[1] for r in self._conn.execute(sql)}

        coll_c = grp("SELECT domain, COUNT(*) FROM collections GROUP BY domain")
        job_c = grp("SELECT domain, COUNT(*) FROM jobs GROUP BY domain")
        concept_c = grp("SELECT domain, COUNT(*) FROM glossary GROUP BY domain")
        sub_c = grp("SELECT domain, COUNT(*) FROM collections WHERE source_type IS NOT NULL GROUP BY domain")
        last = grp("SELECT domain, MAX(updated_at) FROM jobs GROUP BY domain")
        return [
            {
                "domain": d,
                "collection_count": coll_c.get(d, 0),
                "job_count": job_c.get(d, 0),
                "concept_count": concept_c.get(d, 0),
                "subscription_count": sub_c.get(d, 0),
                "last_active_at": last.get(d),
            }
            for d in sorted(domains)
        ]

    def domain_top_terms(self, domain: str, limit: int = 30) -> list[dict]:
        """领域工作台语义栏:该 domain 的术语(含候选 suggested,各带 status;rejected 除外),
        按来源数(佐证强度代理)降序。候选数另由 suggested_count 单独提示;前端可按 status 区分展示。"""
        rows = self._conn.execute(
            "SELECT term, definition, occurrences, status, is_topic FROM glossary "
            "WHERE domain=? AND status != 'rejected'",
            (domain,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            out.append({
                "term": r["term"], "definition": r["definition"],
                "source_count": len(occs) if isinstance(occs, list) else 0,
                "status": r["status"], "is_topic": bool(r["is_topic"]),
            })
        out.sort(key=lambda t: t["source_count"], reverse=True)
        return out[:limit]

    def concept_timeline(self, domain: str, granularity: str = "month") -> dict:
        """概念时间线:把该 domain 各概念的 occurrences 经 job_id→源内容发布时间映射,按粒度分桶计数。
        分桶时间用 COALESCE(published_at, created_at):优先源内容在平台的发布/更新时间("这个概念
        在世界上何时出现"),无已知发布时间的 job 回退入库时间(created_at),不丢计数。
        granularity: day(YYYY-MM-DD) / week(YYYY-Www) / month(YYYY-MM)。无 glossary/job 时返回空。"""
        from collections import defaultdict
        job_dates = {
            r["id"]: r["bucket_at"]
            for r in self._conn.execute(
                "SELECT id, COALESCE(published_at, created_at) AS bucket_at "
                "FROM jobs WHERE domain=?",
                (domain,),
            )
        }

        def bucket(iso: str | None) -> str | None:
            dt = _parse_dt(iso)
            if dt is None:
                return None
            if granularity == "day":
                return dt.strftime("%Y-%m-%d")
            if granularity == "week":
                y, w, _ = dt.isocalendar()
                return f"{y}-W{w:02d}"
            return dt.strftime("%Y-%m")

        rows = self._conn.execute(
            "SELECT term, occurrences FROM glossary "
            "WHERE domain=? AND status != 'rejected'", (domain,)
        ).fetchall()
        totals: dict = defaultdict(int)
        concepts: list[dict] = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            buckets: dict = defaultdict(int)
            for o in occs if isinstance(occs, list) else []:
                b = bucket(job_dates.get(o.get("job_id")))
                if b:
                    buckets[b] += 1
                    totals[b] += 1
            if buckets:
                concepts.append({
                    "term": r["term"], "buckets": dict(buckets),
                    "total": sum(buckets.values()),
                })
        concepts.sort(key=lambda c: c["total"], reverse=True)
        return {
            "granularity": granularity,
            "buckets": sorted(totals),
            "totals": dict(totals),
            "concepts": concepts,
        }

    def concept_occurrence_dates(self, domain: str) -> dict[str, list[str]]:
        """概念趋势雷达基础数据:该 domain 各概念的每条 occurrence 经 job_id→源内容时间映射,
        返回 {term: [iso_date, ...]}(每个 occurrence 一个时间点,可重复)。时间口径与 concept_timeline
        一致:COALESCE(published_at, created_at)("这个概念在世界上何时出现",无发布时间回退入库时间)。
        无映射到时间的 occurrence 略过(不计入)。供 radar 服务按窗口切片算飙升/新出现,纯数据无业务策略。"""
        job_dates = {
            r["id"]: r["bucket_at"]
            for r in self._conn.execute(
                "SELECT id, COALESCE(published_at, created_at) AS bucket_at "
                "FROM jobs WHERE domain=?",
                (domain,),
            )
        }
        out: dict[str, list[str]] = {}
        rows = self._conn.execute(
            "SELECT term, occurrences FROM glossary "
            "WHERE domain=? AND status != 'rejected'", (domain,)
        ).fetchall()
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            dates: list[str] = []
            for o in occs if isinstance(occs, list) else []:
                d = job_dates.get(o.get("job_id")) if isinstance(o, dict) else None
                if d:
                    dates.append(d)
            out[r["term"]] = dates
        return out

    def domain_topics(self, domain: str) -> list[dict]:
        """领域内主题(可浏览标签) = 该 domain 所有 job 的 style_tags distinct + 计数。"""
        from collections import Counter
        c: Counter = Counter()
        for r in self._conn.execute("SELECT style_tags FROM jobs WHERE domain=?", (domain,)):
            try:
                for t in json.loads(r["style_tags"] or "[]"):
                    if t:
                        c[t] += 1
            except (ValueError, TypeError):
                pass
        return [{"topic": t, "count": n} for t, n in c.most_common()]

    def ingested_bvids(self) -> set[str]:
        """已入库的 B站 BV 号集合(从 jobs.url 提取),供订阅同步去重。
        通用去重走 ingested_items 表(见 ingested_item_ids/mark_ingested),按
        (collection_id, item_id) 去重;本方法只作存量 bili 数据的兜底回填——同步首跑时
        可把它的结果并入某集合的 ingested 集合,避免已入库的 B站视频被重复建 job。"""
        import re
        out: set[str] = set()
        for (u,) in self._conn.execute(
            "SELECT url FROM jobs WHERE url LIKE '%BV%'"
        ).fetchall():
            m = re.search(r"(BV[0-9A-Za-z]{8,12})", u or "")
            if m:
                out.add(m.group(1))
        return out

    def ingested_item_ids(self, collection_id: str) -> set[str]:
        """某集合(订阅)已入库过的 item_id 集合,供 source-adapter 通用去重。
        item_id 含义随来源而定(B站=bvid、youtube=videoId、rss=entry id 等)。"""
        rows = self._conn.execute(
            "SELECT item_id FROM ingested_items WHERE collection_id=?",
            (collection_id,),
        ).fetchall()
        return {r["item_id"] for r in rows}

    def mark_ingested(self, collection_id: str, item_id: str) -> None:
        """登记某集合已入库 item_id(幂等:重复 mark 不报错),同步成功后调。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ingested_items "
                "(collection_id, item_id, ingested_at) VALUES (?,?,?)",
                (collection_id, item_id, _now_iso()),
            )
            self._conn.commit()

    def increment_collection_count(self, collection_id: str, delta: int) -> None:
        """维护集合的 job_count:建/删 job 时增减;负值不下穿 0。"""
        if not collection_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE collections SET job_count = MAX(0, job_count + ?) WHERE id=?",
                (delta, collection_id),
            )
            self._conn.commit()

    # Glossary

    def upsert_glossary_term(
        self,
        domain: str,
        term: str,
        definition: str = "",
        related: list | None = None,
        status: str = "accepted",
    ) -> None:
        """写入/覆盖一条术语(手动维护入口):按 (domain, term) 幂等 upsert,
        保留已有 occurrences,覆盖 definition/related/status。
        related 元素可为字符串或 {term, rel},落库前归一为 [{term, rel}]。"""
        now = _now_iso()
        related_json = json.dumps(_norm_related(related), ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO glossary
                       (domain, term, definition, occurrences, related, status,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (domain, term, definition, "[]", related_json, status, now, now),
                )
            else:
                self._conn.execute(
                    """UPDATE glossary SET definition=?, related=?, status=?,
                       updated_at=? WHERE domain=? AND term=?""",
                    (definition, related_json, status, now, domain, term),
                )
            self._conn.commit()

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
        from shared.concepts import primary_fields, resolve

        term = (term or "").strip()
        if not term:
            return
        now = _now_iso()
        occ = {"job_id": job_id, "content_type": content_type, "location": location}
        cols = "term, occurrences, definition, definition_locked, zh_name, aliases, status"
        with self._lock:
            row = self._conn.execute(
                f"SELECT {cols} FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                # 归一匹配:域内行的 term/zh_name/aliases 归一键 vs 本建议的候选键。
                idx_rows = [
                    {"term": r["term"], "zh_name": r["zh_name"],
                     "aliases": json.loads(r["aliases"] or "[]")}
                    for r in self._conn.execute(
                        "SELECT term, zh_name, aliases FROM glossary "
                        "WHERE domain=? ORDER BY term", (domain,),
                    ).fetchall()
                ]
                hit = resolve(idx_rows, term, zh_name or None)
                if hit is not None:
                    row = self._conn.execute(
                        f"SELECT {cols} FROM glossary WHERE domain=? AND term=?",
                        (domain, hit),
                    ).fetchone()
            if row is not None and row["status"] == "rejected":
                return   # 已驳回的实体不再收 occurrence,也不改状态
            if row is None:
                p_term, p_zh, p_aliases = primary_fields(term, zh_name)
                self._conn.execute(
                    """INSERT INTO glossary
                       (domain, term, definition, zh_name, aliases, occurrences, related,
                        status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (domain, p_term, definition, p_zh,
                     json.dumps(p_aliases, ensure_ascii=False),
                     json.dumps([occ], ensure_ascii=False),
                     "[]", "suggested", now, now),
                )
            else:
                occs = json.loads(row["occurrences"] or "[]")
                changed = False
                if not any(o.get("job_id") == job_id for o in occs):
                    occs.append(occ)
                    changed = True
                new_def = row["definition"]
                # 候选定义补空:仅当本条还没定义且未钉住时填,不覆盖已有/已钉住。
                if definition and not (row["definition"] or "").strip() \
                        and not row["definition_locked"]:
                    new_def = definition
                    changed = True
                # 译名同定义策略:仅补空,不覆盖已有(人工定准/先到先得)。
                new_zh = row["zh_name"]
                if zh_name and not (row["zh_name"] or "").strip():
                    new_zh = zh_name
                    changed = True
                # 新变体名留痕进 aliases(与 term/zh_name 重复的不记)。
                aliases = json.loads(row["aliases"] or "[]")
                if term != row["term"] and term != (new_zh or "") and term not in aliases:
                    aliases.append(term)
                    changed = True
                # 自动晋升:suggested 且 occurrence 覆盖 ≥2 个不同 job → accepted
                # (跨内容复现 = 真概念的强信号;正文 term-link 高亮即时生效)。
                new_status = row["status"]
                if row["status"] == "suggested" \
                        and len({o.get("job_id") for o in occs if o.get("job_id")}) >= 2:
                    new_status = "accepted"
                    changed = True
                if changed:
                    self._conn.execute(
                        "UPDATE glossary SET occurrences=?, definition=?, zh_name=?, "
                        "aliases=?, status=?, updated_at=? WHERE domain=? AND term=?",
                        (json.dumps(occs, ensure_ascii=False), new_def, new_zh,
                         json.dumps(aliases, ensure_ascii=False), new_status,
                         now, domain, row["term"]),
                    )
            self._conn.commit()

    _STATUS_RANK = {"accepted": 2, "suggested": 1, "rejected": 0}

    def merge_glossary_terms(self, domain: str, src_term: str, dst_term: str) -> dict:
        """把 src 实体并入 dst,供存量清洗与前端"合并到已有词条"共用:
        occurrences 并集按 job_id 去重(dst 先)、definition 取更长者、zh_name 补空、
        src 的 term/zh_name/aliases 全部入 dst.aliases(可逆留痕)、status 取更高档
        (accepted > suggested > rejected)、is_topic/definition_locked 取或、related 并集。
        然后删 src 行。任一行不存在或 src==dst 抛 ValueError。返回合并后的行 dict。"""
        if src_term == dst_term:
            raise ValueError("src and dst are the same term")
        with self._lock:
            rows = {
                r["term"]: r for r in self._conn.execute(
                    "SELECT * FROM glossary WHERE domain=? AND term IN (?,?)",
                    (domain, src_term, dst_term),
                ).fetchall()
            }
            if src_term not in rows or dst_term not in rows:
                missing = src_term if src_term not in rows else dst_term
                raise ValueError(f"term not found: {missing}")
            s, d = rows[src_term], rows[dst_term]

            occs = json.loads(d["occurrences"] or "[]")
            seen_jobs = {o.get("job_id") for o in occs}
            for o in json.loads(s["occurrences"] or "[]"):
                if o.get("job_id") not in seen_jobs:
                    occs.append(o)
                    seen_jobs.add(o.get("job_id"))

            d_def, s_def = (d["definition"] or "").strip(), (s["definition"] or "").strip()
            definition = d_def if len(d_def) >= len(s_def) else s_def
            zh_name = (d["zh_name"] or "").strip() or (s["zh_name"] or "").strip()

            aliases = json.loads(d["aliases"] or "[]")
            for cand in (json.loads(s["aliases"] or "[]")
                         + [s["term"], (s["zh_name"] or "").strip()]):
                if cand and cand != dst_term and cand != zh_name and cand not in aliases:
                    aliases.append(cand)

            related = _norm_related(json.loads(d["related"] or "[]"))
            rel_terms = {r["term"] for r in related}
            for r in _norm_related(json.loads(s["related"] or "[]")):
                if r["term"] not in rel_terms:
                    related.append(r)
                    rel_terms.add(r["term"])

            rank = self._STATUS_RANK
            status = max((d["status"], s["status"]), key=lambda x: rank.get(x, 1))

            self._conn.execute(
                """UPDATE glossary SET definition=?, zh_name=?, aliases=?, occurrences=?,
                   related=?, status=?, is_topic=?, definition_locked=?, updated_at=?
                   WHERE domain=? AND term=?""",
                (definition, zh_name, json.dumps(aliases, ensure_ascii=False),
                 json.dumps(occs, ensure_ascii=False),
                 json.dumps(related, ensure_ascii=False), status,
                 1 if (d["is_topic"] or s["is_topic"]) else 0,
                 1 if (d["definition_locked"] or s["definition_locked"]) else 0,
                 _now_iso(), domain, dst_term),
            )
            self._conn.execute(
                "DELETE FROM glossary WHERE domain=? AND term=?", (domain, src_term)
            )
            self._conn.commit()
        merged = self.get_glossary_term(domain, dst_term)
        assert merged is not None
        return merged

    def add_glossary_relations(self, domain: str, term: str, relations: list[dict]) -> int:
        """给该概念并入关系边,供采集链与补边脚本共用:按目标 term 去重(先到先得,
        不覆盖已有 rel),自指跳过。行不存在返回 0(调用方应先 resolve 到主名)。返回新增边数。"""
        rels = [r for r in _norm_related(relations) if r["term"] != term]
        if not rels:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT related FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                return 0
            related = _norm_related(json.loads(row["related"] or "[]"))
            have = {r["term"] for r in related}
            added = 0
            for r in rels:
                if r["term"] not in have:
                    related.append(r)
                    have.add(r["term"])
                    added += 1
            if added:
                self._conn.execute(
                    "UPDATE glossary SET related=?, updated_at=? WHERE domain=? AND term=?",
                    (json.dumps(related, ensure_ascii=False), _now_iso(), domain, term),
                )
                self._conn.commit()
            return added

    def glossary_term_rows(self, domain: str) -> list[dict]:
        """术语一致性 L1 导出用:该域词条的 (term, zh_name, definition, aliases) 轻量行。
        rejected 不导出(驳回件不该再注入翻译);aliases 供导出层把英文别名也映射到同一译名。"""
        rows = self._conn.execute(
            "SELECT term, zh_name, definition, aliases FROM glossary "
            "WHERE domain=? AND status != 'rejected'", (domain,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["aliases"] = json.loads(d.get("aliases") or "[]")
            except (ValueError, TypeError):
                d["aliases"] = []
            out.append(d)
        return out

    def set_glossary_zh_name(self, domain: str, term: str, zh_name: str) -> bool:
        """backfill/人工定准写译名;返回是否更新(不存在的词条返回 False)。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE glossary SET zh_name=?, updated_at=? WHERE domain=? AND term=?",
                (zh_name, _now_iso(), domain, term),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_glossary_term(self, domain: str, term: str) -> dict | None:
        """读单条术语,未命中返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM glossary WHERE domain=? AND term=?", (domain, term)
        ).fetchone()
        return self._row_to_glossary(row) if row is not None else None

    def list_glossary(
        self, domain: str | None = None, status: str | None = None,
        q: str | None = None,
    ) -> list[dict]:
        """列术语,可按 domain / status 过滤 + q 检索(term/zh_name/aliases 子串,
        大小写不敏感),按 term 升序。status 未指定时默认排除 rejected。驳回件
        只在显式 status='rejected' 时可见)。"""
        where_parts: list[str] = []
        params: list = []
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if status:
            where_parts.append("status=?")
            params.append(status)
        else:
            where_parts.append("status != 'rejected'")
        if q and q.strip():
            # aliases 是 JSON 文本列,LIKE 子串足够(检索场景,无需精确解析)。
            like = f"%{q.strip()}%"
            where_parts.append("(term LIKE ? OR zh_name LIKE ? OR aliases LIKE ?)")
            params += [like, like, like]
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        rows = self._conn.execute(
            f"SELECT * FROM glossary {where} ORDER BY term", params
        ).fetchall()
        return [self._row_to_glossary(r) for r in rows]

    def get_job_titles(self, job_ids: list[str]) -> dict[str, str]:
        """批量取 job 标题(概念详情出现处 enrich 用):{job_id: title},缺 title 的 job 不返回。"""
        out: dict[str, str] = {}
        ids = [j for j in dict.fromkeys(job_ids) if j]
        for i in range(0, len(ids), 500):   # SQLite 变量数上限保护
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(
                f"SELECT id, title FROM jobs WHERE id IN ({ph})", chunk
            ).fetchall():
                if r["title"]:
                    out[r["id"]] = r["title"]
        return out

    def accept_glossary_term(self, domain: str, term: str) -> None:
        """采纳候选术语:status -> 'accepted'。"""
        with self._lock:
            self._conn.execute(
                "UPDATE glossary SET status='accepted', updated_at=? "
                "WHERE domain=? AND term=?",
                (_now_iso(), domain, term),
            )
            self._conn.commit()

    def reject_glossary_term(self, domain: str, term: str) -> bool:
        """驳回概念:status -> 'rejected'。行保留——采集链 resolve 命中 rejected 直接
        跳过,同名/变体不会再被重复建议;各消费面(列表/图谱/雷达/term_map)默认排除。
        命中返回 True,无该行返回 False(供路由判 404)。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE glossary SET status='rejected', updated_at=? "
                "WHERE domain=? AND term=?",
                (_now_iso(), domain, term),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_glossary_watched(self, domain: str, term: str, watched: bool) -> bool:
        """置概念 watch 标记。命中返回 True,无该行返回 False(供路由判 404)。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE glossary SET watched=?, updated_at=? WHERE domain=? AND term=?",
                (1 if watched else 0, _now_iso(), domain, term),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_glossary_topic(self, domain: str, term: str, is_topic: bool) -> bool:
        """置该词 is_topic(主题概念标记)。命中返回 True,无该行返回 False(供路由判 404)。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE glossary SET is_topic=?, updated_at=? WHERE domain=? AND term=?",
                (1 if is_topic else 0, _now_iso(), domain, term),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_topic_concepts(self, domain: str) -> list[dict]:
        """该 domain 中标为主题概念(is_topic=1,rejected 除外)的列表,按出现数降序;
        每项含 term/definition/occurrence_count/related/is_topic。空则 []。"""
        rows = self._conn.execute(
            "SELECT term, definition, occurrences, related, is_topic "
            "FROM glossary WHERE domain=? AND is_topic=1 AND status != 'rejected'",
            (domain,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            try:
                related = _norm_related(json.loads(r["related"] or "[]"))
            except (ValueError, TypeError):
                related = []
            out.append({
                "term": r["term"],
                "definition": r["definition"] or "",
                "occurrence_count": len(occs) if isinstance(occs, list) else 0,
                "related": related,
                "is_topic": True,
            })
        out.sort(key=lambda t: t["occurrence_count"], reverse=True)
        return out

    def delete_glossary_term(self, domain: str, term: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM glossary WHERE domain=? AND term=?", (domain, term)
            )
            self._conn.commit()

    # Notes 全文索引 (FTS5)

    def list_unindexed_done_jobs(self, limit: int = 100) -> list[Job]:
        """返回尚无任何全文索引的当前已完成 job,供 scheduler 幂等补账。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM jobs
                   WHERE status='done' AND is_current=1
                     AND NOT EXISTS (
                       SELECT 1 FROM notes_fts5 WHERE notes_fts5.job_id=jobs.id
                     )
                   ORDER BY created_at ASC LIMIT ?""",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

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
    ) -> None:
        """原子替换某 job/note_type 的全文与证据块索引,失败时保留旧版本。"""
        with self._lock:
            # notes_fts5 与两张 chunk 表是一个可见版本。任一写入失败都回滚删除和
            # 已插入行,避免后续无关 commit 固化半成品;同一输入可安全重试。
            with self._conn:
                for stale_type in set(supersede_note_types or []) - {note_type}:
                    self._conn.execute(
                        "DELETE FROM notes_fts5 WHERE job_id=? AND note_type=?",
                        (job_id, stale_type),
                    )
                    self._conn.execute(
                        "DELETE FROM note_chunks WHERE job_id=? AND note_type=?",
                        (job_id, stale_type),
                    )
                    self._conn.execute(
                        "DELETE FROM note_chunks_fts5 WHERE job_id=? AND note_type=?",
                        (job_id, stale_type),
                    )
                self._conn.execute(
                    "DELETE FROM notes_fts5 WHERE job_id=? AND note_type=?",
                    (job_id, note_type),
                )
                self._conn.execute(
                    """INSERT INTO notes_fts5
                       (job_id, content_type, note_type, collection_id, domain,
                        title, body)
                       VALUES (?,?,?,?,?,?,?)""",
                    (job_id, content_type, note_type, collection_id or "",
                     domain or "", title or "", body or ""),
                )
                self._replace_note_chunks_locked(
                    job_id=job_id,
                    note_type=note_type,
                    title=title,
                    body=body,
                    content_type=content_type,
                    domain=domain,
                    collection_id=collection_id,
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
        self._conn.execute(
            "DELETE FROM note_chunks WHERE job_id=? AND note_type=?", (job_id, note_type)
        )
        self._conn.execute(
            "DELETE FROM note_chunks_fts5 WHERE job_id=? AND note_type=?",
            (job_id, note_type),
        )
        now = _now_iso()
        for idx, chunk in enumerate(_chunk_note_body(body)):
            chunk_id = f"{job_id}:{note_type}:{idx}"
            evidence = {
                "chunk_id": chunk_id,
                "note_type": note_type,
                "section": chunk["section"],
                "chunk_index": idx,
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
                "timestamp_sec": None,
                "page": None,
                "frame_path": None,
                "image_path": None,
            }
            evidence_json = json.dumps(evidence, ensure_ascii=False)
            values = (
                chunk_id, job_id, note_type, content_type or "", collection_id or "",
                domain or "", title or "", chunk["section"], idx,
                chunk["char_start"], chunk["char_end"], chunk["body"], evidence_json,
                now, now,
            )
            self._conn.execute(
                """INSERT INTO note_chunks
                   (chunk_id, job_id, note_type, content_type, collection_id, domain,
                    title, section, chunk_index, char_start, char_end, body,
                    evidence_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            self._conn.execute(
                """INSERT INTO note_chunks_fts5
                   (chunk_id, job_id, note_type, content_type, collection_id, domain,
                    title, section, body, evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk_id, job_id, note_type, content_type or "", collection_id or "",
                    domain or "", title or "", chunk["section"], chunk["body"], evidence_json,
                ),
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
        """全文检索笔记。q 走 fts5 MATCH(trigram,中文子串友好),做基本转义防注入;
        可按 collection_id / domain / content_type 收窄。返回 (total, items),
        items 含 job_id/note_type/title/snippet/content_type/domain/collection_id。
        注意:trigram 至少需 3 个字符才能命中,更短的查询会无结果。"""
        match = _fts_match_query(q)
        if not match:
            return 0, []

        where_parts = ["notes_fts5 MATCH ?"]
        params: list = [match]
        if collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if content_type:
            where_parts.append("content_type=?")
            params.append(content_type)
        where = " AND ".join(where_parts)

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM notes_fts5 WHERE {where}", params
        ).fetchone()[0]

        # snippet(表, 列号 6=body, 高亮包裹, 省略号, 单片最多 12 token)。
        rows = self._conn.execute(
            f"""SELECT job_id, note_type, title, content_type, domain,
                   collection_id,
                   snippet(notes_fts5, 6, '<mark>', '</mark>', '…', 12) AS snippet
                FROM notes_fts5 WHERE {where}
                ORDER BY rank LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        items = [
            {
                "job_id": r["job_id"],
                "note_type": r["note_type"],
                "title": r["title"],
                "snippet": r["snippet"],
                "content_type": r["content_type"],
                "domain": r["domain"],
                "collection_id": r["collection_id"] or None,
            }
            for r in rows
        ]
        return total, items

    def search_note_chunks(
        self,
        q: str,
        collection_id: str | None = None,
        domain: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """全文检索问答证据块。返回 chunk 级 body/snippet/evidence,供 Ask 使用。"""
        match = _fts_match_query(q)
        if not match:
            return 0, []

        where_parts = ["note_chunks_fts5 MATCH ?"]
        params: list = [match]
        if collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if content_type:
            where_parts.append("content_type=?")
            params.append(content_type)
        where = " AND ".join(where_parts)

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM note_chunks_fts5 WHERE {where}", params
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""SELECT chunk_id, job_id, note_type, title, content_type, domain,
                   collection_id, section, body, evidence_json,
                   snippet(note_chunks_fts5, 8, '<mark>', '</mark>', '…', 12) AS snippet
                FROM note_chunks_fts5 WHERE {where}
                ORDER BY rank LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            try:
                evidence = json.loads(r["evidence_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                evidence = {}
            items.append({
                "chunk_id": r["chunk_id"],
                "job_id": r["job_id"],
                "note_type": r["note_type"],
                "title": r["title"],
                "snippet": r["snippet"],
                "body": r["body"],
                "content_type": r["content_type"],
                "domain": r["domain"],
                "collection_id": r["collection_id"] or None,
                "section": r["section"] or "",
                "evidence": evidence,
            })
        return total, items

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
        normalized_domain = domain.strip() if isinstance(domain, str) else ""
        normalized_front = front.strip() if isinstance(front, str) else ""
        normalized_back = back.strip() if isinstance(back, str) else ""
        normalized_source = source.strip() if isinstance(source, str) else ""
        if not normalized_domain or not normalized_front or not normalized_back:
            raise ValueError("domain/front/back 不能为空")
        if not normalized_source:
            raise ValueError("source 不能为空")
        if status not in STUDY_STATUSES:
            raise ValueError("invalid study card status")
        now_dt = utc_now()
        now = now_dt.isoformat()
        initial_due = due_at or now_dt
        due_iso = canonical_utc_iso(initial_due, "due_at")
        due_epoch = datetime_to_epoch_us(initial_due, "due_at")
        evidence_json = json.dumps(evidence if evidence is not None else [], ensure_ascii=False)
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO study_cards
                       (card_id, domain, job_id, concept_term, card_type, front, back,
                        explanation, evidence_json, status, source, revision,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        card_id, normalized_domain, job_id or None, concept_term or None,
                        card_type, normalized_front, normalized_back, explanation or "",
                        evidence_json, status, normalized_source, 1, now, now,
                    ),
                )
                if status == "active":
                    self._conn.execute(
                        """INSERT INTO study_reviews
                           (card_id, due_at, due_at_epoch_us, interval_days, ease,
                            repetitions, lapses, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (card_id, due_iso, due_epoch, 0, 2.5, 0, 0, now),
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        card = self.get_study_card(card_id)
        if card is None:
            raise RuntimeError("study card insert failed")
        return card

    def get_study_card(self, card_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                          c.front, c.back, c.explanation, c.evidence_json, c.status,
                          c.source, c.revision, c.created_at, c.updated_at,
                          r.due_at AS review_due_at, r.interval_days, r.ease,
                          r.repetitions, r.lapses, r.last_grade, r.last_reviewed_at,
                          r.updated_at AS review_updated_at
                   FROM study_cards c
                   LEFT JOIN study_reviews r ON r.card_id = c.card_id
                   WHERE c.card_id=?""",
                (card_id,),
            ).fetchone()
        return self._row_to_study_card(row) if row else None

    def list_study_cards(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        where_parts = ["1=1"]
        params: list = []
        if domain:
            where_parts.append("c.domain=?")
            params.append(domain)
        if status:
            where_parts.append("c.status=?")
            params.append(status)
        if q:
            like = f"%{q}%"
            where_parts.append(
                "(c.front LIKE ? OR c.back LIKE ? OR c.explanation LIKE ? OR c.concept_term LIKE ?)"
            )
            params.extend([like, like, like, like])
        where = " AND ".join(where_parts)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM study_cards c WHERE {where}", params,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                           c.front, c.back, c.explanation, c.evidence_json, c.status,
                           c.source, c.revision, c.created_at, c.updated_at,
                           r.due_at AS review_due_at, r.interval_days, r.ease,
                           r.repetitions, r.lapses, r.last_grade, r.last_reviewed_at,
                           r.updated_at AS review_updated_at
                    FROM study_cards c
                    LEFT JOIN study_reviews r ON r.card_id = c.card_id
                    WHERE {where}
                    ORDER BY c.updated_at DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return total, [self._row_to_study_card(r) for r in rows]

    def list_due_study_cards(
        self,
        *,
        domain: str | None = None,
        now: datetime | str | None = None,
        now_iso: str | None = None,
        limit: int = 50,
    ) -> tuple[int, list[dict]]:
        if now is not None and now_iso is not None:
            raise ValueError("now 与 now_iso 不能同时传入")
        current = now if now is not None else now_iso if now_iso is not None else utc_now()
        current_epoch = datetime_to_epoch_us(current, "now")
        where_parts = ["c.status='active'", "r.due_at_epoch_us<=?"]
        params: list = [current_epoch]
        if domain:
            where_parts.append("c.domain=?")
            params.append(domain)
        where = " AND ".join(where_parts)
        with self._lock:
            total = self._conn.execute(
                f"""SELECT COUNT(*) FROM study_cards c
                    JOIN study_reviews r ON r.card_id = c.card_id
                    WHERE {where}""",
                params,
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                           c.front, c.back, c.explanation, c.evidence_json, c.status,
                           c.source, c.revision, c.created_at, c.updated_at,
                           r.due_at AS review_due_at, r.interval_days, r.ease,
                           r.repetitions, r.lapses, r.last_grade, r.last_reviewed_at,
                           r.updated_at AS review_updated_at
                    FROM study_cards c
                    JOIN study_reviews r ON r.card_id = c.card_id
                    WHERE {where}
                    ORDER BY r.due_at_epoch_us ASC, c.created_at ASC
                    LIMIT ?""",
                params + [limit],
            ).fetchall()
        return total, [self._row_to_study_card(r) for r in rows]

    def set_study_card_status(
        self,
        card_id: str,
        status: str,
        *,
        expected_revision: int,
    ) -> dict:
        if status not in STUDY_STATUSES:
            raise ValueError("invalid study card status")
        if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_SQLITE_INTEGER:
            raise ValueError("expected_revision 必须是 SQLite 64 位正整数")
        now_dt = utc_now()
        now = now_dt.isoformat()
        now_epoch = datetime_to_epoch_us(now_dt)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT status, revision FROM study_cards WHERE card_id=?", (card_id,)
                ).fetchone()
                if row is None:
                    raise StudyNotFoundError("card not found")
                current_status = str(row["status"])
                if current_status == status:
                    self._conn.commit()
                    card = self.get_study_card(card_id)
                    if card is None:
                        raise StudyNotFoundError("card not found")
                    return card
                allowed = {
                    ("active", "suspended"),
                    ("suspended", "active"),
                    ("suggested", "rejected"),
                }
                if (current_status, status) not in allowed:
                    raise StudyConflictError(
                        "study_status_transition_invalid",
                        f"study card cannot transition from {current_status} to {status}",
                    )
                if int(row["revision"]) != expected_revision:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                if expected_revision == MAX_SQLITE_INTEGER:
                    raise StudyConflictError(
                        "study_revision_exhausted",
                        "study card revision exhausted SQLite integer range",
                    )
                changed = self._conn.execute(
                    """UPDATE study_cards SET status=?, revision=revision+1, updated_at=?
                       WHERE card_id=? AND revision=? AND status=?""",
                    (status, now, card_id, expected_revision, current_status),
                )
                if changed.rowcount != 1:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                if status == "active":
                    self._conn.execute(
                        """INSERT OR IGNORE INTO study_reviews
                           (card_id, due_at, due_at_epoch_us, interval_days, ease,
                            repetitions, lapses, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (card_id, now, now_epoch, 0, 2.5, 0, 0, now),
                    )
                self._conn.commit()
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        card = self.get_study_card(card_id)
        if card is None:
            raise StudyNotFoundError("card not found")
        return card

    def delete_study_card(self, card_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM study_cards WHERE card_id=?", (card_id,))
            self._conn.commit()
            return cur.rowcount > 0

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
        normalized_request_id, normalized_grade = validate_review_request(
            request_id=request_id,
            card_id=card_id,
            grade=grade,
            response_ms=response_ms,
            expected_revision=expected_revision,
        )
        fingerprint = review_request_fingerprint(
            card_id=card_id,
            grade=normalized_grade,
            response_ms=response_ms,
            expected_revision=expected_revision,
        )
        reviewed_dt = utc_now() if reviewed_at is None else require_aware_utc(
            reviewed_at, "reviewed_at"
        )
        reviewed_iso = reviewed_dt.isoformat()
        reviewed_epoch = datetime_to_epoch_us(reviewed_dt, "reviewed_at")

        def inject(stage: str) -> None:
            if fault_injector is not None:
                fault_injector(stage)

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    """SELECT request_fingerprint, outcome_json
                       FROM study_review_logs WHERE request_id=?""",
                    (normalized_request_id,),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != fingerprint:
                        raise StudyConflictError(
                            "study_request_id_conflict",
                            "request_id was already used with a different payload",
                        )
                    outcome = json.loads(existing["outcome_json"])
                    self._conn.commit()
                    return outcome

                row = self._conn.execute(
                    """SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                              c.front, c.back, c.explanation, c.evidence_json, c.status,
                              c.source, c.revision, c.created_at, c.updated_at,
                              r.due_at AS review_due_at, r.due_at_epoch_us,
                              r.interval_days, r.ease, r.repetitions, r.lapses,
                              r.last_grade, r.last_reviewed_at,
                              r.updated_at AS review_updated_at
                       FROM study_cards c
                       LEFT JOIN study_reviews r ON r.card_id=c.card_id
                       WHERE c.card_id=?""",
                    (card_id,),
                ).fetchone()
                if row is None:
                    raise StudyNotFoundError("card not found")
                if row["status"] != "active":
                    raise StudyConflictError(
                        "study_card_not_active", "only active study cards can be reviewed"
                    )
                if int(row["revision"]) != expected_revision:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                if expected_revision == MAX_SQLITE_INTEGER:
                    raise StudyConflictError(
                        "study_revision_exhausted",
                        "study card revision exhausted SQLite integer range",
                    )
                card = self._row_to_study_card(row)
                schedule = schedule_next_review(card, normalized_grade, reviewed_dt)
                scheduled_due_at = row["review_due_at"]
                scheduled_due_epoch = row["due_at_epoch_us"]
                changed = self._conn.execute(
                    """UPDATE study_cards SET revision=revision+1, updated_at=?
                       WHERE card_id=? AND status='active' AND revision=?""",
                    (reviewed_iso, card_id, expected_revision),
                )
                if changed.rowcount != 1:
                    raise StudyConflictError(
                        "study_revision_stale", "study card revision is stale"
                    )
                inject("after_card_cas")
                self._conn.execute(
                    """INSERT INTO study_reviews
                       (card_id, due_at, due_at_epoch_us, interval_days, ease,
                        repetitions, lapses, last_grade, last_reviewed_at,
                        last_reviewed_at_epoch_us, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(card_id) DO UPDATE SET
                         due_at=excluded.due_at,
                         due_at_epoch_us=excluded.due_at_epoch_us,
                         interval_days=excluded.interval_days,
                         ease=excluded.ease,
                         repetitions=excluded.repetitions,
                         lapses=excluded.lapses,
                         last_grade=excluded.last_grade,
                         last_reviewed_at=excluded.last_reviewed_at,
                         last_reviewed_at_epoch_us=excluded.last_reviewed_at_epoch_us,
                         updated_at=excluded.updated_at""",
                    (
                        card_id,
                        schedule["next_due_at"],
                        schedule["next_due_at_epoch_us"],
                        schedule["interval_days"],
                        schedule["ease"],
                        schedule["repetitions"],
                        schedule["lapses"],
                        normalized_grade,
                        reviewed_iso,
                        reviewed_epoch,
                        reviewed_iso,
                    ),
                )
                inject("after_review")
                updated_row = self._conn.execute(
                    """SELECT c.card_id, c.domain, c.job_id, c.concept_term, c.card_type,
                              c.front, c.back, c.explanation, c.evidence_json, c.status,
                              c.source, c.revision, c.created_at, c.updated_at,
                              r.due_at AS review_due_at, r.due_at_epoch_us,
                              r.interval_days, r.ease, r.repetitions, r.lapses,
                              r.last_grade, r.last_reviewed_at,
                              r.updated_at AS review_updated_at
                       FROM study_cards c JOIN study_reviews r ON r.card_id=c.card_id
                       WHERE c.card_id=?""",
                    (card_id,),
                ).fetchone()
                if updated_row is None:
                    raise RuntimeError("study review update disappeared inside transaction")
                outcome = self._row_to_study_card(updated_row)
                outcome_json = json.dumps(
                    outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                self._conn.execute(
                    """INSERT INTO study_review_logs
                       (id, card_id, request_id, request_fingerprint, grade, reviewed_at,
                        reviewed_at_epoch_us, response_ms, scheduled_due_at,
                        scheduled_due_at_epoch_us, next_due_at, next_due_at_epoch_us,
                        interval_days, ease, repetitions, lapses, revision_before,
                        revision_after, outcome_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"srl_{uuid.uuid4().hex}", card_id, normalized_request_id,
                        fingerprint, normalized_grade, reviewed_iso, reviewed_epoch,
                        response_ms, scheduled_due_at, scheduled_due_epoch,
                        schedule["next_due_at"], schedule["next_due_at_epoch_us"],
                        schedule["interval_days"], schedule["ease"],
                        schedule["repetitions"], schedule["lapses"],
                        expected_revision, expected_revision + 1, outcome_json,
                    ),
                )
                inject("after_log")
                inject("before_commit")
                self._conn.commit()
                return outcome
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def get_study_stats(
        self,
        *,
        domain: str | None = None,
        now: datetime | str | None = None,
    ) -> dict:
        """单次 CTE 从已提交事实聚合卡片,到期,评分和留存统计."""
        now_epoch = datetime_to_epoch_us(now or utc_now(), "now")
        with self._lock:
            row = self._conn.execute(
                """WITH filtered_cards AS (
                   SELECT card_id, status FROM study_cards
                   WHERE (? IS NULL OR domain=?)
                 ),
                 card_totals AS (
                   SELECT COUNT(*) AS total,
                          COALESCE(SUM(status='suggested'),0) AS suggested,
                          COALESCE(SUM(status='active'),0) AS active,
                          COALESCE(SUM(status='suspended'),0) AS suspended,
                          COALESCE(SUM(status='rejected'),0) AS rejected
                   FROM filtered_cards
                 ),
                 due_totals AS (
                   SELECT COUNT(*) AS due
                   FROM filtered_cards c JOIN study_reviews r USING(card_id)
                   WHERE c.status='active' AND r.due_at_epoch_us<=?
                 ),
                 log_totals AS (
                   SELECT COUNT(l.id) AS reviews_total,
                          COUNT(DISTINCT l.card_id) AS reviewed_cards,
                          COALESCE(SUM(l.grade='again'),0) AS again_count,
                          COALESCE(SUM(l.grade='hard'),0) AS hard_count,
                          COALESCE(SUM(l.grade='good'),0) AS good_count,
                          COALESCE(SUM(l.grade='easy'),0) AS easy_count
                   FROM filtered_cards c
                   LEFT JOIN study_review_logs l USING(card_id)
                 )
                     SELECT * FROM card_totals CROSS JOIN due_totals CROSS JOIN log_totals""",
                (domain, domain, now_epoch),
            ).fetchone()
        reviews_total = int(row["reviews_total"])
        retained = int(row["hard_count"]) + int(row["good_count"]) + int(row["easy_count"])
        return {
            "total": int(row["total"]),
            "statuses": {
                "suggested": int(row["suggested"]),
                "active": int(row["active"]),
                "suspended": int(row["suspended"]),
                "rejected": int(row["rejected"]),
            },
            "due": int(row["due"]),
            "reviewed_cards": int(row["reviewed_cards"]),
            "reviews_total": reviews_total,
            "grades": {
                "again": int(row["again_count"]),
                "hard": int(row["hard_count"]),
                "good": int(row["good_count"]),
                "easy": int(row["easy_count"]),
            },
            "retained_reviews": retained,
            "retention_rate": round(retained / reviews_total, 4) if reviews_total else 0.0,
        }

    # Private

    def _row_to_study_card(self, row: sqlite3.Row) -> dict:
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            evidence = []
        review = None
        if row["review_due_at"] is not None:
            review = {
                "due_at": row["review_due_at"],
                "interval_days": row["interval_days"],
                "ease": row["ease"],
                "repetitions": row["repetitions"],
                "lapses": row["lapses"],
                "last_grade": row["last_grade"],
                "last_reviewed_at": row["last_reviewed_at"],
                "updated_at": row["review_updated_at"],
            }
        return {
            "card_id": row["card_id"],
            "domain": row["domain"],
            "job_id": row["job_id"],
            "concept_term": row["concept_term"],
            "card_type": row["card_type"],
            "front": row["front"],
            "back": row["back"],
            "explanation": row["explanation"],
            "evidence": evidence,
            "status": row["status"],
            "source": row["source"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "review": review,
        }

    def _row_to_glossary(self, row: sqlite3.Row) -> dict:
        return {
            "domain": row["domain"],
            "term": row["term"],
            "definition": row["definition"],
            "zh_name": (row["zh_name"] if "zh_name" in row.keys() else "") or "",
            "aliases": json.loads(
                (row["aliases"] if "aliases" in row.keys() else "") or "[]"
            ),
            "occurrences": json.loads(row["occurrences"] or "[]"),
            # 规范形态 [{term, rel}];存量字符串元素在读出时归一(rel='related')。
            "related": _norm_related(json.loads(row["related"] or "[]")),
            "status": row["status"],
            "watched": bool(row["watched"] if "watched" in row.keys() else 0),
            "is_topic": bool(row["is_topic"]),
            "definition_locked": bool(row["definition_locked"]),
            "created_at": _parse_dt(row["created_at"]),
            "updated_at": _parse_dt(row["updated_at"]),
        }

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            content_type=row["content_type"],
            pipeline=row["pipeline"],
            collection_id=row["collection_id"],
            url=row["url"],
            title=row["title"],
            domain=row["domain"],
            source=row["source"],
            style_tags=json.loads(row["style_tags"]),
            status=JobStatus(row["status"]),
            progress_pct=row["progress_pct"],
            meta=json.loads(row["meta"]),
            published_at=_parse_dt(row["published_at"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            error=row["error"],
            lineage_key=row["lineage_key"],
            is_current=bool(row["is_current"]),
            source_digest=row["source_digest"],
            pipeline_digest=row["pipeline_digest"],
            parent_job_id=row["parent_job_id"],
        )

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        return Step(
            job_id=row["job_id"],
            name=row["step"],
            status=StepStatus(row["status"]),
            pool=row["pool"],
            input_hash=row["input_hash"],
            worker_id=row["worker_id"],
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            duration_sec=row["duration_sec"],
            meta=json.loads(row["meta"]) if row["meta"] else {},
            error=row["error"],
            retries=row["retries"],
        )

    def _row_to_worker(self, row: sqlite3.Row) -> Worker:
        return Worker(
            id=row["id"],
            type=row["type"],
            pools=json.loads(row["pools"]),
            tags=set(json.loads(row["tags"])),
            reject_tags=set(json.loads(row["reject_tags"])),
            hostname=row["hostname"],
            gpu_name=row["gpu_name"],
            gpu_memory_mb=row["gpu_memory_mb"],
            concurrency=row["concurrency"] if "concurrency" in row.keys() else 1,
            remote_addr=row["remote_addr"] if "remote_addr" in row.keys() else None,
            status=row["status"],
            admin_status=row["admin_status"] if "admin_status" in row.keys() else "",
            current_job=row["current_job"],
            current_step=row["current_step"],
            tasks_completed=row["tasks_completed"],
            tasks_failed=row["tasks_failed"],
            total_duration_sec=row["total_duration_sec"],
            first_seen=_parse_dt(row["first_seen"]),
            started_at=_parse_dt(row["started_at"]),
            last_heartbeat=_parse_dt(row["last_heartbeat"]),
            admin_note=row["admin_note"],
            desired_config=(
                json.loads(row["desired_config"])
                if "desired_config" in row.keys() and row["desired_config"] else None
            ),
            cfg_rev=(row["cfg_rev"] or 0) if "cfg_rev" in row.keys() else 0,
        )
