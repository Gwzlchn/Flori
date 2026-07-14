"""按不可变清单事务化执行 SQLite 迁移。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


DEFAULT_MANIFEST_PATH = Path(__file__).with_name("manifest.json")
MANIFEST_FORMAT = "flori-sqlite-migrations"
LEDGER_TABLE = "schema_migrations"


class SchemaCompatibilityError(RuntimeError):
    """schema 版本或迁移清单无法由当前程序安全处理。"""


class UnsupportedSchemaVersionError(SchemaCompatibilityError):
    """数据库 schema 不在当前程序支持范围内。"""


class MigrationHistoryError(SchemaCompatibilityError):
    """已应用迁移记录与不可变清单不一致。"""


class MigrationExecutionError(RuntimeError):
    """单步迁移失败，当前事务已回滚。"""


@dataclass(frozen=True)
class Migration:
    """一个从 version-1 到 version 的不可变迁移。"""

    version: int
    name: str
    payload: str
    apply: Callable[[sqlite3.Connection], None]
    validate: Callable[[sqlite3.Connection], None] | None = None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.payload.encode("utf-8")).hexdigest()


def _require_plain_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise MigrationHistoryError(f"{field} 必须是整数")
    return value


def load_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> dict:
    """读取并校验迁移清单形状，返回可安全比对的数据。"""
    manifest_path = Path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationHistoryError(f"无法读取迁移清单 {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise MigrationHistoryError("迁移清单 format 不匹配")
    minimum = _require_plain_int(
        manifest.get("minimum_supported_version"), "minimum_supported_version"
    )
    current = _require_plain_int(manifest.get("current_version"), "current_version")
    ledger = _require_plain_int(manifest.get("ledger_version"), "ledger_version")
    if minimum != 0 or current < 1 or not 1 <= ledger <= current:
        raise MigrationHistoryError("迁移清单版本边界非法")
    entries = manifest.get("migrations")
    if not isinstance(entries, list) or len(entries) != current:
        raise MigrationHistoryError("迁移清单必须覆盖 1..current_version")
    for expected, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise MigrationHistoryError("迁移清单条目必须是对象")
        if _require_plain_int(entry.get("version"), "migration.version") != expected:
            raise MigrationHistoryError("迁移版本必须从 1 连续递增")
        name = entry.get("name")
        checksum = entry.get("checksum")
        if not isinstance(name, str) or not name.strip():
            raise MigrationHistoryError("迁移 name 不能为空")
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(ch not in "0123456789abcdef" for ch in checksum)
        ):
            raise MigrationHistoryError("迁移 checksum 必须是小写 sha256")
    return manifest


def migration_manifest_fingerprint(path: Path | str = DEFAULT_MANIFEST_PATH) -> str:
    """计算语义化清单指纹，灾备 manifest 用它固定生产者迁移集。"""
    manifest = load_manifest(path)
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_schema_version(path: Path | str = DEFAULT_MANIFEST_PATH) -> int:
    return int(load_manifest(path)["current_version"])


def validate_registry(
    migrations: Sequence[Migration],
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
) -> dict:
    """在触碰数据库前验证代码注册表与清单完全一致。"""
    manifest = load_manifest(manifest_path)
    entries = manifest["migrations"]
    if len(migrations) != len(entries):
        raise MigrationHistoryError("代码迁移数与 manifest 不一致")
    for expected, (migration, entry) in enumerate(zip(migrations, entries), start=1):
        if type(migration.version) is not int:
            raise MigrationHistoryError("代码迁移 version 必须是整数")
        if migration.version != expected:
            raise MigrationHistoryError("代码迁移必须从 1 连续递增")
        if migration.name != entry["name"]:
            raise MigrationHistoryError(
                f"迁移 v{expected} name 与 manifest 不一致"
            )
        if migration.checksum != entry["checksum"]:
            raise MigrationHistoryError(
                f"迁移 v{expected} checksum 与 manifest 不一致"
            )
    return manifest


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def assert_schema_compatible(
    connection: sqlite3.Connection,
    *,
    minimum_version: int = 0,
    maximum_version: int,
) -> int:
    """只读判断 schema 范围，不在范围内时不执行任何 PRAGMA 写。"""
    version = schema_version(connection)
    if version < minimum_version or version > maximum_version:
        raise UnsupportedSchemaVersionError(
            f"SQLite user_version={version} 不在当前程序支持范围 "
            f"{minimum_version}..{maximum_version}"
        )
    return version


def _ledger_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (LEDGER_TABLE,)
    ).fetchone()
    return row is not None


def _record_history(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
    through_version: int,
) -> None:
    if not _ledger_exists(connection):
        return
    applied_at = datetime.now(timezone.utc).isoformat()
    for migration in migrations[:through_version]:
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations "
            "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, migration.checksum, applied_at),
        )


def _validate_history(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    current_version: int,
    ledger_version: int,
) -> None:
    if current_version < ledger_version:
        return
    if not _ledger_exists(connection):
        raise MigrationHistoryError(
            f"schema v{current_version} 缺少 {LEDGER_TABLE}"
        )
    try:
        rows = connection.execute(
            "SELECT version, name, checksum, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise MigrationHistoryError(f"{LEDGER_TABLE} 结构非法: {exc}") from exc
    if len(rows) != current_version:
        raise MigrationHistoryError(
            f"{LEDGER_TABLE} 必须精确覆盖 1..{current_version}"
        )
    for expected, row in enumerate(rows, start=1):
        migration = migrations[expected - 1]
        if (
            int(row[0]) != expected
            or row[1] != migration.name
            or row[2] != migration.checksum
            or not isinstance(row[3], str)
            or not row[3].strip()
        ):
            raise MigrationHistoryError(
                f"{LEDGER_TABLE} v{expected} 与不可变迁移清单不一致"
            )


def _validate_schema_invariants(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
    current_version: int,
) -> None:
    if current_version == 0:
        return
    migration = migrations[current_version - 1]
    if migration.validate is None:
        raise MigrationHistoryError(
            f"迁移 v{current_version} 缺少完整 current-schema validator"
        )
    migration.validate(connection)


def run_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    target_version: int | None = None,
    fault_injector: Callable[[int, sqlite3.Connection], None] | None = None,
) -> int:
    """逐版校验并在一个事务内原子提交整个 pending chain。"""
    manifest = validate_registry(migrations, manifest_path)
    current_limit = int(manifest["current_version"])
    ledger_version = int(manifest["ledger_version"])
    target = current_limit if target_version is None else target_version
    if type(target) is not int or not 0 <= target <= current_limit:
        raise UnsupportedSchemaVersionError(
            f"目标 schema 版本 {target!r} 超出 0..{current_limit}"
        )
    initial = assert_schema_compatible(
        connection, minimum_version=0, maximum_version=target
    )
    _validate_history(
        connection,
        migrations,
        current_version=initial,
        ledger_version=ledger_version,
    )
    if initial == target:
        _validate_schema_invariants(connection, migrations, initial)
        return initial

    # 一个 pending chain 是一个原子升级单元。逐版 validate 仍在链内执行，
    # 但任何后段失败都会撤销 initial 之后的全部 DDL、数据、ledger 与版本戳。
    migration: Migration | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        actual = schema_version(connection)
        if actual != initial:
            raise MigrationHistoryError(
                f"迁移开始前 schema 从 v{initial} 变为 v{actual}"
            )
        while actual < target:
            migration = migrations[actual]
            if migration.version != actual + 1:
                raise MigrationHistoryError(
                    f"缺少 {actual} -> {actual + 1} 迁移"
                )
            migration.apply(connection)
            if fault_injector is not None:
                fault_injector(migration.version, connection)
            connection.execute(f"PRAGMA user_version = {migration.version}")
            _record_history(connection, migrations, migration.version)
            # INSERT OR IGNORE 只用于回填旧版记录，冲突不得被静默接受。
            # 必须在同一事务 commit 前验证精确历史。
            _validate_history(
                connection,
                migrations,
                current_version=migration.version,
                ledger_version=ledger_version,
            )
            _validate_schema_invariants(
                connection, migrations, migration.version
            )
            actual = migration.version
        connection.commit()
    except BaseException as exc:
        if connection.in_transaction:
            connection.rollback()
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, SchemaCompatibilityError):
            raise
        version = migration.version if migration is not None else initial + 1
        name = migration.name if migration is not None else "unknown"
        raise MigrationExecutionError(
            f"迁移 v{version}({name}) 失败，pending chain 已回滚到 v{initial}: {exc}"
        ) from exc

    final = schema_version(connection)
    _validate_history(
        connection,
        migrations,
        current_version=final,
        ledger_version=ledger_version,
    )
    _validate_schema_invariants(connection, migrations, final)
    return final
