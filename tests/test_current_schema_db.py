"""current-schema 测试模板的完整性与隔离不变量。"""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from shared.db import SCHEMA_VERSION
from shared.migrations import migration_steps
from tests.current_schema_db import (
    clone_current_schema_database,
    sqlite_sidecars,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_template_is_closed_current_schema_with_complete_ledger(
    current_schema_db_template: Path,
) -> None:
    assert sqlite_sidecars(current_schema_db_template) == ()
    connection = sqlite3.connect(
        f"file:{current_schema_db_template}?mode=ro",
        uri=True,
    )
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA user_version").fetchone() == (
            SCHEMA_VERSION,
        )
        rows = connection.execute(
            "SELECT version, name, checksum, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()
    expected = migration_steps()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (item.version, item.name, item.checksum) for item in expected
    ]
    assert all(row[3] for row in rows)


def test_clones_keep_runtime_pragmas_and_do_not_mutate_template(
    current_schema_db_template: Path,
    tmp_path: Path,
) -> None:
    original_hash = _sha256(current_schema_db_template)
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    first = clone_current_schema_database(current_schema_db_template, first_path)
    second = clone_current_schema_database(current_schema_db_template, second_path)
    try:
        assert first._path == first_path
        assert first_path.stat().st_ino != current_schema_db_template.stat().st_ino
        assert second_path.stat().st_ino != current_schema_db_template.stat().st_ino
        assert first._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert first._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert first._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        first._conn.execute("CREATE TABLE clone_only(value TEXT NOT NULL)")
        first._conn.execute("INSERT INTO clone_only VALUES ('first')")
        first._conn.commit()
        assert second._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='clone_only'"
        ).fetchone() is None
    finally:
        first.close()
        second.close()

    reopened = sqlite3.connect(first_path)
    try:
        assert reopened.execute("SELECT value FROM clone_only").fetchone() == (
            "first",
        )
    finally:
        reopened.close()
    assert _sha256(current_schema_db_template) == original_hash
    assert sqlite_sidecars(current_schema_db_template) == ()


def test_concurrent_clones_write_to_independent_files(
    current_schema_db_template: Path,
    tmp_path: Path,
) -> None:
    original_hash = _sha256(current_schema_db_template)

    def write_clone(index: int) -> tuple[int, str]:
        target = tmp_path / f"clone-{index}.db"
        database = clone_current_schema_database(current_schema_db_template, target)
        try:
            database._conn.execute("CREATE TABLE clone_marker(value INTEGER NOT NULL)")
            database._conn.execute("INSERT INTO clone_marker VALUES (?)", (index,))
            database._conn.commit()
            value = database._conn.execute(
                "SELECT value FROM clone_marker"
            ).fetchone()[0]
        finally:
            database.close()
        return int(value), _sha256(target)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(write_clone, range(8)))

    assert [value for value, _digest in results] == list(range(8))
    assert len({digest for _value, digest in results}) == 8
    assert _sha256(current_schema_db_template) == original_hash
    assert sqlite_sidecars(current_schema_db_template) == ()
