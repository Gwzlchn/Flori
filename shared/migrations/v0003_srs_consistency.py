"""把 SRS 评分升级为幂等,CAS 且时区安全的事务模型."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import v0001_legacy_baseline, v0002_immutable_ledger


VERSION = 3
NAME = "srs-transaction-consistency"
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


STUDY_SCHEMA_SQL = """
CREATE TABLE study_cards (
    card_id TEXT PRIMARY KEY CHECK(length(trim(card_id)) > 0),
    domain TEXT NOT NULL DEFAULT 'general' CHECK(length(trim(domain)) > 0),
    job_id TEXT,
    concept_term TEXT,
    card_type TEXT NOT NULL DEFAULT 'basic'
        CHECK(card_type IN ('basic','cloze','qa','quiz_single','quiz_multi')),
    front TEXT NOT NULL CHECK(length(trim(front)) > 0),
    back TEXT NOT NULL CHECK(length(trim(back)) > 0),
    explanation TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('suggested','active','suspended','rejected')),
    source TEXT NOT NULL DEFAULT 'manual' CHECK(length(trim(source)) > 0),
    revision INTEGER NOT NULL DEFAULT 1
        CHECK(typeof(revision) = 'integer' AND revision BETWEEN 1 AND 9223372036854775807),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_study_cards_domain ON study_cards(domain);
CREATE INDEX idx_study_cards_status ON study_cards(status);
CREATE INDEX idx_study_cards_job ON study_cards(job_id);

CREATE TABLE study_reviews (
    card_id TEXT PRIMARY KEY REFERENCES study_cards(card_id) ON DELETE CASCADE,
    due_at TEXT NOT NULL,
    due_at_epoch_us INTEGER NOT NULL
        CHECK(typeof(due_at_epoch_us) = 'integer'),
    interval_days REAL NOT NULL DEFAULT 0
        CHECK(interval_days >= 0 AND interval_days <= 36500),
    ease REAL NOT NULL DEFAULT 2.5 CHECK(ease >= 1.3 AND ease <= 3.0),
    repetitions INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(repetitions) = 'integer' AND repetitions >= 0),
    lapses INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(lapses) = 'integer' AND lapses >= 0),
    last_grade TEXT CHECK(last_grade IS NULL OR last_grade IN ('again','hard','good','easy')),
    last_reviewed_at TEXT,
    last_reviewed_at_epoch_us INTEGER
        CHECK(last_reviewed_at_epoch_us IS NULL OR typeof(last_reviewed_at_epoch_us) = 'integer'),
    updated_at TEXT NOT NULL,
    CHECK((last_reviewed_at IS NULL) = (last_reviewed_at_epoch_us IS NULL))
);
CREATE INDEX idx_study_reviews_due ON study_reviews(due_at_epoch_us);

