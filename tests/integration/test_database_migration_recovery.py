"""验证服务启动经生产迁移入口遇到失败与非法历史时的边界。"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from shared.db import Database, SCHEMA_VERSION
from shared.migrations import (
    Migration,
    MigrationHistoryError,
    UnsupportedSchemaVersionError,
    load_manifest,
    migration_steps,
    run_migrations,
)
from shared.models import Job


pytestmark = pytest.mark.integration

_NEXT_VERSION = SCHEMA_VERSION + 1
_FOLLOWING_VERSION = SCHEMA_VERSION + 2
_NEXT_PAYLOAD = f"integration-synthetic-v{_NEXT_VERSION}"
_FOLLOWING_PAYLOAD = f"integration-synthetic-v{_FOLLOWING_VERSION}"


def _job(job_id: str, title: str) -> Job:
    return Job(
        id=job_id,
        content_type="document",
        pipeline="document",
        document_kind="article",
        title=title,
        lineage_key=job_id,
    )


def _apply_next(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE integration_future_next(value TEXT NOT NULL)")
    connection.execute("INSERT INTO integration_future_next VALUES ('next')")


def _apply_following(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE integration_future_following(value TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO integration_future_following VALUES ('following')")


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(str(value) for value in row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    )


def _assert_synthetic_schema(
    connection: sqlite3.Connection,
    expected: tuple[tuple[str, ...], ...],
    expected_values: dict[str, str],
) -> None:
    actual = _schema_objects(connection)
    if actual != expected:
        raise sqlite3.DatabaseError("合成迁移未保留完整当前 schema")
    for table, value in expected_values.items():
        row = connection.execute(f"SELECT value FROM {table}").fetchone()
        if row is None or tuple(row) != (value,):
            raise sqlite3.DatabaseError(f"合成迁移表 {table} 数据不完整")


def _synthetic_migrations(
    connection: sqlite3.Connection,
) -> tuple[Migration, ...]:
    current = migration_steps()
    current_validator = current[-1].validate
    if current_validator is None:
        raise RuntimeError("当前迁移缺少完整 schema validator")
    current_validator(connection)
    baseline = _schema_objects(connection)
    next_schema = tuple(
        sorted(
            (
                *baseline,
                (
                    "table",
                    "integration_future_next",
                    "integration_future_next",
                    "CREATE TABLE integration_future_next(value TEXT NOT NULL)",
                ),
            ),
            key=lambda row: (row[0], row[1]),
        )
    )
    following_schema = tuple(
        sorted(
            (
                *next_schema,
                (
                    "table",
                    "integration_future_following",
                    "integration_future_following",
                    "CREATE TABLE integration_future_following(value TEXT NOT NULL)",
                ),
            ),
            key=lambda row: (row[0], row[1]),
        )
    )

    def validate_next(candidate: sqlite3.Connection) -> None:
        _assert_synthetic_schema(
            candidate,
            next_schema,
            {"integration_future_next": "next"},
        )

    def validate_following(candidate: sqlite3.Connection) -> None:
        _assert_synthetic_schema(
            candidate,
            following_schema,
            {
                "integration_future_next": "next",
                "integration_future_following": "following",
            },
        )

    return (
        *current,
        Migration(
            _NEXT_VERSION,
            "integration-next",
            _NEXT_PAYLOAD,
            _apply_next,
            validate_next,
        ),
        Migration(
            _FOLLOWING_VERSION,
            "integration-following",
            _FOLLOWING_PAYLOAD,
            _apply_following,
            validate_following,
        ),
    )


def _run_failing_pending_chain(db_path: str, manifest_path: str, results) -> None:
    database: Database | None = None
    try:
        database = Database(db_path)

        def fail_after_following(
            version: int,
            _connection: sqlite3.Connection,
        ) -> None:
            if version == _FOLLOWING_VERSION:
                raise RuntimeError("集成测试后段故障")

        run_migrations(
            database._conn,
            _synthetic_migrations(database._conn),
            manifest_path=manifest_path,
            fault_injector=fail_after_following,
        )
        results.put(("unexpected-success",))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        if database is not None:
            database.close()


def _extended_manifest(path: Path) -> Path:
    manifest = load_manifest()
    manifest["current_version"] = _FOLLOWING_VERSION
    manifest["migrations"].extend(
        [
            {
                "version": _NEXT_VERSION,
                "name": "integration-next",
                "checksum": hashlib.sha256(_NEXT_PAYLOAD.encode()).hexdigest(),
            },
            {
                "version": _FOLLOWING_VERSION,
                "name": "integration-following",
                "checksum": hashlib.sha256(_FOLLOWING_PAYLOAD.encode()).hexdigest(),
            },
        ]
    )
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path


def test_failed_two_step_chain_rolls_back_and_parent_reopens_current(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pending-chain.db"
    with Database(db_path) as database:
        database.init_schema()
        database.create_job(_job("jobs_before_chain", "当前版本业务数据"))

    manifest_path = _extended_manifest(tmp_path / "manifest-future.json")
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_run_failing_pending_chain,
        args=(str(db_path), str(manifest_path), results),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("故障迁移子进程超时")
    assert process.exitcode == 0
    outcome = results.get(timeout=2)
    assert outcome[0:2] == ("error", "MigrationExecutionError")
    assert f"回滚到 v{SCHEMA_VERSION}" in outcome[2]
    assert "集成测试后段故障" in outcome[2]

    with Database(db_path) as reopened:
        reopened.init_schema()
        assert reopened.schema_version() == SCHEMA_VERSION
        assert reopened.get_job("jobs_before_chain").title == "当前版本业务数据"
        assert reopened._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name IN "
            "('integration_future_next', 'integration_future_following')"
        ).fetchall() == []
        assert [
            row[0]
            for row in reopened._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == list(range(1, SCHEMA_VERSION + 1))


def test_future_main_database_fails_closed_without_byte_changes(tmp_path: Path) -> None:
    db_path = tmp_path / "future-main.db"
    with Database(db_path) as database:
        database.init_schema()
        database.create_job(_job("jobs_future", "未来版本前数据"))

    connection = sqlite3.connect(db_path)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    before_main = db_path.read_bytes()
    sidecars = {
        suffix: db_path.with_name(db_path.name + suffix).read_bytes()
        for suffix in ("-journal", "-wal", "-shm")
        if db_path.with_name(db_path.name + suffix).exists()
    }

    with pytest.raises(UnsupportedSchemaVersionError, match="高于当前程序上限"):
        Database(db_path)

    assert db_path.read_bytes() == before_main
    assert {
        suffix: db_path.with_name(db_path.name + suffix).read_bytes()
        for suffix in ("-journal", "-wal", "-shm")
        if db_path.with_name(db_path.name + suffix).exists()
    } == sidecars


def test_current_schema_with_malformed_ledger_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "malformed-ledger.db"
    with Database(db_path) as database:
        database.init_schema()
        database.create_job(_job("jobs_ledger", "ledger 前数据"))
        database._conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            ("0" * 64, SCHEMA_VERSION),
        )
        database._conn.commit()

    reopened = Database(db_path)
    try:
        with pytest.raises(MigrationHistoryError, match="不一致"):
            reopened.init_schema()
        assert reopened.schema_version() == SCHEMA_VERSION
        assert reopened.get_job("jobs_ledger").title == "ledger 前数据"
        assert reopened._conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (SCHEMA_VERSION,),
        ).fetchone()[0] == "0" * 64
    finally:
        reopened.close()
