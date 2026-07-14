"""冻结迁移 ledger 引入步骤。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import v0001_legacy_baseline


VERSION = 2
NAME = "immutable-migration-ledger"


LEDGER_SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK(length(checksum) = 64),
    applied_at TEXT NOT NULL
)
""".strip()

CURRENT_SCHEMA_SQL = v0001_legacy_baseline.SCHEMA_SQL + "\n" + LEDGER_SQL + ";\n"


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    # 老 v1 库可能来自隐式补列路径，建 ledger 前重申 v1 完整不变量。
    v0001_legacy_baseline.apply(connection)
    connection.execute(LEDGER_SQL)


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(
        connection, CURRENT_SCHEMA_SQL
    )
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute(LEDGER_SQL)
        v0001_legacy_baseline._assert_table_semantics(
            connection,
            expected,
            "schema_migrations",
            exact_sql=True,
        )
        v0001_legacy_baseline._assert_index_semantics(
            connection, expected, "schema_migrations"
        )
    finally:
        expected.close()
    invalid = connection.execute(
        "SELECT version FROM schema_migrations "
        "WHERE applied_at IS NULL OR trim(applied_at)='' LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise sqlite3.DatabaseError(
            f"schema_migrations v{int(invalid[0])} applied_at 为空"
        )
