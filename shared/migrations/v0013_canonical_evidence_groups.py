"""把 canonical evidence 升级为不可拆分的来源组。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import FunctionType
from typing import Callable

from . import (
    v0001_legacy_baseline,
    v0005_canonical_evidence,
    v0006_concept_definition_history,
    v0012_concept_projector_version,
)

VERSION = 13
NAME = "canonical-evidence-source-groups"

OLD_CANONICAL_SCHEMA_SQL = v0005_canonical_evidence.CANONICAL_EVIDENCE_SCHEMA_SQL.split(
    "\n\nALTER TABLE study_suggestion_evidence", 1,
)[0]

CANONICAL_EVIDENCE_GROUP_SCHEMA_SQL = """
CREATE TABLE canonical_evidence (
    evidence_id TEXT PRIMARY KEY
        CHECK(length(evidence_id) = 67 AND substr(evidence_id, 1, 3) = 'ce_'),
    schema_version INTEGER NOT NULL DEFAULT 2
        CHECK(typeof(schema_version) = 'integer' AND schema_version = 2),
    job_id TEXT NOT NULL CHECK(length(trim(job_id)) > 0),
    note_type TEXT NOT NULL CHECK(length(trim(note_type)) > 0),
    chunk_id TEXT NOT NULL CHECK(length(trim(chunk_id)) > 0),
    section TEXT NOT NULL DEFAULT '',
    note_path TEXT NOT NULL CHECK(length(trim(note_path)) > 0),
    note_sha256 TEXT NOT NULL CHECK(length(note_sha256) = 64),
    provenance_path TEXT NOT NULL CHECK(length(trim(provenance_path)) > 0),
    provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256) = 64),
    chunk_body_sha256 TEXT NOT NULL CHECK(length(chunk_body_sha256) = 64),
    chunk_char_start INTEGER NOT NULL
        CHECK(typeof(chunk_char_start) = 'integer' AND chunk_char_start >= 0),
    chunk_char_end INTEGER NOT NULL
        CHECK(typeof(chunk_char_end) = 'integer' AND chunk_char_end > chunk_char_start),
    evidence_fingerprint TEXT NOT NULL CHECK(length(evidence_fingerprint) = 64),
    source_group_fingerprint TEXT NOT NULL CHECK(length(source_group_fingerprint) = 64),
    status TEXT NOT NULL DEFAULT 'valid'
        CHECK(status IN ('valid','stale','missing')),
    invalid_reason TEXT,
    validated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, note_type, evidence_fingerprint),
    UNIQUE(evidence_id, job_id),
    CHECK(
        (status='valid' AND invalid_reason IS NULL)
        OR (status IN ('stale','missing') AND invalid_reason IS NOT NULL
            AND length(trim(invalid_reason)) > 0)
    )
);
CREATE INDEX idx_canonical_evidence_job_note
    ON canonical_evidence(job_id, note_type, chunk_id);
CREATE INDEX idx_canonical_evidence_status
    ON canonical_evidence(status, job_id, note_type);
CREATE INDEX idx_canonical_evidence_source_group
    ON canonical_evidence(source_group_fingerprint, status);
CREATE TABLE canonical_evidence_sources (
    evidence_id TEXT NOT NULL REFERENCES canonical_evidence(evidence_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    ordinal INTEGER NOT NULL
        CHECK(typeof(ordinal)='integer' AND ordinal >= 0),
    source_ref TEXT NOT NULL CHECK(length(trim(source_ref)) > 0),
    source_segment_id TEXT NOT NULL CHECK(length(trim(source_segment_id)) > 0),
    source_path TEXT NOT NULL CHECK(length(trim(source_path)) > 0),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    source_revision TEXT,
    locator_kind TEXT NOT NULL CHECK(locator_kind IN ('media','pdf','text','image')),
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint) = 64),
    PRIMARY KEY(evidence_id, ordinal),
    UNIQUE(evidence_id, source_segment_id)
);
CREATE INDEX idx_canonical_evidence_sources_segment
    ON canonical_evidence_sources(source_segment_id, evidence_id);
CREATE INDEX idx_canonical_evidence_sources_fingerprint
    ON canonical_evidence_sources(source_fingerprint, evidence_id);
