"""为多模态证据定位增加可重验的 canonical evidence 快照。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import v0001_legacy_baseline, v0004_study_suggestions


VERSION = 5
NAME = "canonical-evidence-locators"


CANONICAL_EVIDENCE_SCHEMA_SQL = """
CREATE TABLE canonical_evidence (
    evidence_id TEXT PRIMARY KEY
        CHECK(length(evidence_id) = 67 AND substr(evidence_id, 1, 3) = 'ce_'),
    schema_version INTEGER NOT NULL DEFAULT 1
        CHECK(typeof(schema_version) = 'integer' AND schema_version = 1),
    job_id TEXT NOT NULL CHECK(length(trim(job_id)) > 0),
    note_type TEXT NOT NULL CHECK(length(trim(note_type)) > 0),
    chunk_id TEXT NOT NULL CHECK(length(trim(chunk_id)) > 0),
    section TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL CHECK(length(trim(source_ref)) > 0),
    source_segment_id TEXT NOT NULL CHECK(length(trim(source_segment_id)) > 0),
    source_path TEXT NOT NULL CHECK(length(trim(source_path)) > 0),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    source_revision TEXT,
    note_path TEXT NOT NULL CHECK(length(trim(note_path)) > 0),
    note_sha256 TEXT NOT NULL CHECK(length(note_sha256) = 64),
    provenance_path TEXT NOT NULL CHECK(length(trim(provenance_path)) > 0),
    provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256) = 64),
    chunk_body_sha256 TEXT NOT NULL CHECK(length(chunk_body_sha256) = 64),
    chunk_char_start INTEGER NOT NULL
        CHECK(typeof(chunk_char_start) = 'integer' AND chunk_char_start >= 0),
    chunk_char_end INTEGER NOT NULL
        CHECK(typeof(chunk_char_end) = 'integer' AND chunk_char_end > chunk_char_start),
    locator_kind TEXT NOT NULL CHECK(locator_kind IN ('media','pdf','text','image')),
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    evidence_fingerprint TEXT NOT NULL CHECK(length(evidence_fingerprint) = 64),
    source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint) = 64),
    status TEXT NOT NULL DEFAULT 'valid'
        CHECK(status IN ('valid','stale','missing')),
    invalid_reason TEXT,
    validated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, note_type, evidence_fingerprint),
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
CREATE INDEX idx_canonical_evidence_source
    ON canonical_evidence(source_fingerprint, status);

ALTER TABLE study_suggestion_evidence
    ADD COLUMN canonical_evidence_id TEXT
        REFERENCES canonical_evidence(evidence_id) ON DELETE SET NULL;
CREATE INDEX idx_study_suggestion_evidence_canonical
    ON study_suggestion_evidence(canonical_evidence_id);
""".strip()


CURRENT_SCHEMA_SQL = (
    v0004_study_suggestions.CURRENT_SCHEMA_SQL
    + "\n\n"
    + CANONICAL_EVIDENCE_SCHEMA_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(
        connection, CANONICAL_EVIDENCE_SCHEMA_SQL
    )


def validate(connection: sqlite3.Connection) -> None:
    """校验完整 schema 与 canonical evidence 快照的不变量。"""
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)

    def fail(message: str) -> None:
        raise sqlite3.DatabaseError(message)

    def is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    rows = connection.execute(
        "SELECT * FROM canonical_evidence ORDER BY evidence_id"
    ).fetchall()
    for row in rows:
        evidence_id = str(row["evidence_id"])
        if not all(
            is_sha256(row[field])
            for field in (
                "source_sha256", "note_sha256", "provenance_sha256",
                "chunk_body_sha256", "evidence_fingerprint", "source_fingerprint",
            )
        ):
            fail(f"canonical evidence sha256 非法: {evidence_id}")
        try:
            locator = json.loads(str(row["locator_json"]))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError(
                f"canonical evidence locator 非法: {evidence_id}"
            ) from exc
        canonical = json.dumps(
            locator, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if str(row["locator_json"]) != canonical:
            fail(f"canonical evidence locator 不是 canonical JSON: {evidence_id}")
        if not isinstance(locator, dict) or locator.get("kind") != row["locator_kind"]:
            fail(f"canonical evidence locator kind 不匹配: {evidence_id}")
        expected_id = "ce_" + hashlib.sha256(
            json.dumps(
                {
                    "schema_version": int(row["schema_version"]),
                    "job_id": str(row["job_id"]),
                    "note_type": str(row["note_type"]),
                    "chunk_id": str(row["chunk_id"]),
                    "source_ref": str(row["source_ref"]),
                    "source_segment_id": str(row["source_segment_id"]),
                    "evidence_fingerprint": str(row["evidence_fingerprint"]),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if evidence_id != expected_id:
            fail(f"canonical evidence id 不可复算: {evidence_id}")

    dangling = connection.execute(
        """SELECT e.evidence_id
           FROM study_suggestion_evidence e
           LEFT JOIN canonical_evidence c
             ON c.evidence_id=e.canonical_evidence_id
           WHERE e.canonical_evidence_id IS NOT NULL
             AND (c.evidence_id IS NULL OR c.job_id != e.job_id OR c.chunk_id != e.chunk_id)
           LIMIT 1"""
    ).fetchone()
    if dangling is not None:
        fail(f"study suggestion canonical evidence 指针非法: {dangling[0]}")
