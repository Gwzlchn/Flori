"""为不验收迁移语义的测试提供 current-schema 空库副本。"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from shared.db import Database, SCHEMA_VERSION
from shared.migrations import migration_steps, validate_registry


_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    """返回已存在的 SQLite 未落盘 sidecar。"""
    return tuple(
        candidate
        for suffix in _SQLITE_SIDECAR_SUFFIXES
        if (candidate := Path(f"{path}{suffix}")).exists()
    )


def build_current_schema_template(path: Path) -> Path:
    """构建并完整校验一个已关闭的 current-schema 空库。"""
    database = Database(path)
    try:
        database.init_schema()
    finally:
        database.close()

    sidecars = sqlite_sidecars(path)
    if sidecars:
        raise AssertionError(f"current-schema 模板含未落盘 sidecar: {sidecars}")

    migrations = migration_steps()
    validate_registry(migrations)
    connection = sqlite3.connect(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        ledger = connection.execute(
            "SELECT version, name, checksum, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
        expected = [
            (migration.version, migration.name, migration.checksum)
            for migration in migrations
        ]
        actual = [(int(row[0]), row[1], row[2]) for row in ledger]
        if version != SCHEMA_VERSION or integrity != "ok" or actual != expected:
            raise AssertionError(
                "current-schema 模板不完整: "
                f"version={version}, integrity={integrity}, ledger={actual}"
            )
        if any(not isinstance(row[3], str) or not row[3].strip() for row in ledger):
            raise AssertionError("current-schema 模板 ledger applied_at 不完整")
        validator = migrations[-1].validate
        if validator is None:
            raise AssertionError("current-schema 最新迁移缺少完整 validator")
        validator(connection)
    finally:
        # 模板以单文件形式冻结；每个 clone 打开时 Database
        # 仍会恢复生产 WAL/foreign_keys/busy_timeout PRAGMA。
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.close()
    sidecars = sqlite_sidecars(path)
    if sidecars:
        raise AssertionError(f"current-schema 模板冻结后含 sidecar: {sidecars}")
    return path


def clone_current_schema_database(template: Path, target: Path) -> Database:
    """复制关闭模板并打开独立连接，不重放迁移链。"""
    sidecars = sqlite_sidecars(template)
    if sidecars:
        raise AssertionError(f"current-schema 模板含未落盘 sidecar: {sidecars}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, target)
    if sqlite_sidecars(target):
        raise AssertionError("current-schema 副本打开前不应存在 sidecar")
    return Database(target)
