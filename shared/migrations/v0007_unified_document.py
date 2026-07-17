"""把论文和文章原子收敛为可扩展的 Document 内容族。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import v0001_legacy_baseline, v0006_concept_definition_history


VERSION = 7
NAME = "unified-document-content-family"


DOCUMENT_SCHEMA_SQL = """
ALTER TABLE jobs ADD COLUMN document_kind TEXT NOT NULL DEFAULT '';

ALTER TABLE prompt_overrides RENAME TO prompt_overrides_v6;
CREATE TABLE prompt_overrides (
    scope TEXT NOT NULL DEFAULT 'global',
    domain TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL,
    document_kind TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, domain, pipeline, document_kind, step)
);

ALTER TABLE prompt_override_versions RENAME TO prompt_override_versions_v6;
CREATE TABLE prompt_override_versions (
    scope TEXT NOT NULL DEFAULT 'global',
    domain TEXT NOT NULL DEFAULT '',
    pipeline TEXT NOT NULL,
    document_kind TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, domain, pipeline, document_kind, step, version)
);

INSERT INTO prompt_overrides (
    scope, domain, pipeline, document_kind, step, content, version, updated_at
)
SELECT scope, domain,
       CASE WHEN pipeline IN ('paper', 'article') THEN 'document' ELSE pipeline END,
       CASE pipeline
           WHEN 'paper' THEN 'research_paper'
           WHEN 'article' THEN 'article'
           ELSE ''
       END,
       CASE step
           WHEN '02_pdf_parse' THEN '02_parse'
           WHEN '02_parse_article' THEN '02_parse'
           WHEN '03_sections' THEN '03_structure'
           WHEN '03_article_sections' THEN '03_structure'
           WHEN '04_translate_paper' THEN '04_translate'
           WHEN '04_translate_article' THEN '04_translate'
           WHEN '05_smart_paper' THEN '05_smart'
           WHEN '04_smart_article' THEN '05_smart'
           WHEN '04_smart' THEN '05_smart'
           WHEN '05_semantic_attestation' THEN '06_semantic_attestation'
           WHEN '04_semantic_attestation' THEN '06_semantic_attestation'
           WHEN '05_concepts' THEN '07_concepts'
           WHEN '06_review' THEN '08_review'
           ELSE step
       END,
       content, version, updated_at
FROM prompt_overrides_v6;

INSERT INTO prompt_override_versions (
    scope, domain, pipeline, document_kind, step, version, content, note, created_at
)
SELECT scope, domain,
       CASE WHEN pipeline IN ('paper', 'article') THEN 'document' ELSE pipeline END,
       CASE pipeline
           WHEN 'paper' THEN 'research_paper'
           WHEN 'article' THEN 'article'
           ELSE ''
       END,
       CASE step
           WHEN '02_pdf_parse' THEN '02_parse'
           WHEN '02_parse_article' THEN '02_parse'
           WHEN '03_sections' THEN '03_structure'
           WHEN '03_article_sections' THEN '03_structure'
           WHEN '04_translate_paper' THEN '04_translate'
           WHEN '04_translate_article' THEN '04_translate'
           WHEN '05_smart_paper' THEN '05_smart'
           WHEN '04_smart_article' THEN '05_smart'
           WHEN '04_smart' THEN '05_smart'
           WHEN '05_semantic_attestation' THEN '06_semantic_attestation'
           WHEN '04_semantic_attestation' THEN '06_semantic_attestation'
           WHEN '05_concepts' THEN '07_concepts'
           WHEN '06_review' THEN '08_review'
           ELSE step
       END,
       version, content, note, created_at
FROM prompt_override_versions_v6;

DROP TABLE prompt_overrides_v6;
DROP TABLE prompt_override_versions_v6;

UPDATE job_steps
SET step = CASE step
    WHEN '02_pdf_parse' THEN '02_parse'
    WHEN '02_parse_article' THEN '02_parse'
    WHEN '03_sections' THEN '03_structure'
    WHEN '03_article_sections' THEN '03_structure'
    WHEN '04_translate_paper' THEN '04_translate'
    WHEN '04_translate_article' THEN '04_translate'
    WHEN '05_smart_paper' THEN '05_smart'
    WHEN '04_smart_article' THEN '05_smart'
    WHEN '04_smart' THEN '05_smart'
    WHEN '05_semantic_attestation' THEN '06_semantic_attestation'
    WHEN '04_semantic_attestation' THEN '06_semantic_attestation'
    WHEN '05_concepts' THEN '07_concepts'
    WHEN '06_review' THEN '08_review'
    ELSE step
END
WHERE job_id IN (SELECT id FROM jobs WHERE content_type IN ('paper', 'article'));

UPDATE jobs
SET document_kind = CASE content_type
        WHEN 'paper' THEN 'research_paper'
        WHEN 'article' THEN 'article'
        ELSE 'unknown'
    END,
    content_type = 'document',
    pipeline = 'document'
WHERE content_type NOT IN ('video', 'audio', 'document');

UPDATE jobs
SET document_kind = 'unknown'
WHERE content_type = 'document' AND trim(document_kind) = '';

