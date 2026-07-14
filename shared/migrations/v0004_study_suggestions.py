"""为证据型学习建议增加不可变快照和幂等操作账本."""

from __future__ import annotations

import hashlib
import base64
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import v0001_legacy_baseline, v0003_srs_consistency


VERSION = 4
NAME = "evidence-backed-study-suggestions"


SUGGESTION_SCHEMA_SQL = """
CREATE TABLE study_suggestion_batches (
    batch_id TEXT PRIMARY KEY CHECK(length(trim(batch_id)) > 0),
    domain TEXT NOT NULL CHECK(length(trim(domain)) > 0),
    status TEXT NOT NULL
        CHECK(status IN ('pending_enqueue','queued','ready','failed')),
    revision INTEGER NOT NULL DEFAULT 1
        CHECK(typeof(revision) = 'integer' AND revision BETWEEN 1 AND 9223372036854775807),
    attempt INTEGER NOT NULL DEFAULT 1
        CHECK(typeof(attempt) = 'integer' AND attempt BETWEEN 1 AND 9223372036854775807),
    generator_fingerprint TEXT NOT NULL
        CHECK(length(generator_fingerprint) = 71
              AND substr(generator_fingerprint, 1, 7) = 'sha256:'),
    input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint) = 64),
    task_id TEXT NOT NULL UNIQUE CHECK(length(trim(task_id)) > 0),
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    model TEXT NOT NULL CHECK(length(trim(model)) > 0),
    max_cards INTEGER NOT NULL
        CHECK(typeof(max_cards) = 'integer' AND max_cards BETWEEN 1 AND 50),
    llm_request_json TEXT NOT NULL CHECK(json_valid(llm_request_json)),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    error_code TEXT,
    error_message TEXT,
    deadline_at TEXT NOT NULL,
    deadline_at_epoch_us INTEGER NOT NULL
        CHECK(typeof(deadline_at_epoch_us) = 'integer'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(domain, input_fingerprint),
    CHECK(
        (status IN ('pending_enqueue','queued') AND result_json IS NULL
         AND error_code IS NULL AND error_message IS NULL)
        OR (status='ready' AND result_json IS NOT NULL
            AND error_code IS NULL AND error_message IS NULL)
        OR (status='failed' AND result_json IS NULL
            AND error_code IS NOT NULL AND length(trim(error_code)) > 0
            AND error_message IS NOT NULL AND length(trim(error_message)) > 0)
    )
);
CREATE INDEX idx_study_suggestion_batches_status
    ON study_suggestion_batches(status, deadline_at_epoch_us);
CREATE INDEX idx_study_suggestion_batches_domain
    ON study_suggestion_batches(domain, created_at);

CREATE TABLE study_suggestion_inputs (
    input_id TEXT PRIMARY KEY CHECK(length(trim(input_id)) > 0),
    batch_id TEXT NOT NULL REFERENCES study_suggestion_batches(batch_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL
        CHECK(typeof(ordinal) = 'integer' AND ordinal >= 0),
    kind TEXT NOT NULL CHECK(kind IN ('evidence','concept')),
    concept_term_snapshot TEXT,
    current_concept_term TEXT,
    input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, ordinal),
    UNIQUE(batch_id, input_fingerprint),
    UNIQUE(batch_id, input_id),
    CHECK(
        (kind='concept' AND concept_term_snapshot IS NOT NULL
         AND length(trim(concept_term_snapshot)) > 0
         AND current_concept_term IS NOT NULL
         AND length(trim(current_concept_term)) > 0)
        OR
        (kind='evidence' AND concept_term_snapshot IS NULL
         AND current_concept_term IS NULL)
    )
);
CREATE INDEX idx_study_suggestion_inputs_batch
    ON study_suggestion_inputs(batch_id, ordinal);

CREATE TABLE study_suggestion_evidence (
    evidence_id TEXT PRIMARY KEY CHECK(length(trim(evidence_id)) > 0),
    batch_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    job_id TEXT NOT NULL CHECK(length(trim(job_id)) > 0),
    chunk_id TEXT NOT NULL CHECK(length(trim(chunk_id)) > 0),
    note_type TEXT NOT NULL CHECK(length(trim(note_type)) > 0),
    source_domain_snapshot TEXT NOT NULL CHECK(length(trim(source_domain_snapshot)) > 0),
    current_domain TEXT NOT NULL CHECK(length(trim(current_domain)) > 0),
    title_snapshot TEXT NOT NULL DEFAULT '',
    section_snapshot TEXT NOT NULL DEFAULT '',
    body_snapshot TEXT NOT NULL CHECK(length(body_snapshot) > 0),
    body_sha256 TEXT NOT NULL CHECK(length(body_sha256) = 64),
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    status TEXT NOT NULL DEFAULT 'valid'
        CHECK(status IN ('valid','stale','unavailable')),
    invalid_reason TEXT,
    validated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(batch_id, input_id)
        REFERENCES study_suggestion_inputs(batch_id, input_id) ON DELETE RESTRICT,
    UNIQUE(batch_id, chunk_id),
    UNIQUE(batch_id, evidence_id),
    CHECK(
        (status='valid' AND invalid_reason IS NULL)
        OR (status IN ('stale','unavailable') AND invalid_reason IS NOT NULL
            AND length(trim(invalid_reason)) > 0)
    )
);
CREATE INDEX idx_study_suggestion_evidence_batch
    ON study_suggestion_evidence(batch_id, evidence_id);
CREATE INDEX idx_study_suggestion_evidence_job
    ON study_suggestion_evidence(job_id, note_type, chunk_id);
CREATE INDEX idx_study_suggestion_evidence_status
    ON study_suggestion_evidence(status, batch_id);

CREATE TABLE study_suggestions (
    suggestion_id TEXT PRIMARY KEY CHECK(length(trim(suggestion_id)) > 0),
    batch_id TEXT NOT NULL REFERENCES study_suggestion_batches(batch_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL
        CHECK(typeof(ordinal) = 'integer' AND ordinal >= 0),
    status TEXT NOT NULL DEFAULT 'suggested'
        CHECK(status IN ('suggested','accepted','rejected')),
    revision INTEGER NOT NULL DEFAULT 1
        CHECK(typeof(revision) = 'integer' AND revision BETWEEN 1 AND 9223372036854775807),
    domain TEXT NOT NULL CHECK(length(trim(domain)) > 0),
    concept_term TEXT,
    knowledge_key TEXT NOT NULL CHECK(length(trim(knowledge_key)) > 0),
    card_type TEXT NOT NULL CHECK(card_type IN ('basic','cloze','qa')),
    front TEXT NOT NULL CHECK(length(trim(front)) > 0),
    back TEXT NOT NULL CHECK(length(trim(back)) > 0),
    explanation TEXT NOT NULL DEFAULT '',
    knowledge_fingerprint TEXT NOT NULL CHECK(length(knowledge_fingerprint) = 64),
    content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint) = 64),
    accepted_card_id TEXT REFERENCES study_cards(card_id) ON DELETE RESTRICT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, ordinal),
    UNIQUE(batch_id, suggestion_id),
    UNIQUE(domain, knowledge_fingerprint),
    UNIQUE(domain, content_fingerprint),
    CHECK(
        (status='accepted' AND accepted_card_id IS NOT NULL
         AND length(trim(accepted_card_id)) > 0)
        OR (status!='accepted' AND accepted_card_id IS NULL)
    ),
    CHECK(
        (status='rejected' AND rejection_reason IS NOT NULL
         AND length(trim(rejection_reason)) > 0)
        OR (status!='rejected' AND rejection_reason IS NULL)
    )
);
CREATE INDEX idx_study_suggestions_batch_status
    ON study_suggestions(batch_id, status, ordinal);
CREATE INDEX idx_study_suggestions_domain_status
    ON study_suggestions(domain, status, updated_at);
CREATE INDEX idx_study_suggestions_concept
    ON study_suggestions(domain, concept_term);

CREATE TABLE study_suggestion_evidence_links (
    batch_id TEXT NOT NULL,
    suggestion_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL
        CHECK(typeof(ordinal) = 'integer' AND ordinal >= 0),
    quote_snapshot TEXT NOT NULL CHECK(length(quote_snapshot) > 0),
    quote_sha256 TEXT NOT NULL CHECK(length(quote_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(suggestion_id, evidence_id),
    UNIQUE(suggestion_id, ordinal),
    FOREIGN KEY(batch_id, suggestion_id)
        REFERENCES study_suggestions(batch_id, suggestion_id) ON DELETE RESTRICT,
    FOREIGN KEY(batch_id, evidence_id)
        REFERENCES study_suggestion_evidence(batch_id, evidence_id) ON DELETE RESTRICT
);
CREATE INDEX idx_study_suggestion_links_evidence
    ON study_suggestion_evidence_links(evidence_id, suggestion_id);

CREATE TABLE study_suggestion_operations (
    request_id TEXT PRIMARY KEY CHECK(length(trim(request_id)) BETWEEN 1 AND 128),
    ledger_seq INTEGER NOT NULL UNIQUE
        CHECK(typeof(ledger_seq) = 'integer' AND ledger_seq >= 1),
    previous_ledger_sha256 TEXT NOT NULL
        CHECK(length(previous_ledger_sha256) = 64),
    ledger_sha256 TEXT NOT NULL UNIQUE CHECK(length(ledger_sha256) = 64),
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    operation_kind TEXT NOT NULL
        CHECK(operation_kind IN (
            'batch_create','batch_queued','batch_ready','batch_failed','batch_retry',
            'suggestion_review','identity_transition'
        )),
    batch_id TEXT NOT NULL REFERENCES study_suggestion_batches(batch_id) ON DELETE RESTRICT,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    outcome_json TEXT NOT NULL CHECK(json_valid(outcome_json)),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_study_suggestion_operations_batch
    ON study_suggestion_operations(batch_id, ledger_seq);

CREATE TRIGGER study_suggestion_evidence_snapshot_immutable
BEFORE UPDATE OF batch_id, input_id, job_id, chunk_id, note_type,
    source_domain_snapshot, title_snapshot, section_snapshot, body_snapshot,
    body_sha256, locator_json, created_at
ON study_suggestion_evidence
BEGIN
    SELECT RAISE(ABORT, 'study suggestion evidence snapshot is immutable');
END;

CREATE TRIGGER study_suggestion_input_snapshot_immutable
BEFORE UPDATE OF batch_id, ordinal, kind, concept_term_snapshot,
    input_fingerprint, created_at
ON study_suggestion_inputs
BEGIN
    SELECT RAISE(ABORT, 'study suggestion input snapshot is immutable');
END;

CREATE TRIGGER study_suggestion_input_no_delete
BEFORE DELETE ON study_suggestion_inputs
BEGIN
    SELECT RAISE(ABORT, 'study suggestion input cannot be deleted');
END;

CREATE TRIGGER study_suggestion_evidence_no_delete
BEFORE DELETE ON study_suggestion_evidence
BEGIN
    SELECT RAISE(ABORT, 'study suggestion evidence cannot be deleted');
END;

CREATE TRIGGER study_suggestion_link_immutable
BEFORE UPDATE ON study_suggestion_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'study suggestion evidence link is immutable');
END;

CREATE TRIGGER study_suggestion_link_no_delete
BEFORE DELETE ON study_suggestion_evidence_links
BEGIN
    SELECT RAISE(ABORT, 'study suggestion evidence link cannot be deleted');
END;

CREATE TRIGGER study_suggestion_terminal_immutable
BEFORE UPDATE OF batch_id, ordinal, status, revision, knowledge_key, card_type,
    front, back, explanation, accepted_card_id, rejection_reason, created_at
ON study_suggestions
WHEN OLD.status IN ('accepted','rejected')
BEGIN
    SELECT RAISE(ABORT, 'terminal study suggestion is immutable');
END;

CREATE TRIGGER study_suggestion_batch_status_transition
BEFORE UPDATE OF status ON study_suggestion_batches
WHEN NOT (
    OLD.status=NEW.status
    OR (OLD.status='pending_enqueue' AND NEW.status='queued')
    OR (OLD.status='queued' AND NEW.status IN ('ready','failed'))
    OR (OLD.status='failed' AND NEW.status='pending_enqueue')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid study suggestion batch status transition');
END;

CREATE TRIGGER study_suggestion_batch_ready_immutable
BEFORE UPDATE OF status, revision, attempt, generator_fingerprint,
    input_fingerprint, task_id, provider, model, max_cards, llm_request_json,
    result_json, error_code, error_message, deadline_at, deadline_at_epoch_us,
    created_at
ON study_suggestion_batches
WHEN OLD.status='ready'
BEGIN
    SELECT RAISE(ABORT, 'ready study suggestion batch is immutable');
END;

CREATE TRIGGER study_suggestion_status_transition
BEFORE UPDATE OF status ON study_suggestions
WHEN NOT (
    OLD.status=NEW.status
    OR (OLD.status='suggested' AND NEW.status IN ('accepted','rejected'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid study suggestion status transition');
END;

CREATE TRIGGER study_suggestion_fingerprint_insert_guard
BEFORE INSERT ON study_suggestions
WHEN NEW.knowledge_fingerprint != flori_study_knowledge_fingerprint(
       NEW.domain, NEW.knowledge_key)
  OR NEW.content_fingerprint != flori_study_content_fingerprint(
       NEW.domain, NEW.card_type, NEW.front, NEW.back, NEW.explanation)
BEGIN
    SELECT RAISE(ABORT, 'study suggestion fingerprint mismatch');
END;

CREATE TRIGGER study_suggestion_fingerprint_update_guard
BEFORE UPDATE OF domain, knowledge_key, card_type, front, back, explanation,
    knowledge_fingerprint, content_fingerprint
ON study_suggestions
WHEN NEW.knowledge_fingerprint != flori_study_knowledge_fingerprint(
       NEW.domain, NEW.knowledge_key)
  OR NEW.content_fingerprint != flori_study_content_fingerprint(
       NEW.domain, NEW.card_type, NEW.front, NEW.back, NEW.explanation)
BEGIN
    SELECT RAISE(ABORT, 'study suggestion fingerprint mismatch');
END;

CREATE TRIGGER study_suggestion_evidence_status_transition
BEFORE UPDATE OF status ON study_suggestion_evidence
WHEN NOT (
    OLD.status=NEW.status
    OR (OLD.status='valid' AND NEW.status IN ('stale','unavailable'))
    OR (OLD.status='stale' AND NEW.status IN ('valid','unavailable'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid study suggestion evidence status transition');
END;

CREATE TRIGGER study_suggestion_operation_immutable
BEFORE UPDATE ON study_suggestion_operations
BEGIN
    SELECT RAISE(ABORT, 'study suggestion operation is immutable');
END;

CREATE TRIGGER study_suggestion_operation_no_delete
BEFORE DELETE ON study_suggestion_operations
BEGIN
    SELECT RAISE(ABORT, 'study suggestion operation cannot be deleted');
END;
""".strip()