CREATE TABLE study_review_logs (
    id TEXT PRIMARY KEY CHECK(length(trim(id)) > 0),
    card_id TEXT NOT NULL REFERENCES study_cards(card_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL CHECK(length(trim(request_id)) > 0),
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    grade TEXT NOT NULL CHECK(grade IN ('again','hard','good','easy')),
    reviewed_at TEXT NOT NULL,
    reviewed_at_epoch_us INTEGER NOT NULL
        CHECK(typeof(reviewed_at_epoch_us) = 'integer'),
    response_ms INTEGER
        CHECK(response_ms IS NULL OR (typeof(response_ms) = 'integer' AND response_ms >= 0)),
    scheduled_due_at TEXT,
    scheduled_due_at_epoch_us INTEGER
        CHECK(scheduled_due_at_epoch_us IS NULL OR typeof(scheduled_due_at_epoch_us) = 'integer'),
    next_due_at TEXT NOT NULL,
    next_due_at_epoch_us INTEGER NOT NULL
        CHECK(typeof(next_due_at_epoch_us) = 'integer'),
    interval_days REAL NOT NULL CHECK(interval_days >= 0 AND interval_days <= 36500),
    ease REAL NOT NULL CHECK(ease >= 1.3 AND ease <= 3.0),
    repetitions INTEGER NOT NULL
        CHECK(typeof(repetitions) = 'integer' AND repetitions >= 0),
    lapses INTEGER NOT NULL CHECK(typeof(lapses) = 'integer' AND lapses >= 0),
    revision_before INTEGER NOT NULL
        CHECK(typeof(revision_before) = 'integer' AND revision_before >= 1),
    revision_after INTEGER NOT NULL
        CHECK(typeof(revision_after) = 'integer' AND revision_after = revision_before + 1),
    outcome_json TEXT NOT NULL CHECK(length(trim(outcome_json)) > 0),
    CHECK((scheduled_due_at IS NULL) = (scheduled_due_at_epoch_us IS NULL))
);
CREATE INDEX idx_study_review_logs_card ON study_review_logs(card_id);
CREATE UNIQUE INDEX idx_study_review_logs_request_id ON study_review_logs(request_id);
""".strip()

_LEGACY_STUDY_SCHEMA_SQL = v0001_legacy_baseline.SCHEMA_SQL[
    v0001_legacy_baseline.SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS study_cards") :
].strip()
CURRENT_SCHEMA_SQL = v0002_immutable_ledger.CURRENT_SCHEMA_SQL.replace(
    _LEGACY_STUDY_SCHEMA_SQL, STUDY_SCHEMA_SQL
)
if CURRENT_SCHEMA_SQL == v0002_immutable_ledger.CURRENT_SCHEMA_SQL:
    raise RuntimeError("v3 无法定位冻结的 v2 study schema")


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    cursor = connection.execute(sql)
    names = [str(column[0]) for column in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _legacy_time(value: object, field: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise sqlite3.DatabaseError(f"{field} 旧值为空")
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise sqlite3.DatabaseError(f"{field} 旧值不是 ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    delta = parsed - _EPOCH
    epoch_us = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if not -_MAX_SQLITE_INTEGER - 1 <= epoch_us <= _MAX_SQLITE_INTEGER:
        raise sqlite3.DatabaseError(f"{field} 超出 SQLite INTEGER 范围")
    return parsed.isoformat(), epoch_us


def _optional_legacy_time(value: object, field: str) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    iso, epoch_us = _legacy_time(value, field)
    return iso, epoch_us


def _legacy_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply(connection: sqlite3.Connection) -> None:
    cards = _rows(connection, "SELECT * FROM study_cards ORDER BY card_id")
    reviews = _rows(connection, "SELECT * FROM study_reviews ORDER BY card_id")
    logs = _rows(connection, "SELECT * FROM study_review_logs")
    logs.sort(
        key=lambda row: (
            str(row["card_id"]),
            _legacy_time(row["reviewed_at"], "study_review_logs.reviewed_at")[1],
            str(row["id"]),
        )
    )
    log_counts: dict[str, int] = {}
    for row in logs:
        card_id = str(row["card_id"])
        log_counts[card_id] = log_counts.get(card_id, 0) + 1

    connection.execute("ALTER TABLE study_review_logs RENAME TO study_review_logs_v2")
    connection.execute("ALTER TABLE study_reviews RENAME TO study_reviews_v2")
    connection.execute("ALTER TABLE study_cards RENAME TO study_cards_v2")
    for index in (
        "idx_study_review_logs_card",
        "idx_study_reviews_due",
        "idx_study_cards_domain",
        "idx_study_cards_status",
        "idx_study_cards_job",
    ):
        connection.execute(f'DROP INDEX "{index}"')
    v0001_legacy_baseline._execute_sql_script(connection, STUDY_SCHEMA_SQL)

    card_facts: dict[str, dict[str, object]] = {}
    for row in cards:
        created_at, _ = _legacy_time(row["created_at"], "study_cards.created_at")
        updated_at, _ = _legacy_time(row["updated_at"], "study_cards.updated_at")
        card_id = str(row["card_id"])
        revision = 1 + log_counts.get(card_id, 0)
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except (json.JSONDecodeError, TypeError):
            evidence = []
        card_facts[card_id] = {
            "card_id": card_id,
            "domain": row["domain"],
            "job_id": row["job_id"],
            "concept_term": row["concept_term"],
            "card_type": row["card_type"],
            "front": row["front"],
            "back": row["back"],
            "explanation": row["explanation"],
            "evidence": evidence,
            "source": row["source"],
            "created_at": created_at,
        }
        connection.execute(
            """INSERT INTO study_cards
               (card_id, domain, job_id, concept_term, card_type, front, back,
                explanation, evidence_json, status, source, revision, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                card_id,
                row["domain"],
                row["job_id"],
                row["concept_term"],
                row["card_type"],
                row["front"],
                row["back"],
                row["explanation"],
                row["evidence_json"],
                row["status"],
                row["source"],
                revision,
                created_at,
                updated_at,
            ),
        )

    for row in reviews:
        due_at, due_epoch = _legacy_time(row["due_at"], "study_reviews.due_at")
        last_reviewed_at, last_reviewed_epoch = _optional_legacy_time(
            row["last_reviewed_at"], "study_reviews.last_reviewed_at"
        )
        updated_at, _ = _legacy_time(row["updated_at"], "study_reviews.updated_at")
        connection.execute(
            """INSERT INTO study_reviews
               (card_id, due_at, due_at_epoch_us, interval_days, ease, repetitions,
                lapses, last_grade, last_reviewed_at, last_reviewed_at_epoch_us, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["card_id"],
                due_at,
                due_epoch,
                row["interval_days"],
                row["ease"],
                row["repetitions"],
                row["lapses"],
                row["last_grade"],
                last_reviewed_at,
                last_reviewed_epoch,
                updated_at,
            ),
        )

    per_card_revision: dict[str, int] = {}
    for row in logs:
        card_id = str(row["card_id"])
        revision_before = per_card_revision.get(card_id, 1)
        revision_after = revision_before + 1
        per_card_revision[card_id] = revision_after
        reviewed_at, reviewed_epoch = _legacy_time(
            row["reviewed_at"], "study_review_logs.reviewed_at"
        )
        scheduled_due_at, scheduled_due_epoch = _optional_legacy_time(
            row["scheduled_due_at"], "study_review_logs.scheduled_due_at"
        )
        next_due_at, next_due_epoch = _legacy_time(
            row["next_due_at"], "study_review_logs.next_due_at"
        )
        request_id = f"legacy:{row['id']}"
        payload = {
            "card_id": card_id,
            "expected_revision": revision_before,
            "grade": row["grade"],
            "response_ms": row["response_ms"],
        }
        outcome = {
            **card_facts[card_id],
            "legacy_migrated": True,
            "status": "active",
            "revision": revision_after,
            "updated_at": reviewed_at,
            "review": {
                "due_at": next_due_at,
                "interval_days": row["interval_days"],
                "ease": row["ease"],
                "repetitions": row["repetitions"],
                "lapses": row["lapses"],
                "last_grade": row["grade"],
                "last_reviewed_at": reviewed_at,
                "updated_at": reviewed_at,
            },
        }
        connection.execute(
            """INSERT INTO study_review_logs
               (id, card_id, request_id, request_fingerprint, grade, reviewed_at,
                reviewed_at_epoch_us, response_ms, scheduled_due_at,
                scheduled_due_at_epoch_us, next_due_at, next_due_at_epoch_us,
                interval_days, ease, repetitions, lapses, revision_before,
                revision_after, outcome_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"],
                card_id,
                request_id,
                _legacy_fingerprint(payload),
                row["grade"],
                reviewed_at,
                reviewed_epoch,
                row["response_ms"],
                scheduled_due_at,
                scheduled_due_epoch,
                next_due_at,
                next_due_epoch,
                row["interval_days"],
                row["ease"],
                row["repetitions"],
                row["lapses"],
                revision_before,
                revision_after,
                json.dumps(outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    connection.execute("DROP TABLE study_review_logs_v2")
    connection.execute("DROP TABLE study_reviews_v2")
    connection.execute("DROP TABLE study_cards_v2")


def validate(connection: sqlite3.Connection) -> None:
    """校验 v3 的完整 current schema,不调用 v2 exact validator."""
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    invalid_outcome = connection.execute(
        "SELECT id FROM study_review_logs WHERE json_valid(outcome_json)=0 LIMIT 1"
    ).fetchone()
    if invalid_outcome is not None:
        raise sqlite3.DatabaseError("study_review_logs.outcome_json 不是有效 JSON")
