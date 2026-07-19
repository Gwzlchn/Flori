"""为概念 occurrence 的可重建投影增加持久重试账本。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import (
    v0001_legacy_baseline,
    v0006_concept_definition_history,
    v0008_multipart_jobs,
)

VERSION = 9
NAME = "recovery-projection-ledgers"

PROJECTION_SCHEMA_SQL = """
CREATE TABLE concept_occurrence_projection (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    source_digest TEXT NOT NULL CHECK(
        substr(source_digest, 1, 7)='sha256:'
        AND length(source_digest)=71
        AND substr(source_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    projection_digest TEXT NOT NULL CHECK(
        substr(projection_digest, 1, 7)='sha256:'
        AND length(projection_digest)=71
        AND substr(projection_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    reconciled_at TEXT NOT NULL
);

CREATE TABLE restored_job_activations (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    activated_at TEXT NOT NULL
);
""".strip()

CURRENT_SCHEMA_SQL = (
    v0008_multipart_jobs.CURRENT_SCHEMA_SQL
    + "\n\n"
    + PROJECTION_SCHEMA_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(connection, PROJECTION_SCHEMA_SQL)


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    v0006_concept_definition_history._replay_frozen_validator(
        connection, v0008_multipart_jobs.validate,
    )
    orphan = connection.execute(
        """SELECT job_id FROM concept_occurrence_projection
           WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_id)
           LIMIT 1"""
    ).fetchone()
    if orphan is not None:
        raise sqlite3.DatabaseError(
            f"concept occurrence projection has orphan job: {orphan[0]}"
        )
    activation_orphan = connection.execute(
        """SELECT job_id FROM restored_job_activations
           WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_id)
           LIMIT 1"""
    ).fetchone()
    if activation_orphan is not None:
        raise sqlite3.DatabaseError(
            f"restored activation has orphan job: {activation_orphan[0]}"
        )