CURRENT_SCHEMA_SQL = (
    v0003_srs_consistency.CURRENT_SCHEMA_SQL + "\n\n" + SUGGESTION_SCHEMA_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(connection, SUGGESTION_SCHEMA_SQL)


def validate(connection: sqlite3.Connection) -> None:
    """校验 v4 完整 schema 与不可变证据/操作账本语义."""
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)

    max_sqlite_integer = (1 << 63) - 1
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def fail(message: str) -> None:
        raise sqlite3.DatabaseError(message)

    def is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    def is_prefixed_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and value.startswith("sha256:")
            and is_sha256(value[7:])
        )

    def v4_canonical_json(value: object) -> str:
        """冻结 v4 JSON 编码,避免运行时 serializer 演进改写旧迁移语义."""
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def v4_payload_fingerprint(value: object) -> str:
        return hashlib.sha256(v4_canonical_json(value).encode("utf-8")).hexdigest()

    def v4_prefixed_fingerprint(value: object) -> str:
        return "sha256:" + v4_payload_fingerprint(value)

    def validate_prompt_snapshot(value: object, *, row_id: str) -> dict:
        if not isinstance(value, dict) or set(value) != {
            "name", "content_b64", "bytes", "sha256", "source", "version",
        }:
            fail(f"批次 Prompt 快照 schema 非法: {row_id}")
        if value.get("name") != "study_suggestions":
            fail(f"批次 Prompt name 非法: {row_id}")
        encoded = value.get("content_b64")
        if not isinstance(encoded, str) or not encoded:
            fail(f"批次 Prompt bytes 非法: {row_id}")
        try:
            raw = base64.b64decode(encoded, validate=True)
            raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            fail(f"批次 Prompt bytes 非法: {row_id}")
        if (
            not raw
            or type(value.get("bytes")) is not int
            or value["bytes"] != len(raw)
            or value.get("sha256") != "sha256:" + hashlib.sha256(raw).hexdigest()
            or value.get("source") not in {"override", "hot", "image"}
        ):
            fail(f"批次 Prompt 快照不匹配: {row_id}")
        version = value.get("version")
        if version is not None and (
            type(version) is not int or not 1 <= version < (1 << 63)
        ):
            fail(f"批次 Prompt version 非法: {row_id}")
        return value

    def v4_normalized_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold().strip()
        return re.sub(r"\s+", " ", normalized)

    def v4_knowledge_fingerprint(domain: str, knowledge_key: str) -> str:
        return v4_payload_fingerprint(
            {
                "domain": v4_normalized_text(domain),
                "knowledge_key": v4_normalized_text(knowledge_key),
            }
        )

    def v4_content_fingerprint(
        *,
        domain: str,
        card_type: str,
        front: str,
        back: str,
        explanation: str,
    ) -> str:
        return v4_payload_fingerprint(
            {
                "domain": v4_normalized_text(domain),
                "card_type": card_type,
                "front": v4_normalized_text(front),
                "back": v4_normalized_text(back),
                "explanation": v4_normalized_text(explanation),
            }
        )

    def v4_datetime_to_epoch_us(value: object, *, field: str) -> int:
        if not isinstance(value, str) or value != value.strip():
            fail(f"{field} 必须是带时区 ISO 8601 时间")
        candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise sqlite3.DatabaseError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            fail(f"{field} 必须带时区")
        delta = parsed.astimezone(timezone.utc) - epoch
        result = (
            delta.days * 86_400_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )
        if not -max_sqlite_integer - 1 <= result <= max_sqlite_integer:
            fail(f"{field} 超出 SQLite INTEGER 范围")
        return result

    def v4_identifier(value: object, *, field: str, maximum: int = 256) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > maximum
        ):
            fail(f"{field} 必须是 1..{maximum} 字符的归一化字符串")
        return value

    def v4_normalize_identifier(
        value: object, *, field: str, maximum: int = 256
    ) -> str:
        """冻结 v4 输入归一化:先 strip,再按归一化后的长度判界."""
        normalized = value.strip() if isinstance(value, str) else ""
        if not normalized or len(normalized) > maximum:
            fail(f"{field} 必须是 1..{maximum} 字符的非空字符串")
        return normalized

    def v4_positive_integer(value: object, *, field: str) -> int:
        if type(value) is not int or not 1 <= value <= max_sqlite_integer:
            fail(f"{field} 必须是 SQLite 64 位正整数")
        return value

    def v4_card_content(
        *,
        card_type: object,
        front: object,
        back: object,
        explanation: object,
        field: str,
    ) -> tuple[str, str, str, str]:
        if card_type not in {"basic", "cloze", "qa"}:
            fail(f"{field}.card_type 非法")
        if not isinstance(front, str) or not front.strip() or len(front) > 20_000:
            fail(f"{field}.front 非法")
        if not isinstance(back, str) or not back.strip() or len(back) > 100_000:
            fail(f"{field}.back 非法")
        if not isinstance(explanation, str) or len(explanation) > 100_000:
            fail(f"{field}.explanation 非法")
        return (
            str(card_type),
            front.strip(),
            back.strip(),
            explanation.strip(),
        )

    def load_json(
        raw: object,
        *,
        table: str,
        column: str,
        row_id: object,
        expected: type,
    ):
        try:
            value = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError(
                f"{table}.{column} 不是有效 JSON: {row_id}"
            ) from exc
        if not isinstance(value, expected):
            fail(f"{table}.{column} JSON 类型错误: {row_id}")
        if str(raw) != v4_canonical_json(value):
            fail(f"{table}.{column} 不是 canonical JSON: {row_id}")
        return value

    def v4_parse_result(
        value: dict,
        *,
        max_cards: int,
        evidence_ids: set[str],
        concept_ids: set[str],
        row_id: str,
    ) -> list[dict]:
        """冻结 v4 的 AI result schema,不跟随运行时 parser 演进."""
        if set(value) != {"schema_version", "suggestions"}:
            fail(f"批次 result 字段集非法: {row_id}")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            fail(f"批次 result schema_version 非法: {row_id}")
        suggestions = value.get("suggestions")
        if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= max_cards:
            fail(f"批次 result suggestions 数量非法: {row_id}")
        parsed: list[dict] = []
        seen_keys: set[str] = set()
        for index, item in enumerate(suggestions):
            if not isinstance(item, dict) or set(item) != {
                "knowledge_key", "concept_input_id", "card_type", "front", "back",
                "explanation", "evidence",
            }:
                fail(f"批次 result suggestion 字段非法: {row_id}/{index}")
            knowledge_key = v4_normalize_identifier(
                item.get("knowledge_key"),
                field=f"批次 result knowledge_key: {row_id}/{index}",
                maximum=512,
            )
            normalized_key = v4_normalized_text(knowledge_key)
            if normalized_key in seen_keys:
                fail(f"批次 result knowledge_key 重复: {row_id}")
            seen_keys.add(normalized_key)
            concept_id = item.get("concept_input_id")
            if concept_id is not None:
                concept_id = v4_normalize_identifier(
                    concept_id,
                    field=f"批次 result concept_input_id: {row_id}/{index}",
                )
                if concept_id not in concept_ids:
                    fail(f"批次 result concept_input_id 非法: {row_id}/{index}")
            card_type, front, back, explanation = v4_card_content(
                card_type=item.get("card_type"),
                front=item.get("front"),
                back=item.get("back"),
                explanation=item.get("explanation"),
                field=f"批次 result: {row_id}/{index}",
            )
            refs = item.get("evidence")
            if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
                fail(f"批次 result evidence 数量非法: {row_id}/{index}")
            seen_refs: set[str] = set()
            for ref in refs:
                if not isinstance(ref, dict) or set(ref) != {"evidence_id", "quote"}:
                    fail(f"批次 result evidence 字段非法: {row_id}/{index}")
                evidence_id = ref.get("evidence_id")
                quote = ref.get("quote")
                if not isinstance(evidence_id, str):
                    fail(f"批次 result evidence 引用非法: {row_id}/{index}")
                evidence_id = v4_normalize_identifier(
                    evidence_id,
                    field=f"批次 result evidence_id: {row_id}/{index}",
                )
                if (
                    evidence_id not in evidence_ids
                    or evidence_id in seen_refs
                    or not isinstance(quote, str)
                    or not quote.strip()
                    or len(quote) > 20_000
                ):
                    fail(f"批次 result evidence 引用非法: {row_id}/{index}")
                seen_refs.add(evidence_id)
            parsed.append(
                {
                    "knowledge_key": knowledge_key,
                    "concept_input_id": concept_id,
                    "card_type": card_type,
                    "front": front,
                    "back": back,
                    "explanation": explanation,
                    "evidence": [dict(ref) for ref in refs],
                }
            )
        return parsed

    def v4_operation_items(value: object, *, row_id: str) -> list[dict]:
        """冻结 v4 的人工操作 schema,只接受写路落盘的归一化形态."""
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            fail(f"suggestion_review items 数量非法: {row_id}")
        seen: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) != {
                "suggestion_id", "expected_revision", "action", "patch", "reason"
            }:
                fail(f"suggestion_review item 字段非法: {row_id}/{index}")
            suggestion_id = item.get("suggestion_id")
            revision = item.get("expected_revision")
            action = item.get("action")
            patch = item.get("patch")
            reason = item.get("reason")
            if not isinstance(suggestion_id, str):
                fail(f"suggestion_review item 值非法: {row_id}/{index}")
            suggestion_id = v4_identifier(
                suggestion_id,
                field=f"suggestion_review suggestion_id: {row_id}/{index}",
            )
            revision = v4_positive_integer(
                revision,
                field=f"suggestion_review expected_revision: {row_id}/{index}",
            )
            if (
                suggestion_id in seen
                or action not in {"edit", "accept", "reject"}
                or not isinstance(patch, dict)
                or set(patch) - {
                    "card_type", "front", "back", "explanation", "concept_term"
                }
            ):
                fail(f"suggestion_review item 值非法: {row_id}/{index}")
            seen.add(suggestion_id)
            if action == "edit" and not patch:
                fail(f"suggestion_review edit patch 为空: {row_id}/{index}")
            if action == "reject":
                if (
                    patch
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or reason != reason.strip()
                ):
                    fail(f"suggestion_review reject 非法: {row_id}/{index}")
            elif reason is not None:
                fail(f"suggestion_review 非 reject 带 reason: {row_id}/{index}")
            if isinstance(reason, str) and len(reason.strip()) > 2_000:
                fail(f"suggestion_review reason 过长: {row_id}/{index}")
            if "card_type" in patch and patch["card_type"] not in {"basic", "cloze", "qa"}:
                fail(f"suggestion_review patch card_type 非法: {row_id}/{index}")
            for field, maximum, allow_empty in (
                ("front", 20_000, False),
                ("back", 100_000, False),
                ("explanation", 100_000, True),
            ):
                if field not in patch:
                    continue
                raw = patch[field]
                if (
                    not isinstance(raw, str)
                    or len(raw) > maximum
                    or (not allow_empty and not raw.strip())
                ):
                    fail(f"suggestion_review patch {field} 非法: {row_id}/{index}")
            if "concept_term" in patch:
                raw_concept = patch["concept_term"]
                if raw_concept is not None and (
                    not isinstance(raw_concept, str) or len(raw_concept.strip()) > 256
                ):
                    fail(f"suggestion_review patch concept_term 非法: {row_id}/{index}")
        return value

    evidence_rows = connection.execute(
        "SELECT * FROM study_suggestion_evidence ORDER BY batch_id, evidence_id"
    ).fetchall()
    evidence_by_id = {str(row["evidence_id"]): row for row in evidence_rows}
    evidence_locator: dict[str, dict] = {}
    for row in evidence_rows:
        evidence_id = str(row["evidence_id"])
        v4_identifier(evidence_id, field="study_suggestion_evidence.evidence_id")
        for column in ("batch_id", "input_id", "job_id", "chunk_id", "note_type"):
            v4_identifier(
                row[column], field=f"study_suggestion_evidence.{column}: {evidence_id}"
            )
        for column in ("source_domain_snapshot", "current_domain"):
            v4_identifier(
                row[column], field=f"study_suggestion_evidence.{column}: {evidence_id}"
            )
        if not is_sha256(row["body_sha256"]):
            fail(f"证据正文 hash 格式错误: {evidence_id}")
        actual = hashlib.sha256(str(row["body_snapshot"]).encode("utf-8")).hexdigest()
        if actual != row["body_sha256"]:
            fail(f"证据正文 hash 不匹配: {evidence_id}")
        evidence_locator[evidence_id] = load_json(
            row["locator_json"],
            table="study_suggestion_evidence",
            column="locator_json",
            row_id=evidence_id,
            expected=dict,
        )

    input_rows = connection.execute(
        "SELECT * FROM study_suggestion_inputs ORDER BY batch_id, ordinal"
    ).fetchall()
    inputs_by_batch: dict[str, list[sqlite3.Row]] = {}
    evidence_by_input: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for evidence in evidence_rows:
        evidence_by_input.setdefault(
            (str(evidence["batch_id"]), str(evidence["input_id"])), []
        ).append(evidence)
    for row in input_rows:
        batch_id = str(row["batch_id"])
        input_id = str(row["input_id"])
        v4_identifier(input_id, field="study_suggestion_inputs.input_id")
        v4_identifier(batch_id, field=f"study_suggestion_inputs.batch_id: {input_id}")
        if type(row["ordinal"]) is not int or row["ordinal"] < 0:
            fail(f"建议输入 ordinal 非法: {input_id}")
        inputs_by_batch.setdefault(batch_id, []).append(row)
        if not is_sha256(row["input_fingerprint"]):
            fail(f"建议输入 fingerprint 格式错误: {input_id}")
        if row["kind"] == "concept":
            v4_identifier(
                row["concept_term_snapshot"],
                field=f"建议输入 concept_term_snapshot: {input_id}",
            )
            v4_identifier(
                row["current_concept_term"],
                field=f"建议输入 current_concept_term: {input_id}",
            )
            expected_input = v4_payload_fingerprint(
                {"kind": "concept", "term": row["concept_term_snapshot"]}
            )
            if evidence_by_input.get((batch_id, input_id)):
                fail(f"概念输入错误绑定证据: {input_id}")
        else:
            bound = evidence_by_input.get((batch_id, input_id), [])
            if len(bound) != 1:
                fail(f"证据输入必须精确绑定一条证据: {input_id}")
            evidence = bound[0]
            expected_input = v4_payload_fingerprint(
                {
                    "kind": "evidence",
                    "job_id": evidence["job_id"],
                    "chunk_id": evidence["chunk_id"],
                    "body_sha256": evidence["body_sha256"],
                }
            )
        if row["input_fingerprint"] != expected_input:
            fail(f"建议输入 fingerprint 不匹配: {input_id}")

    batch_rows = connection.execute(
        "SELECT * FROM study_suggestion_batches ORDER BY batch_id"
    ).fetchall()
    batches = {str(row["batch_id"]): row for row in batch_rows}
    batch_original_domains: dict[str, str] = {}
    batch_llm_requests: dict[str, dict] = {}
    batch_evidence_ids: dict[str, set[str]] = {}
    batch_evidence_jobs: dict[str, list[str]] = {}
    batch_concept_ids: dict[str, set[str]] = {}
    batch_concept_snapshots: dict[str, list[str]] = {}
    batch_identity_domains: dict[str, str] = {}
    batch_concept_states: dict[str, dict[str, str]] = {}
    suggestion_states: dict[str, dict] = {}
    suggestion_concept_input_ids: dict[str, str | None] = {}
    materialized_suggestion_ids: set[str] = set()
    for batch in batch_rows:
        batch_id = str(batch["batch_id"])
        v4_identifier(batch_id, field="study_suggestion_batches.batch_id")
        v4_identifier(
            batch["domain"], field=f"study_suggestion_batches.domain: {batch_id}"
        )
        v4_identifier(
            batch["task_id"], field=f"study_suggestion_batches.task_id: {batch_id}"
        )
        v4_identifier(
            batch["provider"],
            field=f"study_suggestion_batches.provider: {batch_id}",
            maximum=128,
        )
        v4_identifier(
            batch["model"], field=f"study_suggestion_batches.model: {batch_id}"
        )
        v4_positive_integer(
            batch["revision"], field=f"study_suggestion_batches.revision: {batch_id}"
        )
        v4_positive_integer(
            batch["attempt"], field=f"study_suggestion_batches.attempt: {batch_id}"
        )
        if type(batch["max_cards"]) is not int or not 1 <= batch["max_cards"] <= 50:
            fail(f"批次 max_cards 非法: {batch_id}")
        if not is_prefixed_sha256(batch["generator_fingerprint"]):
            fail(f"生成器 fingerprint 格式错误: {batch_id}")
        if not is_sha256(batch["input_fingerprint"]):
            fail(f"批次输入 fingerprint 格式错误: {batch_id}")
        deadline_epoch = v4_datetime_to_epoch_us(
            batch["deadline_at"], field=f"批次 deadline: {batch_id}"
        )
        if deadline_epoch != batch["deadline_at_epoch_us"]:
            fail(f"批次 deadline text/epoch 不匹配: {batch_id}")
        v4_datetime_to_epoch_us(
            batch["created_at"], field=f"批次 created_at: {batch_id}"
        )
        v4_datetime_to_epoch_us(
            batch["updated_at"], field=f"批次 updated_at: {batch_id}"
        )

        batch_inputs = inputs_by_batch.get(batch_id, [])
        batch_evidence = [
            evidence
            for input_row in batch_inputs
            for evidence in evidence_by_input.get(
                (batch_id, str(input_row["input_id"])), []
            )
        ]
        source_domains = {
            str(evidence["source_domain_snapshot"]) for evidence in batch_evidence
        }
        if len(source_domains) != 1:
            fail(f"批次证据来源 domain 快照不唯一: {batch_id}")
        original_domain = next(iter(source_domains))
        batch_original_domains[batch_id] = original_domain
        batch_identity_domains[batch_id] = original_domain
        chunk_facts = [
            {
                "chunk_id": str(evidence["chunk_id"]),
                "job_id": str(evidence["job_id"]),
                "note_type": str(evidence["note_type"]),
                "domain": str(evidence["source_domain_snapshot"]),
                "title": str(evidence["title_snapshot"]),
                "section": str(evidence["section_snapshot"]),
                "body_sha256": str(evidence["body_sha256"]),
                "locator": evidence_locator[str(evidence["evidence_id"])],
            }
            for evidence in batch_evidence
        ]
        concept_inputs = [row for row in batch_inputs if row["kind"] == "concept"]
        batch_evidence_ids[batch_id] = {
            str(row["evidence_id"]) for row in batch_evidence
        }
        batch_evidence_jobs[batch_id] = sorted(
            {str(row["job_id"]) for row in batch_evidence}
        )
        batch_concept_ids[batch_id] = {
            str(row["input_id"]) for row in concept_inputs
        }
        batch_concept_snapshots[batch_id] = [
            str(row["concept_term_snapshot"]) for row in concept_inputs
        ]
        batch_concept_states[batch_id] = {
            str(row["input_id"]): str(row["concept_term_snapshot"])
            for row in concept_inputs
        }
        request = load_json(
            batch["llm_request_json"],
            table="study_suggestion_batches",
            column="llm_request_json",
            row_id=batch_id,
            expected=dict,
        )
        prompt_snapshot = validate_prompt_snapshot(
            request.get("prompt_snapshot"), row_id=batch_id,
        )
        expected_generator = v4_prefixed_fingerprint(
            {
                "name": "study-suggestions",
                "schema_version": 1,
                "parser_version": 1,
                "prompt_sha256": prompt_snapshot["sha256"],
            }
        )
        if batch["generator_fingerprint"] != expected_generator:
            fail(f"生成器 fingerprint 与 Prompt 不匹配: {batch_id}")

        expected_batch_fingerprint = v4_payload_fingerprint(
            {
                "domain": original_domain,
                "chunks": chunk_facts,
                "concept_terms": [
                    str(row["concept_term_snapshot"]) for row in concept_inputs
                ],
                "max_cards": int(batch["max_cards"]),
                "provider": str(batch["provider"]),
                "model": str(batch["model"]),
                "generator_fingerprint": str(batch["generator_fingerprint"]),
                "prompt_snapshot": prompt_snapshot,
            }
        )
        if batch["input_fingerprint"] != expected_batch_fingerprint:
            fail(f"批次输入 fingerprint 不匹配: {batch_id}")

        expected_request = {
            "schema_version": 1,
            "batch_id": batch_id,
            "max_cards": int(batch["max_cards"]),
            "domain": original_domain,
            "concepts": [
                {
                    "input_id": str(row["input_id"]),
                    "term": str(row["concept_term_snapshot"]),
                }
                for row in concept_inputs
            ],
            "evidence": [
                {
                    "evidence_id": str(evidence["evidence_id"]),
                    "title": str(evidence["title_snapshot"]),
                    "section": str(evidence["section_snapshot"]),
                    "untrusted_body": str(evidence["body_snapshot"]),
                }
                for evidence in batch_evidence
            ],
            "prompt_snapshot": prompt_snapshot,
        }
        if request != expected_request:
            fail(f"批次 LLM request 与输入快照不匹配: {batch_id}")
        batch_llm_requests[batch_id] = expected_request

        if batch["result_json"] is not None:
            result = load_json(
                batch["result_json"],
                table="study_suggestion_batches",
                column="result_json",
                row_id=batch_id,
                expected=dict,
            )
            parsed = v4_parse_result(
                result,
                max_cards=int(batch["max_cards"]),
                evidence_ids={str(row["evidence_id"]) for row in batch_evidence},
                concept_ids={str(row["input_id"]) for row in concept_inputs},
                row_id=batch_id,
            )
            materialized_rows = connection.execute(
                "SELECT * FROM study_suggestions WHERE batch_id=? ORDER BY ordinal",
                (batch_id,),
            ).fetchall()
            if batch["status"] != "ready" or len(materialized_rows) != len(parsed):
                fail(f"批次 result 与物化候选不匹配: {batch_id}")
            concept_by_id = {
                str(row["input_id"]): row for row in concept_inputs
            }
            for ordinal, (item, suggestion) in enumerate(zip(parsed, materialized_rows)):
                suggestion_id = str(suggestion["suggestion_id"])
                v4_identifier(
                    suggestion_id,
                    field=f"物化 suggestion_id: {batch_id}/{ordinal}",
                )
                if (
                    int(suggestion["ordinal"]) != ordinal
                    or suggestion["knowledge_key"] != item["knowledge_key"]
                ):
                    fail(
                        f"批次 result 与候选 ordinal/knowledge 不匹配: "
                        f"{batch_id}/{ordinal}"
                    )
                concept_id = item["concept_input_id"]
                expected_current_concept = (
                    concept_by_id[str(concept_id)]["current_concept_term"]
                    if concept_id is not None else None
                )
                if suggestion["concept_term"] != expected_current_concept:
                    fail(f"批次 result 与候选 concept 不匹配: {batch_id}/{ordinal}")
                links = connection.execute(
                    """SELECT l.evidence_id, l.quote_snapshot
                       FROM study_suggestion_evidence_links l
                       WHERE l.suggestion_id=? ORDER BY l.ordinal""",
                    (suggestion_id,),
                ).fetchall()
                expected_refs = [
                    {"evidence_id": str(link["evidence_id"]), "quote": str(link["quote_snapshot"])}
                    for link in links
                ]
                if expected_refs != item["evidence"]:
                    fail(f"批次 result 与候选 evidence links 不匹配: {batch_id}/{ordinal}")
                suggestion_states[suggestion_id] = {
                    "batch_id": batch_id,
                    "ordinal": ordinal,
                    "domain": original_domain,
                    "status": "suggested",
                    "revision": 1,
                    "knowledge_key": item["knowledge_key"],
                    "concept_term": (
                        concept_by_id[str(concept_id)]["concept_term_snapshot"]
                        if concept_id is not None else None
                    ),
                    "card_type": item["card_type"],
                    "front": item["front"].strip(),
                    "back": item["back"].strip(),
                    "explanation": item["explanation"].strip(),
                    "accepted_card_id": None,
                    "rejection_reason": None,
                    "created_at": str(suggestion["created_at"]),
                    "updated_at": str(suggestion["created_at"]),
                    "evidence": expected_refs,
                }
                suggestion_concept_input_ids[suggestion_id] = (
                    str(concept_id) if concept_id is not None else None
                )
        elif connection.execute(
            "SELECT 1 FROM study_suggestions WHERE batch_id=? LIMIT 1", (batch_id,)
        ).fetchone() is not None:
            fail(f"无 result 的批次存在物化候选: {batch_id}")

    link_rows = connection.execute(
        """SELECT l.suggestion_id, l.evidence_id, l.quote_snapshot, l.quote_sha256,
                  e.body_snapshot, s.batch_id AS suggestion_batch,
                  e.batch_id AS evidence_batch
           FROM study_suggestion_evidence_links l
           JOIN study_suggestions s ON s.suggestion_id=l.suggestion_id
           JOIN study_suggestion_evidence e ON e.evidence_id=l.evidence_id"""
    ).fetchall()
    for suggestion_id, evidence_id, quote, expected_hash, body, s_batch, e_batch in link_rows:
        if not is_sha256(expected_hash):
            fail(f"建议证据 quote hash 格式错误: {suggestion_id}/{evidence_id}")
        actual = hashlib.sha256(str(quote).encode("utf-8")).hexdigest()
        if actual != expected_hash or str(quote) not in str(body) or s_batch != e_batch:
            fail(
                f"建议证据引用不匹配: {suggestion_id}/{evidence_id}"
            )

    suggestion_rows = connection.execute(
        "SELECT * FROM study_suggestions ORDER BY batch_id, ordinal"
    ).fetchall()
    current_suggestions = {
        str(row["suggestion_id"]): row for row in suggestion_rows
    }
    for row in suggestion_rows:
        suggestion_id = str(row["suggestion_id"])
        domain = str(row["domain"])
        concept_term = row["concept_term"]
        knowledge_key = str(row["knowledge_key"])
        card_type = str(row["card_type"])
        front = str(row["front"])
        back = str(row["back"])
        explanation = str(row["explanation"] or "")
        stored_knowledge = row["knowledge_fingerprint"]
        stored_content = row["content_fingerprint"]
        status = str(row["status"])
        accepted_card_id = row["accepted_card_id"]
        v4_identifier(suggestion_id, field="study_suggestions.suggestion_id")
        v4_identifier(domain, field=f"study_suggestions.domain: {suggestion_id}")
        v4_identifier(
            knowledge_key,
            field=f"study_suggestions.knowledge_key: {suggestion_id}",
            maximum=512,
        )
        v4_positive_integer(
            row["revision"], field=f"study_suggestions.revision: {suggestion_id}"
        )
        v4_card_content(
            card_type=card_type,
            front=front,
            back=back,
            explanation=explanation,
            field=f"study_suggestions: {suggestion_id}",
        )
        v4_datetime_to_epoch_us(
            row["created_at"], field=f"study_suggestions.created_at: {suggestion_id}"
        )
        v4_datetime_to_epoch_us(
            row["updated_at"], field=f"study_suggestions.updated_at: {suggestion_id}"
        )
        if stored_knowledge != v4_knowledge_fingerprint(
            str(domain), str(knowledge_key)
        ) or stored_content != v4_content_fingerprint(
            domain=str(domain),
            card_type=str(card_type),
            front=str(front),
            back=str(back),
            explanation=str(explanation or ""),
        ):
            raise sqlite3.DatabaseError(
                f"建议指纹不匹配: {suggestion_id}"
            )
        if status == "accepted":
            card = connection.execute(
                """SELECT domain, job_id, concept_term, card_type, front, back,
                          explanation, evidence_json, source
                   FROM study_cards WHERE card_id=?""",
                (accepted_card_id,),
            ).fetchone()
            if card is None or card["source"] != f"suggestion:{suggestion_id}":
                fail(
                    f"已接受建议未绑定对应卡片: {suggestion_id}"
                )
            if (
                card["domain"] != domain
                or card["concept_term"] != concept_term
                or card["card_type"] != card_type
                or card["front"] != front
                or card["back"] != back
                or str(card["explanation"] or "") != str(explanation or "")
            ):
                fail(f"已接受建议与卡片内容不匹配: {suggestion_id}")
            accepted_links = connection.execute(
                """SELECT l.evidence_id, l.quote_snapshot,
                          e.job_id, e.chunk_id, e.note_type, e.title_snapshot,
                          e.section_snapshot, e.body_sha256, e.locator_json
                   FROM study_suggestion_evidence_links l
                   JOIN study_suggestion_evidence e ON e.evidence_id=l.evidence_id
                   WHERE l.suggestion_id=? ORDER BY l.ordinal""",
                (suggestion_id,),
            ).fetchall()
            expected_evidence = [
                {
                    "evidence_id": link["evidence_id"],
                    "job_id": link["job_id"],
                    "chunk_id": link["chunk_id"],
                    "note_type": link["note_type"],
                    "title": link["title_snapshot"],
                    "section": link["section_snapshot"],
                    "quote": link["quote_snapshot"],
                    "body_sha256": link["body_sha256"],
                    "locator": evidence_locator[str(link["evidence_id"])],
                }
                for link in accepted_links
            ]
            card_evidence = load_json(
                card["evidence_json"],
                table="study_cards",
                column="evidence_json",
                row_id=accepted_card_id,
                expected=list,
            )
            if card_evidence != expected_evidence:
                fail(f"已接受卡片证据快照不匹配: {suggestion_id}")
            evidence_jobs = {str(link["job_id"]) for link in accepted_links}
            expected_job = next(iter(evidence_jobs)) if len(evidence_jobs) == 1 else None
            if card["job_id"] != expected_job:
                deleted_source = (
                    card["job_id"] is None
                    and expected_job is not None
                    and connection.execute(
                        "SELECT 1 FROM jobs WHERE id=?", (expected_job,)
                    ).fetchone() is None
                )
                if not deleted_source:
                    fail(f"已接受卡片 source job 不匹配: {suggestion_id}")

    suggestion_outcome_keys = {
        "suggestion_id", "batch_id", "ordinal", "status", "revision", "domain",
        "concept_term", "knowledge_key", "card_type", "front", "back",
        "explanation", "knowledge_fingerprint", "content_fingerprint",
        "accepted_card_id", "rejection_reason", "evidence", "created_at",
        "updated_at",
    }
    evidence_outcome_keys = {
        "evidence_id", "job_id", "chunk_id", "note_type", "source_domain",
        "current_domain", "title", "section", "quote", "quote_sha256",
        "body_sha256", "locator", "status", "invalid_reason",
    }
    card_outcome_keys = {
        "card_id", "domain", "job_id", "concept_term", "card_type", "front",
        "back", "explanation", "evidence", "status", "source", "revision",
        "created_at", "updated_at", "review",
    }
    batch_outcome_keys = {
        "batch_id", "domain", "status", "revision", "attempt",
        "generator_fingerprint", "input_fingerprint", "task_id", "provider",
        "model", "max_cards", "llm_request", "result", "error_code",
        "error_message", "deadline_at", "created_at", "updated_at",
    }

    def suggestion_evidence_snapshot(suggestion_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT l.evidence_id, l.quote_snapshot, l.quote_sha256,
                      e.job_id, e.chunk_id, e.note_type, e.source_domain_snapshot,
                      e.current_domain, e.title_snapshot, e.section_snapshot,
                      e.body_sha256, e.locator_json, e.status, e.invalid_reason
               FROM study_suggestion_evidence_links l
               JOIN study_suggestion_evidence e ON e.evidence_id=l.evidence_id
               WHERE l.suggestion_id=? ORDER BY l.ordinal""",
            (suggestion_id,),
        ).fetchall()
        return [
            {
                "evidence_id": str(row["evidence_id"]),
                "job_id": str(row["job_id"]),
                "chunk_id": str(row["chunk_id"]),
                "note_type": str(row["note_type"]),
                "source_domain": str(row["source_domain_snapshot"]),
                "current_domain": str(row["current_domain"]),
                "title": str(row["title_snapshot"]),
                "section": str(row["section_snapshot"]),
                "quote": str(row["quote_snapshot"]),
                "quote_sha256": str(row["quote_sha256"]),
                "body_sha256": str(row["body_sha256"]),
                "locator": evidence_locator[str(row["evidence_id"])],
                "status": str(row["status"]),
                "invalid_reason": row["invalid_reason"],
            }
            for row in rows
        ]

    def validate_outcome_evidence(
        value: object,
        *,
        suggestion_id: str,
        request_id: str,
    ) -> list[dict]:
        expected = suggestion_evidence_snapshot(suggestion_id)
        if not isinstance(value, list) or len(value) != len(expected):
            fail(f"suggestion_review outcome evidence 数量不匹配: {request_id}")
        immutable = evidence_outcome_keys - {
            "current_domain", "status", "invalid_reason"
        }
        for index, (actual, current) in enumerate(zip(value, expected)):
            if not isinstance(actual, dict) or set(actual) != evidence_outcome_keys:
                fail(f"suggestion_review outcome evidence schema 不匹配: {request_id}/{index}")
            if any(actual[key] != current[key] for key in immutable):
                fail(f"suggestion_review outcome evidence 快照不匹配: {request_id}/{index}")
            v4_identifier(
                actual["current_domain"],
                field=f"suggestion_review evidence current_domain: {request_id}/{index}",
            )
            status = actual["status"]
            reason = actual["invalid_reason"]
            if status not in {"valid", "stale", "unavailable"} or (
                (status == "valid" and reason is not None)
                or (
                    status != "valid"
                    and (not isinstance(reason, str) or not reason.strip())
                )
            ):
                fail(f"suggestion_review outcome evidence 状态非法: {request_id}/{index}")
        return value

    def validate_suggestion_outcome(
        value: object,
        *,
        state: dict,
        request_id: str,
        operation_time: str,
    ) -> dict:
        if not isinstance(value, dict) or set(value) != suggestion_outcome_keys:
            fail(f"suggestion_review outcome item schema 不匹配: {request_id}")
        suggestion_id = v4_identifier(
            value["suggestion_id"],
            field=f"suggestion_review outcome suggestion_id: {request_id}",
        )
        if suggestion_id not in current_suggestions:
            fail(f"suggestion_review outcome suggestion 不存在: {request_id}")
        v4_identifier(value["batch_id"], field=f"suggestion_review outcome batch: {request_id}")
        v4_identifier(value["domain"], field=f"suggestion_review outcome domain: {request_id}")
        if value["concept_term"] is not None:
            v4_identifier(
                value["concept_term"],
                field=f"suggestion_review outcome concept: {request_id}",
            )
        v4_identifier(
            value["knowledge_key"],
            field=f"suggestion_review outcome knowledge_key: {request_id}",
            maximum=512,
        )
        v4_positive_integer(
            value["revision"], field=f"suggestion_review outcome revision: {request_id}"
        )
        if type(value["ordinal"]) is not int or value["ordinal"] < 0:
            fail(f"suggestion_review outcome ordinal 非法: {request_id}")
        card_type, front, back, explanation = v4_card_content(
            card_type=value["card_type"],
            front=value["front"],
            back=value["back"],
            explanation=value["explanation"],
            field=f"suggestion_review outcome: {request_id}",
        )
        if (
            card_type != value["card_type"]
            or front != value["front"]
            or back != value["back"]
            or explanation != value["explanation"]
        ):
            fail(f"suggestion_review outcome 卡片内容未归一化: {request_id}")
        if value["knowledge_fingerprint"] != v4_knowledge_fingerprint(
            value["domain"], value["knowledge_key"]
        ) or value["content_fingerprint"] != v4_content_fingerprint(
            domain=value["domain"],
            card_type=value["card_type"],
            front=value["front"],
            back=value["back"],
            explanation=value["explanation"],
        ):
            fail(f"suggestion_review outcome fingerprint 不匹配: {request_id}")
        if value["created_at"] != state["created_at"] or value["updated_at"] != operation_time:
            fail(f"suggestion_review outcome 时间线不匹配: {request_id}")
        validate_outcome_evidence(
            value["evidence"], suggestion_id=suggestion_id, request_id=request_id
        )
        return value

    def accepted_card_evidence(suggestion_id: str) -> list[dict]:
        return [
            {
                "evidence_id": entry["evidence_id"],
                "job_id": entry["job_id"],
                "chunk_id": entry["chunk_id"],
                "note_type": entry["note_type"],
                "title": entry["title"],
                "section": entry["section"],
                "quote": entry["quote"],
                "body_sha256": entry["body_sha256"],
                "locator": entry["locator"],
            }
            for entry in suggestion_evidence_snapshot(suggestion_id)
        ]

    def validate_card_outcome(
        value: object,
        *,
        suggestion: dict,
        request_id: str,
        operation_time: str,
    ) -> None:
        if not isinstance(value, dict) or set(value) != card_outcome_keys:
            fail(f"suggestion_review outcome card schema 不匹配: {request_id}")
        card_id = v4_identifier(
            value["card_id"], field=f"suggestion_review outcome card_id: {request_id}"
        )
        evidence = accepted_card_evidence(str(suggestion["suggestion_id"]))
        evidence_jobs = {entry["job_id"] for entry in evidence}
        expected_job = next(iter(evidence_jobs)) if len(evidence_jobs) == 1 else None
        expected_review = {
            "due_at": operation_time,
            "interval_days": 0,
            "ease": 2.5,
            "repetitions": 0,
            "lapses": 0,
            "last_grade": None,
            "last_reviewed_at": None,
            "updated_at": operation_time,
        }
        if (
            card_id != suggestion["accepted_card_id"]
            or value["domain"] != suggestion["domain"]
            or value["job_id"] != expected_job
            or value["concept_term"] != suggestion["concept_term"]
            or value["card_type"] != suggestion["card_type"]
            or value["front"] != suggestion["front"]
            or value["back"] != suggestion["back"]
            or value["explanation"] != suggestion["explanation"]
            or value["evidence"] != evidence
            or value["status"] != "active"
            or value["source"] != f"suggestion:{suggestion['suggestion_id']}"
            or value["revision"] != 1
            or value["created_at"] != operation_time
            or value["updated_at"] != operation_time
            or value["review"] != expected_review
        ):
            fail(f"suggestion_review outcome card 与接受操作不匹配: {request_id}")

    def validate_batch_outcome(
        value: object,
        *,
        batch_id: str,
        request_id: str,
        expected_domain: str,
    ) -> dict:
        if not isinstance(value, dict) or set(value) != batch_outcome_keys:
            fail(f"批次操作 outcome schema 不匹配: {request_id}")
        batch = batches[batch_id]
        v4_identifier(value["batch_id"], field=f"批次 outcome batch_id: {request_id}")
        v4_identifier(value["domain"], field=f"批次 outcome domain: {request_id}")
        v4_identifier(value["task_id"], field=f"批次 outcome task_id: {request_id}")
        v4_positive_integer(value["revision"], field=f"批次 outcome revision: {request_id}")
        v4_positive_integer(value["attempt"], field=f"批次 outcome attempt: {request_id}")
        if value["status"] not in {"pending_enqueue", "queued", "ready", "failed"}:
            fail(f"批次 outcome status 非法: {request_id}")
        if (
            value["batch_id"] != batch_id
            or value["domain"] != expected_domain
            or value["generator_fingerprint"] != batch["generator_fingerprint"]
            or value["input_fingerprint"] != batch["input_fingerprint"]
            or value["provider"] != batch["provider"]
            or value["model"] != batch["model"]
            or value["max_cards"] != batch["max_cards"]
            or value["llm_request"] != batch_llm_requests[batch_id]
            or value["created_at"] != batch["created_at"]
        ):
            fail(f"批次 outcome 与输入快照不匹配: {request_id}")
        v4_datetime_to_epoch_us(
            value["deadline_at"], field=f"批次 outcome deadline: {request_id}"
        )
        v4_datetime_to_epoch_us(
            value["created_at"], field=f"批次 outcome created_at: {request_id}"
        )
        v4_datetime_to_epoch_us(
            value["updated_at"], field=f"批次 outcome updated_at: {request_id}"
        )
        if value["result"] is not None:
            if not isinstance(value["result"], dict):
                fail(f"批次 outcome result 类型非法: {request_id}")
            v4_parse_result(
                value["result"],
                max_cards=int(value["max_cards"]),
                evidence_ids=batch_evidence_ids[batch_id],
                concept_ids=batch_concept_ids[batch_id],
                row_id=f"outcome:{request_id}",
            )
            if value["status"] == "ready" and v4_canonical_json(value["result"]) != batch[
                "result_json"
            ]:
                fail(f"批次 ready outcome result 不匹配: {request_id}")
        if value["error_code"] is not None and (
            not isinstance(value["error_code"], str)
            or not value["error_code"].strip()
            or len(value["error_code"].strip()) > 128
        ):
            fail(f"批次 outcome error_code 非法: {request_id}")
        if value["error_message"] is not None and (
            not isinstance(value["error_message"], str)
            or not value["error_message"].strip()
            or len(value["error_message"].strip()) > 2_000
        ):
            fail(f"批次 outcome error_message 非法: {request_id}")
        if (
            value["status"] in {"pending_enqueue", "queued"}
            and (
                value["result"] is not None
                or value["error_code"] is not None
                or value["error_message"] is not None
            )
        ) or (
            value["status"] == "ready"
            and (
                value["result"] is None
                or value["error_code"] is not None
                or value["error_message"] is not None
            )
        ) or (
            value["status"] == "failed"
            and (
                value["result"] is not None
                or not isinstance(value["error_code"], str)
                or not value["error_code"].strip()
                or not isinstance(value["error_message"], str)
                or not value["error_message"].strip()
            )
        ):
            fail(f"批次 outcome 状态载荷不一致: {request_id}")
        return value

    def normalized_request_list(
        value: object,
        *,
        field: str,
        maximum_items: int = 100,
    ) -> list[str]:
        if not isinstance(value, list) or len(value) > maximum_items:
            fail(f"{field} 必须是至多 {maximum_items} 项列表")
        output = [v4_identifier(item, field=field) for item in value]
        if output != sorted(set(output)):
            fail(f"{field} 必须排序且不重复")
        return output

    batch_replay_states: dict[str, dict[str, object]] = {}
    seen_task_ids: set[str] = set()
    future_limit_epoch_us = (
        int((datetime.now(timezone.utc) - epoch).total_seconds() * 1_000_000)
        + 300_000_000
    )
    internal_request_prefixes = ("study-lifecycle:", "identity-transition:")
    external_operation_kinds = {
        "batch_create",
        "batch_retry",
        "suggestion_review",
    }

    def lifecycle_request_id(
        *,
        operation_kind: str,
        batch_id: str,
        task_id: str,
        attempt: int,
        expected_revision: int,
    ) -> str:
        identity = {
            "operation_kind": operation_kind,
            "batch_id": batch_id,
            "task_id": task_id,
            "attempt": attempt,
            "expected_revision": expected_revision,
        }
        return (
            f"study-lifecycle:{operation_kind}:"
            f"{v4_payload_fingerprint(identity)}"
        )

    operation_rows = connection.execute(
        "SELECT * FROM study_suggestion_operations ORDER BY ledger_seq"
    ).fetchall()
    ledger_chain_errors: list[str] = []
    previous_ledger_sha256 = "0" * 64
    previous_operation_epoch: int | None = None
    for expected_ledger_seq, operation in enumerate(operation_rows, 1):
        request_id = str(operation["request_id"])
        batch_id = str(operation["batch_id"])
        kind = str(operation["operation_kind"])
        ledger_seq = operation["ledger_seq"]
        if type(ledger_seq) is not int or ledger_seq != expected_ledger_seq:
            fail(
                "study suggestion operation ledger sequence 不连续: "
                f"{request_id}"
            )
        if (
            not is_sha256(operation["previous_ledger_sha256"])
            or not is_sha256(operation["ledger_sha256"])
        ):
            fail(f"study suggestion operation ledger hash 格式非法: {request_id}")
        expected_ledger_sha256 = v4_payload_fingerprint(
            {
                "ledger_seq": ledger_seq,
                "previous_ledger_sha256": str(operation["previous_ledger_sha256"]),
                "request_id": request_id,
                "request_fingerprint": str(operation["request_fingerprint"]),
                "operation_kind": kind,
                "batch_id": batch_id,
                "request_json": str(operation["request_json"]),
                "outcome_json": str(operation["outcome_json"]),
                "created_at": str(operation["created_at"]),
            }
        )
        if (
            operation["previous_ledger_sha256"] != previous_ledger_sha256
            or operation["ledger_sha256"] != expected_ledger_sha256
        ):
            ledger_chain_errors.append(request_id)
        previous_ledger_sha256 = expected_ledger_sha256
        v4_identifier(
            request_id, field="study_suggestion_operations.request_id", maximum=128
        )
        v4_identifier(
            batch_id, field=f"study_suggestion_operations.batch_id: {request_id}"
        )
        operation_time = str(operation["created_at"])
        operation_epoch = v4_datetime_to_epoch_us(
            operation_time, field=f"study_suggestion_operations.created_at: {request_id}"
        )
        if operation_epoch > future_limit_epoch_us:
            fail(f"建议操作 created_at 位于未来: {request_id}")
        if previous_operation_epoch is not None and operation_epoch < previous_operation_epoch:
            fail(f"建议操作时间在全局 ledger 中倒退: {request_id}")
        previous_operation_epoch = operation_epoch
        if batch_id not in batches:
            fail(f"建议操作引用不存在批次: {request_id}")
        if kind in external_operation_kinds and request_id.startswith(
            internal_request_prefixes
        ):
            fail(f"外部建议操作占用内部 request_id 命名空间: {request_id}")
        if kind == "identity_transition":
            identity_suffix = request_id.removeprefix("identity-transition:")
            if (
                not request_id.startswith("identity-transition:")
                or len(identity_suffix) != 32
                or any(char not in "0123456789abcdef" for char in identity_suffix)
            ):
                fail(f"identity_transition request_id 格式非法: {request_id}")
        request = load_json(
            operation["request_json"],
            table="study_suggestion_operations",
            column="request_json",
            row_id=request_id,
            expected=dict,
        )
        outcome = load_json(
            operation["outcome_json"],
            table="study_suggestion_operations",
            column="outcome_json",
            row_id=request_id,
            expected=dict,
        )
        if (
            operation["request_json"] != v4_canonical_json(request)
            or operation["outcome_json"] != v4_canonical_json(outcome)
        ):
            fail(f"建议操作 JSON 必须使用 canonical 编码: {request_id}")
        if not is_sha256(operation["request_fingerprint"]):
            fail(f"建议操作 request fingerprint 格式错误: {request_id}")
        if operation["request_fingerprint"] != v4_payload_fingerprint(request):
            fail(f"建议操作 request fingerprint 不匹配: {request_id}")
        if request.get("request_id") != request_id or request.get("operation_kind") != kind:
            fail(f"建议操作 request_id/kind 不匹配: {request_id}")
        if outcome.get("batch_id") != batch_id:
            fail(f"建议操作 outcome batch 不匹配: {request_id}")
        if kind == "batch_create":
            expected_fields = {
                "operation_kind", "request_id", "domain", "job_ids", "concept_terms",
                "max_cards", "provider", "model", "generator_fingerprint",
                "prompt_snapshot", "deadline_seconds",
            }
            if set(request) != expected_fields:
                fail(f"batch_create request schema 不匹配: {request_id}")
            batch = batches[batch_id]
            job_ids = normalized_request_list(
                request.get("job_ids"), field=f"batch_create job_ids: {request_id}"
            )
            concept_terms = normalized_request_list(
                request.get("concept_terms"),
                field=f"batch_create concept_terms: {request_id}",
            )
            if (
                request.get("domain") != batch_original_domains[batch_id]
                or request.get("max_cards") != batch["max_cards"]
                or request.get("provider") != batch["provider"]
                or request.get("model") != batch["model"]
                or request.get("generator_fingerprint") != batch["generator_fingerprint"]
                or request.get("prompt_snapshot")
                != batch_llm_requests[batch_id]["prompt_snapshot"]
                or type(request.get("deadline_seconds")) is not int
                or not 60 <= request["deadline_seconds"] <= 86_400
                or (job_ids and job_ids != batch_evidence_jobs[batch_id])
                or (
                    concept_terms
                    and concept_terms != batch_concept_snapshots[batch_id]
                )
            ):
                fail(f"batch_create request 与批次不匹配: {request_id}")
            batch_outcome = validate_batch_outcome(
                outcome,
                batch_id=batch_id,
                request_id=request_id,
                expected_domain=batch_identity_domains[batch_id],
            )
            replay_state = batch_replay_states.get(batch_id)
            if replay_state is None:
                deadline_epoch = v4_datetime_to_epoch_us(
                    batch_outcome["deadline_at"],
                    field=f"batch_create deadline: {request_id}",
                )
                if (
                    batch_outcome["created_at"] != operation_time
                    or batch_outcome["status"] != "pending_enqueue"
                    or batch_outcome["revision"] != 1
                    or batch_outcome["attempt"] != 1
                    or batch_outcome["updated_at"] != operation_time
                    or deadline_epoch - operation_epoch
                    != request["deadline_seconds"] * 1_000_000
                ):
                    fail(f"batch_create 初始 outcome 不匹配: {request_id}")
                if batch_outcome["task_id"] in seen_task_ids:
                    fail(f"batch_create task 时间线重复: {request_id}")
                seen_task_ids.add(str(batch_outcome["task_id"]))
                batch_replay_states[batch_id] = dict(batch_outcome)
            elif (
                operation_epoch
                < v4_datetime_to_epoch_us(
                    replay_state["updated_at"],
                    field=f"batch_create replay 前态 updated_at: {request_id}",
                )
                or batch_outcome != replay_state
            ):
                fail(f"batch_create replay outcome 时间线不匹配: {request_id}")
        elif kind == "batch_queued":
            expected_fields = {
                "operation_kind", "request_id", "batch_id", "task_id",
                "attempt", "expected_revision",
            }
            if set(request) != expected_fields or request.get("batch_id") != batch_id:
                fail(f"batch_queued request schema 不匹配: {request_id}")
            task_id = v4_identifier(
                request.get("task_id"), field=f"batch_queued task_id: {request_id}"
            )
            v4_positive_integer(
                request.get("attempt"), field=f"batch_queued attempt: {request_id}"
            )
            v4_positive_integer(
                request.get("expected_revision"),
                field=f"batch_queued expected_revision: {request_id}",
            )
            expected_request_id = lifecycle_request_id(
                operation_kind=kind,
                batch_id=batch_id,
                task_id=task_id,
                attempt=int(request["attempt"]),
                expected_revision=int(request["expected_revision"]),
            )
            replay_state = batch_replay_states.get(batch_id)
            if (
                request_id != expected_request_id
                or replay_state is None
                or replay_state["status"] != "pending_enqueue"
                or replay_state["task_id"] != task_id
                or replay_state["attempt"] != request["attempt"]
                or replay_state["revision"] != request["expected_revision"]
            ):
                fail(f"batch_queued lifecycle 前置不匹配: {request_id}")
            batch_outcome = validate_batch_outcome(
                outcome,
                batch_id=batch_id,
                request_id=request_id,
                expected_domain=str(replay_state["domain"]),
            )
            expected_outcome = {
                **replay_state,
                "status": "queued",
                "revision": int(replay_state["revision"]) + 1,
                "updated_at": operation_time,
            }
            if (
                operation_epoch
                < v4_datetime_to_epoch_us(
                    replay_state["updated_at"],
                    field=f"batch_queued 前态 updated_at: {request_id}",
                )
                or batch_outcome != expected_outcome
            ):
                fail(f"batch_queued outcome 不匹配: {request_id}")
            batch_replay_states[batch_id] = dict(batch_outcome)
        elif kind == "batch_ready":
            expected_fields = {
                "operation_kind", "request_id", "batch_id", "task_id",
                "attempt", "expected_revision", "result_sha256",
            }
            if set(request) != expected_fields or request.get("batch_id") != batch_id:
                fail(f"batch_ready request schema 不匹配: {request_id}")
            task_id = v4_identifier(
                request.get("task_id"), field=f"batch_ready task_id: {request_id}"
            )
            v4_positive_integer(
                request.get("attempt"), field=f"batch_ready attempt: {request_id}"
            )
            v4_positive_integer(
                request.get("expected_revision"),
                field=f"batch_ready expected_revision: {request_id}",
            )
            if not is_sha256(request.get("result_sha256")):
                fail(f"batch_ready result_sha256 非法: {request_id}")
            expected_request_id = lifecycle_request_id(
                operation_kind=kind,
                batch_id=batch_id,
                task_id=task_id,
                attempt=int(request["attempt"]),
                expected_revision=int(request["expected_revision"]),
            )
            replay_state = batch_replay_states.get(batch_id)
            if (
                request_id != expected_request_id
                or replay_state is None
                or replay_state["status"] != "queued"
                or replay_state["task_id"] != task_id
                or replay_state["attempt"] != request["attempt"]
                or replay_state["revision"] != request["expected_revision"]
            ):
                fail(f"batch_ready lifecycle 前置不匹配: {request_id}")
            batch_outcome = validate_batch_outcome(
                outcome,
                batch_id=batch_id,
                request_id=request_id,
                expected_domain=str(replay_state["domain"]),
            )
            expected_outcome = {
                **replay_state,
                "status": "ready",
                "revision": int(replay_state["revision"]) + 1,
                "result": batch_outcome["result"],
                "error_code": None,
                "error_message": None,
                "updated_at": operation_time,
            }
            if (
                operation_epoch
                < v4_datetime_to_epoch_us(
                    replay_state["updated_at"],
                    field=f"batch_ready 前态 updated_at: {request_id}",
                )
                or request["result_sha256"]
                != v4_payload_fingerprint(batch_outcome["result"])
                or batch_outcome != expected_outcome
            ):
                fail(f"batch_ready outcome 不匹配: {request_id}")
            ready_suggestion_ids = [
                suggestion_id
                for suggestion_id, state in suggestion_states.items()
                if state["batch_id"] == batch_id
            ]
            if (
                not ready_suggestion_ids
                or any(
                    suggestion_id in materialized_suggestion_ids
                    for suggestion_id in ready_suggestion_ids
                )
                or any(
                    suggestion_states[suggestion_id]["created_at"] != operation_time
                    for suggestion_id in ready_suggestion_ids
                )
            ):
                fail(f"batch_ready suggestions 时间不匹配: {request_id}")
            placeholders = ",".join("?" for _ in ready_suggestion_ids)
            link_times = connection.execute(
                f"""SELECT created_at FROM study_suggestion_evidence_links
                    WHERE suggestion_id IN ({placeholders})""",
                ready_suggestion_ids,
            ).fetchall()
            if not link_times or any(
                str(link["created_at"]) != operation_time for link in link_times
            ):
                fail(f"batch_ready evidence links 时间不匹配: {request_id}")
            for suggestion_id in ready_suggestion_ids:
                state = suggestion_states[suggestion_id]
                concept_input_id = suggestion_concept_input_ids[suggestion_id]
                state["domain"] = str(replay_state["domain"])
                state["concept_term"] = (
                    batch_concept_states[batch_id][concept_input_id]
                    if concept_input_id is not None
                    else None
                )
                state["updated_at"] = operation_time
                materialized_suggestion_ids.add(suggestion_id)
            batch_replay_states[batch_id] = dict(batch_outcome)
        elif kind == "batch_failed":
            expected_fields = {
                "operation_kind", "request_id", "batch_id", "task_id",
                "attempt", "expected_revision", "error_code", "error_message",
            }
            if set(request) != expected_fields or request.get("batch_id") != batch_id:
                fail(f"batch_failed request schema 不匹配: {request_id}")
            task_id = v4_identifier(
                request.get("task_id"), field=f"batch_failed task_id: {request_id}"
            )
            v4_positive_integer(
                request.get("attempt"), field=f"batch_failed attempt: {request_id}"
            )
            v4_positive_integer(
                request.get("expected_revision"),
                field=f"batch_failed expected_revision: {request_id}",
            )
            error_code = v4_identifier(
                request.get("error_code"),
                field=f"batch_failed error_code: {request_id}",
                maximum=128,
            )
            error_message = v4_identifier(
                request.get("error_message"),
                field=f"batch_failed error_message: {request_id}",
                maximum=2_000,
            )
            expected_request_id = lifecycle_request_id(
                operation_kind=kind,
                batch_id=batch_id,
                task_id=task_id,
                attempt=int(request["attempt"]),
                expected_revision=int(request["expected_revision"]),
            )
            replay_state = batch_replay_states.get(batch_id)
            if (
                request_id != expected_request_id
                or replay_state is None
                or replay_state["status"] != "queued"
                or replay_state["task_id"] != task_id
                or replay_state["attempt"] != request["attempt"]
                or replay_state["revision"] != request["expected_revision"]
            ):
                fail(f"batch_failed lifecycle 前置不匹配: {request_id}")
            batch_outcome = validate_batch_outcome(
                outcome,
                batch_id=batch_id,
                request_id=request_id,
                expected_domain=str(replay_state["domain"]),
            )
            expected_outcome = {
                **replay_state,
                "status": "failed",
                "revision": int(replay_state["revision"]) + 1,
                "result": None,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": operation_time,
            }
            if (
                operation_epoch
                < v4_datetime_to_epoch_us(
                    replay_state["updated_at"],
                    field=f"batch_failed 前态 updated_at: {request_id}",
                )
                or batch_outcome != expected_outcome
            ):
                fail(f"batch_failed outcome 不匹配: {request_id}")
            batch_replay_states[batch_id] = dict(batch_outcome)
        elif kind == "batch_retry":
            if set(request) != {
                "operation_kind", "request_id", "batch_id", "expected_revision",
                "deadline_seconds",
            } or request.get("batch_id") != batch_id:
                fail(f"batch_retry request schema 不匹配: {request_id}")
            if (
                type(request.get("expected_revision")) is not int
                or not 1 <= request["expected_revision"] <= max_sqlite_integer
                or type(request.get("deadline_seconds")) is not int
                or not 60 <= request["deadline_seconds"] <= 86_400
            ):
                fail(f"batch_retry request 值非法: {request_id}")
            replay_state = batch_replay_states.get(batch_id)
            if (
                replay_state is None
                or replay_state["status"] != "failed"
                or request["expected_revision"] != replay_state["revision"]
            ):
                fail(f"batch_retry 必须紧跟 failed lifecycle: {request_id}")
            batch_outcome = validate_batch_outcome(
                outcome,
                batch_id=batch_id,
                request_id=request_id,
                expected_domain=str(replay_state["domain"]),
            )
            deadline_epoch = v4_datetime_to_epoch_us(
                batch_outcome["deadline_at"],
                field=f"batch_retry deadline: {request_id}",
            )
            expected_outcome = {
                **replay_state,
                "status": "pending_enqueue",
                "revision": int(replay_state["revision"]) + 1,
                "attempt": int(replay_state["attempt"]) + 1,
                "task_id": batch_outcome["task_id"],
                "result": None,
                "error_code": None,
                "error_message": None,
                "deadline_at": batch_outcome["deadline_at"],
                "updated_at": operation_time,
            }
            if (
                operation_epoch
                < v4_datetime_to_epoch_us(
                    replay_state["updated_at"],
                    field=f"batch_retry 前态 updated_at: {request_id}",
                )
                or batch_outcome["task_id"] in seen_task_ids
                or deadline_epoch - operation_epoch
                != request["deadline_seconds"] * 1_000_000
                or batch_outcome != expected_outcome
            ):
                fail(f"batch_retry outcome 不匹配: {request_id}")
            seen_task_ids.add(str(batch_outcome["task_id"]))
            batch_replay_states[batch_id] = dict(batch_outcome)
        elif kind == "identity_transition":
            expected_fields = {
                "operation_kind", "request_id", "batch_id", "transition_kind",
                "source_domain", "target_domain", "source_concept", "target_concept",
            }
            if (
                set(request) != expected_fields
                or request.get("batch_id") != batch_id
                or set(outcome) != {"batch_id", "input_ids", "suggestion_ids"}
                or outcome.get("batch_id") != batch_id
            ):
                fail(f"identity_transition schema 不匹配: {request_id}")
            outcome_input_ids = normalized_request_list(
                outcome.get("input_ids"),
                field=f"identity_transition input_ids: {request_id}",
            )
            outcome_suggestion_ids = normalized_request_list(
                outcome.get("suggestion_ids"),
                field=f"identity_transition suggestion_ids: {request_id}",
            )
            source_domain = v4_identifier(
                request.get("source_domain"),
                field=f"identity_transition source_domain: {request_id}",
            )
            target_domain = v4_identifier(
                request.get("target_domain"),
                field=f"identity_transition target_domain: {request_id}",
            )
            transition_kind = request.get("transition_kind")
            replay_state = batch_replay_states.get(batch_id)
            if (
                replay_state is None
                or source_domain != batch_identity_domains[batch_id]
                or source_domain != replay_state["domain"]
            ):
                fail(f"identity_transition domain 时间线不匹配: {request_id}")
            if operation_epoch < v4_datetime_to_epoch_us(
                replay_state["updated_at"],
                field=f"identity_transition 前态 updated_at: {request_id}",
            ):
                fail(f"identity_transition 时间线倒退: {request_id}")
            if transition_kind == "domain_rename":
                if (
                    source_domain == target_domain
                    or request.get("source_concept") is not None
                    or request.get("target_concept") is not None
                ):
                    fail(f"identity_transition domain rename 非法: {request_id}")
                expected_suggestion_ids = sorted(
                    suggestion_id
                    for suggestion_id in materialized_suggestion_ids
                    if suggestion_states[suggestion_id]["batch_id"] == batch_id
                )
                if (
                    outcome_input_ids != []
                    or outcome_suggestion_ids != expected_suggestion_ids
                ):
                    fail(f"identity_transition domain 影响集合不匹配: {request_id}")
                if any(
                    operation_epoch
                    < v4_datetime_to_epoch_us(
                        suggestion_states[suggestion_id]["updated_at"],
                        field=(
                            "identity_transition candidate 前态 updated_at: "
                            f"{request_id}/{suggestion_id}"
                        ),
                    )
                    for suggestion_id in expected_suggestion_ids
                ):
                    fail(f"identity_transition candidate 时间线倒退: {request_id}")
                batch_identity_domains[batch_id] = target_domain
                batch_replay_states[batch_id] = {
                    **replay_state,
                    "domain": target_domain,
                    "updated_at": operation_time,
                }
                for suggestion_id in expected_suggestion_ids:
                    suggestion_states[suggestion_id]["domain"] = target_domain
                    suggestion_states[suggestion_id]["updated_at"] = operation_time
            elif transition_kind == "concept_merge":
                source_concept = v4_identifier(
                    request.get("source_concept"),
                    field=f"identity_transition source_concept: {request_id}",
                )
                target_concept = v4_identifier(
                    request.get("target_concept"),
                    field=f"identity_transition target_concept: {request_id}",
                )
                if source_domain != target_domain or source_concept == target_concept:
                    fail(f"identity_transition concept merge 非法: {request_id}")
                expected_input_ids = sorted(
                    input_id
                    for input_id, concept_term in batch_concept_states[batch_id].items()
                    if concept_term == source_concept
                )
                expected_suggestion_ids = sorted(
                    suggestion_id
                    for suggestion_id in materialized_suggestion_ids
                    if suggestion_states[suggestion_id]["batch_id"] == batch_id
                    and suggestion_states[suggestion_id]["concept_term"] == source_concept
                )
                if (
                    outcome_input_ids != expected_input_ids
                    or outcome_suggestion_ids != expected_suggestion_ids
                ):
                    fail(f"identity_transition concept 影响集合不匹配: {request_id}")
                if not expected_input_ids and not expected_suggestion_ids:
                    fail(f"identity_transition concept 无受影响指针: {request_id}")
                if any(
                    operation_epoch
                    < v4_datetime_to_epoch_us(
                        suggestion_states[suggestion_id]["updated_at"],
                        field=(
                            "identity_transition candidate 前态 updated_at: "
                            f"{request_id}/{suggestion_id}"
                        ),
                    )
                    for suggestion_id in expected_suggestion_ids
                ):
                    fail(f"identity_transition candidate 时间线倒退: {request_id}")
                for input_id in expected_input_ids:
                    batch_concept_states[batch_id][input_id] = target_concept
                for suggestion_id in expected_suggestion_ids:
                    suggestion_states[suggestion_id]["concept_term"] = target_concept
                    suggestion_states[suggestion_id]["updated_at"] = operation_time
            else:
                fail(f"identity_transition kind 非法: {request_id}")
        elif kind == "suggestion_review":
            if set(request) != {
                "operation_kind", "request_id", "batch_id", "items",
            } or request.get("batch_id") != batch_id:
                fail(f"suggestion_review request schema 不匹配: {request_id}")
            replay_state = batch_replay_states.get(batch_id)
            if (
                replay_state is None
                or replay_state["status"] != "ready"
                or operation_epoch
                < v4_datetime_to_epoch_us(
                    replay_state["updated_at"],
                    field=f"suggestion_review 前态 updated_at: {request_id}",
                )
            ):
                fail(f"suggestion_review 必须紧跟 ready lifecycle: {request_id}")
            normalized_items = v4_operation_items(
                request.get("items"), row_id=request_id
            )
            if (
                set(outcome) != {"batch_id", "items", "cards"}
                or not isinstance(outcome["items"], list)
                or not isinstance(outcome["cards"], list)
            ):
                fail(f"suggestion_review outcome schema 不匹配: {request_id}")
            if len(outcome["items"]) != len(normalized_items):
                fail(f"suggestion_review outcome items 数量不匹配: {request_id}")
            accepted_outcomes: list[dict] = []
            for request_item, outcome_item in zip(normalized_items, outcome["items"]):
                suggestion_id = str(request_item["suggestion_id"])
                state = suggestion_states.get(suggestion_id)
                if (
                    state is None
                    or suggestion_id not in materialized_suggestion_ids
                    or state["batch_id"] != batch_id
                ):
                    fail(f"suggestion_review request 跨批次: {request_id}/{suggestion_id}")
                if (
                    state["status"] != "suggested"
                    or state["revision"] != request_item["expected_revision"]
                    or state["revision"] == max_sqlite_integer
                ):
                    fail(f"suggestion_review request 状态/revision 不匹配: {request_id}")
                if operation_epoch < v4_datetime_to_epoch_us(
                    state["updated_at"],
                    field=(
                        "suggestion_review candidate 前态 updated_at: "
                        f"{request_id}/{suggestion_id}"
                    ),
                ):
                    fail(f"suggestion_review candidate 时间线倒退: {request_id}")
                actual = validate_suggestion_outcome(
                    outcome_item,
                    state=state,
                    request_id=request_id,
                    operation_time=operation_time,
                )
                if actual["suggestion_id"] != suggestion_id:
                    fail(f"suggestion_review outcome 顺序不匹配: {request_id}")
                if actual["domain"] != batch_identity_domains[batch_id]:
                    fail(f"suggestion_review outcome domain 时间线不匹配: {request_id}")
                patch = request_item["patch"]
                if "concept_term" in patch:
                    raw_concept = patch["concept_term"]
                    expected_concept = (
                        None
                        if raw_concept is None
                        or (isinstance(raw_concept, str) and not raw_concept.strip())
                        else str(raw_concept).strip()
                    )
                    if actual["concept_term"] != expected_concept:
                        fail(f"suggestion_review outcome concept patch 不匹配: {request_id}")
                elif actual["concept_term"] != state["concept_term"]:
                    fail(f"suggestion_review outcome concept 时间线不匹配: {request_id}")

                action = request_item["action"]
                if any(
                    entry["current_domain"] != actual["domain"]
                    for entry in actual["evidence"]
                ):
                    fail(f"suggestion_review evidence domain 时间线不匹配: {request_id}")
                if action == "accept" and any(
                    entry["status"] != "valid"
                    or entry["invalid_reason"] is not None
                    for entry in actual["evidence"]
                ):
                    fail(f"suggestion_review accept evidence 状态不匹配: {request_id}")
                if action == "reject":
                    expected_card_type = state["card_type"]
                    expected_front = state["front"]
                    expected_back = state["back"]
                    expected_explanation = state["explanation"]
                    expected_status = "rejected"
                    expected_reason = str(request_item["reason"]).strip()
                    expected_card_id = None
                else:
                    (
                        expected_card_type,
                        expected_front,
                        expected_back,
                        expected_explanation,
                    ) = v4_card_content(
                        card_type=patch.get("card_type", state["card_type"]),
                        front=patch.get("front", state["front"]),
                        back=patch.get("back", state["back"]),
                        explanation=patch.get("explanation", state["explanation"]),
                        field=f"suggestion_review replay: {request_id}",
                    )
                    expected_status = "accepted" if action == "accept" else "suggested"
                    expected_reason = None
                    expected_card_id = actual["accepted_card_id"] if action == "accept" else None
                    if action == "accept":
                        v4_identifier(
                            expected_card_id,
                            field=f"suggestion_review accepted_card_id: {request_id}",
                        )
                expected_values = {
                    "batch_id": batch_id,
                    "ordinal": state["ordinal"],
                    "status": expected_status,
                    "revision": state["revision"] + 1,
                    "knowledge_key": state["knowledge_key"],
                    "card_type": expected_card_type,
                    "front": expected_front,
                    "back": expected_back,
                    "explanation": expected_explanation,
                    "accepted_card_id": expected_card_id,
                    "rejection_reason": expected_reason,
                }
                if any(actual[key] != value for key, value in expected_values.items()):
                    fail(
                        f"suggestion_review outcome replay 不匹配: "
                        f"{request_id}/{suggestion_id}"
                    )
                state.update(
                    {
                        "domain": actual["domain"],
                        "concept_term": actual["concept_term"],
                        "status": expected_status,
                        "revision": state["revision"] + 1,
                        "card_type": expected_card_type,
                        "front": expected_front,
                        "back": expected_back,
                        "explanation": expected_explanation,
                        "accepted_card_id": expected_card_id,
                        "rejection_reason": expected_reason,
                        "updated_at": operation_time,
                    }
                )
                if action == "accept":
                    accepted_outcomes.append(actual)
            if len(outcome["cards"]) != len(accepted_outcomes):
                fail(f"suggestion_review outcome cards 数量不匹配: {request_id}")
            for card, accepted in zip(outcome["cards"], accepted_outcomes):
                validate_card_outcome(
                    card,
                    suggestion=accepted,
                    request_id=request_id,
                    operation_time=operation_time,
                )
        else:
            fail(f"建议操作 kind 非法: {request_id}")

    if set(batch_replay_states) != set(batches):
        fail("批次缺少可重放的 create 操作")
    for batch_id, batch in batches.items():
        current_result = (
            None
            if batch["result_json"] is None
            else load_json(
                batch["result_json"],
                table="study_suggestion_batches",
                column="result_json",
                row_id=batch_id,
                expected=dict,
            )
        )
        current_state = {
            "batch_id": batch_id,
            "domain": str(batch["domain"]),
            "status": str(batch["status"]),
            "revision": int(batch["revision"]),
            "attempt": int(batch["attempt"]),
            "generator_fingerprint": str(batch["generator_fingerprint"]),
            "input_fingerprint": str(batch["input_fingerprint"]),
            "task_id": str(batch["task_id"]),
            "provider": str(batch["provider"]),
            "model": str(batch["model"]),
            "max_cards": int(batch["max_cards"]),
            "llm_request": batch_llm_requests[batch_id],
            "result": current_result,
            "error_code": batch["error_code"],
            "error_message": batch["error_message"],
            "deadline_at": str(batch["deadline_at"]),
            "created_at": str(batch["created_at"]),
            "updated_at": str(batch["updated_at"]),
        }
        if (
            current_state != batch_replay_states[batch_id]
            or current_state["domain"] != batch_identity_domains[batch_id]
        ):
            fail(f"批次当前状态无法由 operation 重放: {batch_id}")
        for input_row in inputs_by_batch.get(batch_id, []):
            if input_row["kind"] != "concept":
                continue
            input_id = str(input_row["input_id"])
            if input_row["current_concept_term"] != batch_concept_states[batch_id][input_id]:
                fail(f"概念当前指针无法由 identity transition 重放: {input_id}")

    if (
        set(suggestion_states) != set(current_suggestions)
        or materialized_suggestion_ids != set(suggestion_states)
    ):
        fail("批次 result 与当前候选集合不匹配")
    for suggestion_id, state in suggestion_states.items():
        current = current_suggestions[suggestion_id]
        expected = {
            "batch_id": state["batch_id"],
            "ordinal": state["ordinal"],
            "domain": state["domain"],
            "concept_term": state["concept_term"],
            "status": state["status"],
            "revision": state["revision"],
            "knowledge_key": state["knowledge_key"],
            "card_type": state["card_type"],
            "front": state["front"],
            "back": state["back"],
            "explanation": state["explanation"],
            "accepted_card_id": state["accepted_card_id"],
            "rejection_reason": state["rejection_reason"],
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
        }
        if any(current[key] != value for key, value in expected.items()):
            fail(f"候选当前状态无法由 result/operation 重放: {suggestion_id}")

    if ledger_chain_errors:
        fail(
            "study suggestion operation ledger chain 不匹配: "
            f"{ledger_chain_errors[0]}"
        )
