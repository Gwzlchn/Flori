"""为 occurrence 投影与重放状态持久化 projector 版本。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import (
    v0001_legacy_baseline,
    v0006_concept_definition_history,
    v0011_qoder_credit_accounting,
)

VERSION = 12
NAME = "concept-projector-version"

_PROJECTOR_VERSION_COLUMN = (
    "projector_version INTEGER NOT NULL DEFAULT 1 "
    "CHECK(typeof(projector_version)='integer' AND projector_version >= 1)"
)

LEGACY_WRITER_TRIGGERS_SQL = """
CREATE TRIGGER concept_projection_legacy_writer_version
AFTER UPDATE OF source_digest, projection_digest, reconciled_at
ON concept_occurrence_projection
WHEN NEW.projector_version = OLD.projector_version
 AND NEW.projector_version > 1
 AND (
     NEW.source_digest IS NOT OLD.source_digest
     OR NEW.projection_digest IS NOT OLD.projection_digest
     OR NEW.reconciled_at IS NOT OLD.reconciled_at
 )
BEGIN
    UPDATE concept_occurrence_projection
    SET projector_version=1
    WHERE job_id=NEW.job_id;
END;

CREATE TRIGGER concept_replay_state_legacy_writer_version
AFTER UPDATE OF state, reason, source_digest, attempt_count,
                last_attempt_at, next_retry_at, updated_at
ON concept_occurrence_replay_state
WHEN NEW.projector_version = OLD.projector_version
 AND NEW.projector_version > 1
 AND (
     NEW.state IS NOT OLD.state
     OR NEW.reason IS NOT OLD.reason
     OR NEW.source_digest IS NOT OLD.source_digest
     OR NEW.attempt_count IS NOT OLD.attempt_count
     OR NEW.last_attempt_at IS NOT OLD.last_attempt_at
     OR NEW.next_retry_at IS NOT OLD.next_retry_at
     OR NEW.updated_at IS NOT OLD.updated_at
 )
BEGIN
    UPDATE concept_occurrence_replay_state
    SET projector_version=1
    WHERE job_id=NEW.job_id;
END;
""".strip()

CURRENT_SCHEMA_SQL = v0011_qoder_credit_accounting.CURRENT_SCHEMA_SQL.replace(
    """    reconciled_at TEXT NOT NULL
);""",
    """    reconciled_at TEXT NOT NULL,
    projector_version INTEGER NOT NULL DEFAULT 1 CHECK(
        typeof(projector_version)='integer' AND projector_version >= 1
    )
);""",
    1,
).replace(
    """    updated_at TEXT NOT NULL,
    CHECK(
        (
            state='verified_empty'""",
    """    updated_at TEXT NOT NULL,
    projector_version INTEGER NOT NULL DEFAULT 1 CHECK(
        typeof(projector_version)='integer' AND projector_version >= 1
    ),
    CHECK(
        (
            state='verified_empty'""",
    1,
)
CURRENT_SCHEMA_SQL += "\n\n" + LEGACY_WRITER_TRIGGERS_SQL


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE concept_occurrence_projection ADD COLUMN "
        + _PROJECTOR_VERSION_COLUMN
    )
    connection.execute(
        "ALTER TABLE concept_occurrence_replay_state ADD COLUMN "
        + _PROJECTOR_VERSION_COLUMN
    )
    v0001_legacy_baseline._execute_sql_script(
        connection, LEGACY_WRITER_TRIGGERS_SQL,
    )


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    v0006_concept_definition_history._replay_frozen_validator(
        connection, v0011_qoder_credit_accounting.validate,
    )
    for table in (
        "concept_occurrence_projection",
        "concept_occurrence_replay_state",
    ):
        invalid = connection.execute(
            f"""SELECT job_id FROM {table}
                WHERE typeof(projector_version)<>'integer'
                   OR projector_version < 1
                LIMIT 1"""
        ).fetchone()
        if invalid is not None:
            raise sqlite3.DatabaseError(
                f"{table} has invalid projector version: {invalid[0]}"
            )