UPDATE notes_fts5
SET content_type = 'document'
WHERE content_type IN ('paper', 'article');
UPDATE note_chunks
SET content_type = 'document'
WHERE content_type IN ('paper', 'article');
UPDATE note_chunks_fts5
SET content_type = 'document'
WHERE content_type IN ('paper', 'article');

CREATE TRIGGER trg_jobs_document_kind_insert
BEFORE INSERT ON jobs
BEGIN
    SELECT CASE
        WHEN NEW.content_type IN ('paper', 'article')
          OR (NEW.content_type = 'document' AND trim(NEW.document_kind) = '')
          OR (NEW.content_type != 'document' AND trim(NEW.document_kind) != '')
        THEN RAISE(ABORT, 'job document kind invariant failed')
    END;
END;

CREATE TRIGGER trg_jobs_document_kind_update
BEFORE UPDATE OF content_type, document_kind ON jobs
BEGIN
    SELECT CASE
        WHEN NEW.content_type IN ('paper', 'article')
          OR (NEW.content_type = 'document' AND trim(NEW.document_kind) = '')
          OR (NEW.content_type != 'document' AND trim(NEW.document_kind) != '')
        THEN RAISE(ABORT, 'job document kind invariant failed')
    END;
END;
""".strip()


CURRENT_SCHEMA_SQL = (
    v0006_concept_definition_history.CURRENT_SCHEMA_SQL
    + "\n\n"
    + DOCUMENT_SCHEMA_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def _rewrite_occurrences(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT domain, term, occurrences FROM glossary ORDER BY domain, term"
    ).fetchall()
    for row in rows:
        raw = str(row["occurrences"] or "[]")
        try:
            occurrences = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise sqlite3.DatabaseError(
                f"glossary occurrence JSON 非法: {row['domain']}/{row['term']}"
            ) from exc
        if not isinstance(occurrences, list):
            raise sqlite3.DatabaseError(
                f"glossary occurrence 不是数组: {row['domain']}/{row['term']}"
            )
        changed = False
        for item in occurrences:
            if not isinstance(item, dict):
                raise sqlite3.DatabaseError(
                    f"glossary occurrence 不是对象: {row['domain']}/{row['term']}"
                )
            old_content_type = item.get("content_type")
            if old_content_type in {"paper", "article"}:
                item["content_type"] = "document"
                item["document_kind"] = (
                    "research_paper" if old_content_type == "paper" else "article"
                )
                changed = True
            elif old_content_type == "document" and not item.get("document_kind"):
                item["document_kind"] = "unknown"
                changed = True
            elif old_content_type not in {"video", "audio", "document", None, ""}:
                item["content_type"] = "document"
                item["document_kind"] = "unknown"
                changed = True
        if changed:
            connection.execute(
                "UPDATE glossary SET occurrences=? WHERE domain=? AND term=?",
                (
                    json.dumps(occurrences, ensure_ascii=False, separators=(",", ":")),
                    row["domain"],
                    row["term"],
                ),
            )


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(connection, DOCUMENT_SCHEMA_SQL)
    _rewrite_occurrences(connection)


def validate(connection: sqlite3.Connection) -> None:
    """校验类型、索引、概念 occurrence 和 Prompt namespace 已完整切换。"""
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    # 当前 migration 只新增 Document 约束，不能替代前序版本的数据级校验。
    # 复用 v6 的语义重放入口，避免用旧 schema 清单把 v7 新对象误判为额外对象。
    v0006_concept_definition_history._replay_frozen_validator(
        connection,
        v0006_concept_definition_history.validate,
    )
    invalid_job = connection.execute(
        """SELECT id FROM jobs
           WHERE content_type IN ('paper', 'article')
              OR (content_type='document' AND trim(document_kind)='')
              OR (content_type!='document' AND trim(document_kind)!='')
           LIMIT 1"""
    ).fetchone()
    if invalid_job is not None:
        raise sqlite3.DatabaseError(f"job document kind 迁移不完整: {invalid_job[0]}")
    for table in ("notes_fts5", "note_chunks", "note_chunks_fts5"):
        row = connection.execute(
            f"SELECT rowid FROM {table} WHERE content_type IN ('paper','article') LIMIT 1"
        ).fetchone()
        if row is not None:
            raise sqlite3.DatabaseError(f"{table} 仍含旧 content_type")
    prompt = connection.execute(
        """SELECT pipeline, document_kind, step FROM prompt_overrides
           WHERE pipeline IN ('paper','article')
           LIMIT 1"""
    ).fetchone()
    if prompt is not None:
        raise sqlite3.DatabaseError("Prompt override namespace 迁移不完整")
    for row in connection.execute(
        "SELECT domain, term, occurrences FROM glossary"
    ).fetchall():
        occurrences = json.loads(str(row["occurrences"] or "[]"))
        if any(
            isinstance(item, dict) and item.get("content_type") in {"paper", "article"}
            for item in occurrences
        ):
            raise sqlite3.DatabaseError(
                f"glossary occurrence 仍含旧 content_type: {row['domain']}/{row['term']}"
            )
        if any(
            isinstance(item, dict)
            and item.get("content_type") == "document"
            and not str(item.get("document_kind") or "").strip()
            for item in occurrences
        ):
            raise sqlite3.DatabaseError(
                f"glossary document occurrence 缺少 document_kind: {row['domain']}/{row['term']}"
            )