""".strip()

CURRENT_SCHEMA_SQL = v0012_concept_projector_version.CURRENT_SCHEMA_SQL.replace(
    OLD_CANONICAL_SCHEMA_SQL,
    CANONICAL_EVIDENCE_GROUP_SCHEMA_SQL,
    1,
)
if CURRENT_SCHEMA_SQL == v0012_concept_projector_version.CURRENT_SCHEMA_SQL:
    raise RuntimeError("canonical evidence schema replacement did not apply")


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    """冷迁移清空可重建投影，避免把单来源身份伪装成来源组。"""
    connection.execute(
        "UPDATE study_suggestion_evidence "
        "SET canonical_evidence_id=NULL, status='unavailable', "
        "invalid_reason='canonical_evidence_schema_upgraded'"
    )
    for table in (
        "concept_occurrences",
        "concept_occurrence_projection",
        "concept_occurrence_replay_state",
        "note_chunks_fts5",
        "note_chunks",
        "notes_fts5",
    ):
        connection.execute(f"DELETE FROM {table}")
    connection.execute("DROP TABLE canonical_evidence")
    v0001_legacy_baseline._execute_sql_script(
        connection, CANONICAL_EVIDENCE_GROUP_SCHEMA_SQL,
    )
    connection.execute(
        "CREATE UNIQUE INDEX idx_canonical_evidence_id_job_identity "
        "ON canonical_evidence(evidence_id, job_id)"
    )


class _CurrentSemanticReplay:
    """重放历史数据不变量，但由 v13 接管已替换的 canonical 表。"""

    def __getattr__(self, name: str) -> object:
        return getattr(v0006_concept_definition_history, name)

    def _replay_frozen_validator(
        self,
        connection: sqlite3.Connection,
        validator: Callable[[sqlite3.Connection], None],
    ) -> None:
        if validator.__module__.endswith("v0005_canonical_evidence"):
            return
        validator_globals = dict(validator.__globals__)
        validator_globals["v0001_legacy_baseline"] = (
            v0006_concept_definition_history._SemanticReplayBaseline()
        )
        validator_globals["v0006_concept_definition_history"] = self
        if validator.__module__.endswith("v0006_concept_definition_history"):
            validator_globals["_replay_frozen_validator"] = (
                self._replay_frozen_validator
            )
        replay = FunctionType(
            validator.__code__,
            validator_globals,
            validator.__name__,
            validator.__defaults__,
            validator.__closure__,
        )
        replay(connection)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    _CurrentSemanticReplay()._replay_frozen_validator(
        connection, v0012_concept_projector_version.validate,
    )
    rows = connection.execute(
        "SELECT * FROM canonical_evidence ORDER BY evidence_id"
    ).fetchall()
    for row in rows:
        evidence_id = str(row["evidence_id"])
        if not all(_is_sha256(row[field]) for field in (
            "note_sha256", "provenance_sha256", "chunk_body_sha256",
            "evidence_fingerprint", "source_group_fingerprint",
        )):
            raise sqlite3.DatabaseError(
                f"canonical evidence sha256 非法: {evidence_id}"
            )
        members = connection.execute(
            "SELECT * FROM canonical_evidence_sources WHERE evidence_id=? "
            "ORDER BY ordinal",
            (evidence_id,),
        ).fetchall()
        if not members or [int(item["ordinal"]) for item in members] != list(
            range(len(members))
        ):
            raise sqlite3.DatabaseError(
                f"canonical evidence source group 非连续: {evidence_id}"
            )
        fingerprints: list[str] = []
        for member in members:
            if not all(_is_sha256(member[field]) for field in (
                "source_sha256", "source_fingerprint",
            )):
                raise sqlite3.DatabaseError(
                    f"canonical evidence source sha256 非法: {evidence_id}"
                )
            try:
                locator = json.loads(str(member["locator_json"]))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise sqlite3.DatabaseError(
                    f"canonical evidence locator 非法: {evidence_id}"
                ) from exc
            if (
                not isinstance(locator, dict)
                or locator.get("kind") != member["locator_kind"]
                or json.dumps(locator, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) != member["locator_json"]
            ):
                raise sqlite3.DatabaseError(
                    f"canonical evidence locator 不匹配: {evidence_id}"
                )
            fingerprints.append(str(member["source_fingerprint"]))
        source_group_fingerprint = hashlib.sha256(json.dumps(
            fingerprints, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if source_group_fingerprint != row["source_group_fingerprint"]:
            raise sqlite3.DatabaseError(
                f"canonical evidence source group 指纹不匹配: {evidence_id}"
            )
        expected_id = "ce_" + hashlib.sha256(json.dumps(
            {
                "schema_version": int(row["schema_version"]),
                "job_id": str(row["job_id"]),
                "note_type": str(row["note_type"]),
                "chunk_id": str(row["chunk_id"]),
                "source_group_fingerprint": source_group_fingerprint,
                "evidence_fingerprint": str(row["evidence_fingerprint"]),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if evidence_id != expected_id:
            raise sqlite3.DatabaseError(
                f"canonical evidence id 不可复算: {evidence_id}"
            )

    dangling = connection.execute(
        """SELECT e.evidence_id FROM study_suggestion_evidence e
           LEFT JOIN canonical_evidence c ON c.evidence_id=e.canonical_evidence_id
           WHERE e.canonical_evidence_id IS NOT NULL
             AND (c.evidence_id IS NULL OR c.job_id != e.job_id
                  OR c.chunk_id != e.chunk_id)
           LIMIT 1"""
    ).fetchone()
    if dangling is not None:
        raise sqlite3.DatabaseError(
            f"study suggestion canonical evidence 指针非法: {dangling[0]}"
        )
