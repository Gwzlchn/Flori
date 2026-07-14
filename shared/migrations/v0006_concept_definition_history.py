"""为概念定义增加不可变版本和可验证的正规化证据出现。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import FunctionType

from . import (
    v0001_legacy_baseline,
    v0003_srs_consistency,
    v0004_study_suggestions,
    v0005_canonical_evidence,
)


VERSION = 6
NAME = "concept-definition-history"

_EMPTY_SOURCE_SET = "[]"
_EMPTY_SOURCE_SET_FINGERPRINT = hashlib.sha256(
    _EMPTY_SOURCE_SET.encode("utf-8")
).hexdigest()


CONCEPT_HISTORY_SCHEMA_SQL = f"""
CREATE UNIQUE INDEX idx_jobs_id_domain_identity ON jobs(id, domain);
CREATE UNIQUE INDEX idx_canonical_evidence_id_job_identity
    ON canonical_evidence(evidence_id, job_id);

CREATE TABLE concept_definition_versions (
    definition_version_id TEXT PRIMARY KEY
        CHECK(length(definition_version_id) = 68
              AND substr(definition_version_id, 1, 4) = 'cdv_'),
    domain TEXT NOT NULL CHECK(length(trim(domain)) > 0),
    term TEXT NOT NULL CHECK(length(trim(term)) > 0),
    version INTEGER NOT NULL
        CHECK(typeof(version) = 'integer' AND version > 0),
    definition TEXT NOT NULL DEFAULT '',
    source_evidence_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(source_evidence_ids_json)
              AND json_type(source_evidence_ids_json) = 'array'),
    source_set_fingerprint TEXT NOT NULL
        CHECK(length(source_set_fingerprint) = 64),
    strategy TEXT NOT NULL CHECK(length(trim(strategy)) > 0),
    provider TEXT,
    model TEXT,
    prompt_hash TEXT CHECK(prompt_hash IS NULL OR length(prompt_hash) = 64),
    input_hash TEXT CHECK(input_hash IS NULL OR length(input_hash) = 64),
    supersedes_version_id TEXT
        REFERENCES concept_definition_versions(definition_version_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    UNIQUE(domain, term, version),
    UNIQUE(domain, term, definition_version_id)
);
CREATE INDEX idx_concept_definition_versions_history
    ON concept_definition_versions(domain, term, version DESC);
CREATE INDEX idx_concept_definition_versions_source_set
    ON concept_definition_versions(domain, term, source_set_fingerprint);

ALTER TABLE glossary ADD COLUMN current_definition_version_id TEXT
    REFERENCES concept_definition_versions(definition_version_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE glossary ADD COLUMN lock_revision INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(lock_revision) = 'integer' AND lock_revision >= 0);

CREATE TABLE concept_occurrences (
    domain TEXT NOT NULL CHECK(length(trim(domain)) > 0),
    term TEXT NOT NULL CHECK(length(trim(term)) > 0),
    job_id TEXT NOT NULL CHECK(length(trim(job_id)) > 0),
    evidence_id TEXT NOT NULL
        CHECK(length(evidence_id) = 67 AND substr(evidence_id, 1, 3) = 'ce_'),
    created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    PRIMARY KEY(domain, term, job_id, evidence_id),
    FOREIGN KEY(domain, term) REFERENCES glossary(domain, term)
        ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(job_id, domain) REFERENCES jobs(id, domain)
        ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(evidence_id, job_id)
        REFERENCES canonical_evidence(evidence_id, job_id)
        ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX idx_concept_occurrences_job
    ON concept_occurrences(job_id, domain, term);
CREATE INDEX idx_concept_occurrences_evidence
    ON concept_occurrences(evidence_id);

CREATE TRIGGER trg_concept_definition_versions_no_update
BEFORE UPDATE ON concept_definition_versions
BEGIN
    SELECT RAISE(ABORT, 'concept definition versions are append-only');
END;

CREATE TRIGGER trg_concept_definition_versions_no_delete
BEFORE DELETE ON concept_definition_versions
BEGIN
    SELECT RAISE(ABORT, 'concept definition versions are append-only');
END;

CREATE TRIGGER trg_concept_definition_versions_chain_insert
BEFORE INSERT ON concept_definition_versions
BEGIN
    SELECT CASE WHEN NEW.version != COALESCE((
        SELECT MAX(previous.version) + 1
        FROM concept_definition_versions previous
        WHERE previous.domain = NEW.domain AND previous.term = NEW.term
    ), 1) THEN RAISE(
        ABORT, 'concept definition version chain must be contiguous'
    ) END;
    SELECT CASE WHEN NEW.version > 1
                      AND NEW.supersedes_version_id IS NULL
        THEN RAISE(
            ABORT, 'concept definition non-initial version requires predecessor'
        )
    END;
    SELECT CASE WHEN NEW.version > 1 AND NOT EXISTS (
        SELECT 1
        FROM concept_definition_versions previous
        WHERE previous.definition_version_id = NEW.supersedes_version_id
          AND previous.domain = NEW.domain
          AND previous.term = NEW.term
          AND previous.version = NEW.version - 1
    ) THEN RAISE(
        ABORT, 'concept definition predecessor must be previous identity version'
    ) END;
    -- 首版只允许从当前 identity 做显式 rename/merge 转移。
    SELECT CASE WHEN NEW.version = 1
                      AND NEW.supersedes_version_id IS NOT NULL
                      AND NOT EXISTS (
        SELECT 1
        FROM concept_definition_versions previous
        JOIN glossary source
          ON source.domain = previous.domain
         AND source.term = previous.term
         AND source.current_definition_version_id = previous.definition_version_id
        WHERE previous.definition_version_id = NEW.supersedes_version_id
          AND previous.version = (
              SELECT MAX(candidate.version)
              FROM concept_definition_versions candidate
              WHERE candidate.domain = previous.domain
                AND candidate.term = previous.term
          )
          AND (
              (NEW.strategy = 'domain_rename'
               AND previous.domain != NEW.domain
               AND previous.term = NEW.term)
              OR
              (NEW.strategy = 'concept_merge'
               AND previous.domain = NEW.domain
               AND previous.term != NEW.term)
          )
    ) THEN RAISE(
        ABORT, 'concept definition initial identity transfer is invalid'
    ) END;
END;

CREATE TRIGGER trg_concept_definition_versions_source_ids_insert
BEFORE INSERT ON concept_definition_versions
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.source_evidence_ids_json)
        WHERE type != 'text'
           OR length(value) != 67
           OR substr(value, 1, 3) != 'ce_'
           OR substr(value, 4) GLOB '*[^0-9a-f]*'
    ) THEN RAISE(ABORT, 'concept definition source evidence id invalid') END;
    SELECT CASE WHEN NEW.source_evidence_ids_json != COALESCE((
        SELECT json_group_array(value)
        FROM (
            SELECT DISTINCT value
            FROM json_each(NEW.source_evidence_ids_json)
            ORDER BY value
        )
    ), '[]') THEN RAISE(
        ABORT, 'concept definition source evidence ids must be canonical'
    ) END;
    -- domain rename 只复制已验证的 current source set;历史来源可先于定义删除。
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM json_each(NEW.source_evidence_ids_json) source
        LEFT JOIN canonical_evidence evidence
          ON evidence.evidence_id = source.value
        WHERE NOT (
                NEW.strategy = 'domain_rename'
                AND EXISTS (
                    SELECT 1
                    FROM concept_definition_versions previous
                    JOIN glossary previous_glossary
                      ON previous_glossary.domain = previous.domain
                     AND previous_glossary.term = previous.term
                     AND previous_glossary.current_definition_version_id =
                         previous.definition_version_id
                    WHERE previous.definition_version_id =
                          NEW.supersedes_version_id
                      AND previous.term = NEW.term
                      AND previous.domain != NEW.domain
                      AND previous.definition = NEW.definition
                      AND previous.source_evidence_ids_json =
                          NEW.source_evidence_ids_json
                      AND previous.source_set_fingerprint =
                          NEW.source_set_fingerprint
                      AND previous.version = (
                          SELECT MAX(candidate.version)
                          FROM concept_definition_versions candidate
                          WHERE candidate.domain = previous.domain
                            AND candidate.term = previous.term
                      )
                )
           )
           AND (
                evidence.evidence_id IS NULL
                OR evidence.status != 'valid'
                OR NOT EXISTS (
                    SELECT 1 FROM concept_occurrences occurrence
                    WHERE occurrence.domain = NEW.domain
                      AND occurrence.term = NEW.term
                      AND occurrence.job_id = evidence.job_id
                      AND occurrence.evidence_id = evidence.evidence_id
                )
           )
    ) THEN RAISE(
        ABORT, 'concept definition source evidence is not currently bound'
    ) END;
END;

CREATE TRIGGER trg_glossary_definition_pointer_insert
BEFORE INSERT ON glossary
WHEN NEW.current_definition_version_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM concept_definition_versions v
        WHERE v.definition_version_id = NEW.current_definition_version_id
          AND v.domain = NEW.domain AND v.term = NEW.term
          AND v.definition = COALESCE(NEW.definition, '')
          AND v.version = (
              SELECT MAX(candidate.version)
              FROM concept_definition_versions candidate
              WHERE candidate.domain = NEW.domain AND candidate.term = NEW.term
          )
    ) THEN RAISE(ABORT, 'glossary definition pointer mismatch') END;
END;

CREATE TRIGGER trg_glossary_definition_pointer_update
BEFORE UPDATE OF domain, term, definition, current_definition_version_id ON glossary
WHEN NEW.current_definition_version_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM concept_definition_versions v
        WHERE v.definition_version_id = NEW.current_definition_version_id
          AND v.domain = NEW.domain AND v.term = NEW.term
          AND v.definition = COALESCE(NEW.definition, '')
          AND v.version = (
              SELECT MAX(candidate.version)
              FROM concept_definition_versions candidate
              WHERE candidate.domain = NEW.domain AND candidate.term = NEW.term
          )
    ) THEN RAISE(ABORT, 'glossary definition pointer mismatch') END;
END;

CREATE TRIGGER trg_glossary_definition_pointer_not_null
BEFORE UPDATE OF domain, term, definition, current_definition_version_id ON glossary
WHEN NEW.current_definition_version_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'glossary current definition pointer must not be null');
END;

CREATE TRIGGER trg_glossary_lock_revision_update
BEFORE UPDATE ON glossary
BEGIN
    SELECT CASE WHEN typeof(NEW.definition_locked) != 'integer'
                      OR NEW.definition_locked NOT IN (0, 1)
        THEN RAISE(ABORT, 'glossary definition lock must be boolean')
    END;
    SELECT CASE WHEN NEW.definition_locked IS NOT OLD.definition_locked
                      AND NEW.lock_revision != OLD.lock_revision + 1
        THEN RAISE(ABORT, 'glossary lock revision must advance exactly once')
    END;
    SELECT CASE WHEN NEW.definition_locked IS OLD.definition_locked
                      AND NEW.lock_revision != OLD.lock_revision
                      AND NOT (
                          NEW.lock_revision = OLD.lock_revision + 1
                          AND NEW.current_definition_version_id IS NOT
                              OLD.current_definition_version_id
                          AND EXISTS (
                              SELECT 1 FROM concept_definition_versions v
                              WHERE v.definition_version_id =
                                    NEW.current_definition_version_id
                                AND v.supersedes_version_id =
                                    OLD.current_definition_version_id
                                AND (
                                    (
                                        v.strategy = 'domain_rename'
                                        AND NEW.domain != OLD.domain
                                        AND NEW.term = OLD.term
                                    )
                                    OR (
                                        v.strategy = 'concept_merge'
                                        AND NEW.domain = OLD.domain
                                        AND NEW.term = OLD.term
                                    )
                                )
                          )
                      )
        THEN RAISE(ABORT, 'glossary lock revision changed without lock transition')
    END;
END;

CREATE TRIGGER trg_glossary_legacy_definition_seed
AFTER INSERT ON glossary
WHEN NEW.current_definition_version_id IS NULL
BEGIN
    INSERT INTO concept_definition_versions (
        definition_version_id, domain, term, version, definition,
        source_evidence_ids_json, source_set_fingerprint, strategy,
        provider, model, prompt_hash, input_hash, supersedes_version_id,
        actor, created_at
    ) VALUES (
        'cdv_' || lower(hex(randomblob(32))), NEW.domain, NEW.term,
        COALESCE((
            SELECT MAX(version) + 1 FROM concept_definition_versions
            WHERE domain=NEW.domain AND term=NEW.term
        ), 1),
        COALESCE(NEW.definition, ''), '[]',
        '{_EMPTY_SOURCE_SET_FINGERPRINT}', 'legacy_insert',
        NULL, NULL, NULL, NULL, (
            SELECT definition_version_id FROM concept_definition_versions
            WHERE domain=NEW.domain AND term=NEW.term
            ORDER BY version DESC LIMIT 1
        ), 'database:legacy_insert',
        COALESCE(NEW.created_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    UPDATE glossary
       SET current_definition_version_id = (
           SELECT definition_version_id FROM concept_definition_versions
           WHERE domain = NEW.domain AND term = NEW.term
           ORDER BY version DESC LIMIT 1
       )
     WHERE domain = NEW.domain AND term = NEW.term;
END;
""".strip()


CURRENT_SCHEMA_SQL = (
    v0005_canonical_evidence.CURRENT_SCHEMA_SQL
    + "\n\n"
    + CONCEPT_HISTORY_SCHEMA_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


class _SemanticReplayBaseline:
    """历史 validator 只重放数据语义；当前完整 schema 已在外层校验。"""

    @staticmethod
    def _validate_complete_schema(
        _connection: sqlite3.Connection,
        _schema_sql: str,
    ) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(v0001_legacy_baseline, name)


def _replay_frozen_validator(
    connection: sqlite3.Connection,
    validator: Callable[[sqlite3.Connection], None],
) -> None:
    """在当前完整 schema 上重放冻结版本的语义检查，不改历史 payload。"""
    validator_globals = dict(validator.__globals__)
    validator_globals["v0001_legacy_baseline"] = _SemanticReplayBaseline()
    replay = FunctionType(
        validator.__code__,
        validator_globals,
        validator.__name__,
        validator.__defaults__,
        validator.__closure__,
    )
    replay(connection)


def _version_id(domain: str, term: str, definition: str) -> str:
    payload = json.dumps(
        {
            "domain": domain,
            "term": term,
            "version": 1,
            "definition": definition,
            "strategy": "legacy_migration",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cdv_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(
        connection, CONCEPT_HISTORY_SCHEMA_SQL
    )
    fallback_time = datetime.now(timezone.utc).isoformat()
    rows = connection.execute(
        "SELECT domain, term, definition, created_at, updated_at "
        "FROM glossary ORDER BY domain, term"
    ).fetchall()
    for row in rows:
        domain = str(row["domain"])
        term = str(row["term"])
        definition = str(row["definition"] or "")
        created_at = str(row["updated_at"] or row["created_at"] or fallback_time)
        version_id = _version_id(domain, term, definition)
        connection.execute(
            """INSERT INTO concept_definition_versions (
                   definition_version_id, domain, term, version, definition,
                   source_evidence_ids_json, source_set_fingerprint, strategy,
                   provider, model, prompt_hash, input_hash,
                   supersedes_version_id, actor, created_at
               ) VALUES (?, ?, ?, 1, ?, '[]', ?, 'legacy_migration',
                         NULL, NULL, NULL, NULL, NULL, 'migration:v6', ?)""",
            (
                version_id,
                domain,
                term,
                definition,
                _EMPTY_SOURCE_SET_FINGERPRINT,
                created_at,
            ),
        )
        connection.execute(
            """UPDATE glossary
               SET current_definition_version_id=?, lock_revision=0
               WHERE domain=? AND term=?""",
            (version_id, domain, term),
        )


def validate(connection: sqlite3.Connection) -> None:
    """校验概念版本、current pointer 与正规化 occurrence 身份。"""
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    # 历史 migration 文件与 checksum 不可修改；latest validator 仍须保留 SRS、
    # suggestion ledger 与 canonical evidence 的数据级不变量，而不把新增对象误判为 extra。
    for validator in (
        v0003_srs_consistency.validate,
        v0004_study_suggestions.validate,
        v0005_canonical_evidence.validate,
    ):
        _replay_frozen_validator(connection, validator)

    dangling = connection.execute(
        """SELECT g.domain, g.term
           FROM glossary g
           LEFT JOIN concept_definition_versions v
             ON v.definition_version_id=g.current_definition_version_id
            AND v.domain=g.domain AND v.term=g.term
            AND v.definition=COALESCE(g.definition, '')
            AND v.version=(
                SELECT MAX(candidate.version)
                FROM concept_definition_versions candidate
                WHERE candidate.domain=g.domain AND candidate.term=g.term
            )
           WHERE v.definition_version_id IS NULL
           LIMIT 1"""
    ).fetchone()
    if dangling is not None:
        raise sqlite3.DatabaseError(
            f"glossary current definition pointer 非法: {dangling[0]}/{dangling[1]}"
        )

    invalid_lock = connection.execute(
        """SELECT domain, term FROM glossary
           WHERE typeof(definition_locked) != 'integer'
              OR definition_locked NOT IN (0, 1)
              OR typeof(lock_revision) != 'integer'
              OR lock_revision < 0
           LIMIT 1"""
    ).fetchone()
    if invalid_lock is not None:
        raise sqlite3.DatabaseError(
            f"glossary lock state 非法: {invalid_lock[0]}/{invalid_lock[1]}"
        )

    # source identity 只在版本插入当下要求仍有效且绑定当前概念。历史版本必须在
    # job/evidence 正常删除或失效后继续可验证，因此这里只复核永久结构与指纹。
    for row in connection.execute(
        "SELECT definition_version_id, source_evidence_ids_json, "
        "source_set_fingerprint FROM concept_definition_versions"
    ).fetchall():
        version_id = str(row["definition_version_id"])
        try:
            evidence_ids = json.loads(str(row["source_evidence_ids_json"]))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise sqlite3.DatabaseError(
                f"concept definition source set 非法: {version_id}"
            ) from exc
        if (
            not isinstance(evidence_ids, list)
            or any(
                not isinstance(item, str)
                or len(item) != 67
                or not item.startswith("ce_")
                or any(char not in "0123456789abcdef" for char in item[3:])
                for item in evidence_ids
            )
            or evidence_ids != sorted(set(evidence_ids))
        ):
            raise sqlite3.DatabaseError(
                f"concept definition source set 不是有序唯一列表: {version_id}"
            )
        canonical = json.dumps(
            evidence_ids, ensure_ascii=False, separators=(",", ":")
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if row["source_evidence_ids_json"] != canonical or row["source_set_fingerprint"] != expected:
            raise sqlite3.DatabaseError(
                f"concept definition source set fingerprint 不匹配: {version_id}"
            )

    invalid_sequence = connection.execute(
        """SELECT domain, term
           FROM concept_definition_versions
           GROUP BY domain, term
           HAVING MIN(version) != 1 OR MAX(version) != COUNT(*)
           LIMIT 1"""
    ).fetchone()
    if invalid_sequence is not None:
        raise sqlite3.DatabaseError(
            "concept definition version chain 不连续: "
            f"{invalid_sequence[0]}/{invalid_sequence[1]}"
        )

    invalid_predecessor = connection.execute(
        """SELECT child.definition_version_id
           FROM concept_definition_versions child
           LEFT JOIN concept_definition_versions previous
             ON previous.domain=child.domain
            AND previous.term=child.term
            AND previous.version=child.version-1
           WHERE child.version > 1
             AND (
                 child.supersedes_version_id IS NULL
                 OR previous.definition_version_id IS NULL
                 OR child.supersedes_version_id != previous.definition_version_id
             )
           LIMIT 1"""
    ).fetchone()
    if invalid_predecessor is not None:
        raise sqlite3.DatabaseError(
            f"concept definition supersedes 非法: {invalid_predecessor[0]}"
        )

    invalid_transfer = connection.execute(
        """SELECT child.definition_version_id
           FROM concept_definition_versions child
           LEFT JOIN concept_definition_versions parent
             ON parent.definition_version_id=child.supersedes_version_id
           WHERE child.version=1
             AND child.supersedes_version_id IS NOT NULL
             AND NOT (
                 parent.definition_version_id IS NOT NULL
                 AND parent.version=(
                     SELECT MAX(candidate.version)
                     FROM concept_definition_versions candidate
                     WHERE candidate.domain=parent.domain
                       AND candidate.term=parent.term
                 )
                 AND (
                     (child.strategy='domain_rename'
                      AND parent.domain != child.domain
                      AND parent.term=child.term)
                     OR
                     (child.strategy='concept_merge'
                      AND parent.domain=child.domain
                      AND parent.term != child.term)
                 )
             )
           LIMIT 1"""
    ).fetchone()
    if invalid_transfer is not None:
        raise sqlite3.DatabaseError(
            f"concept definition identity transfer 非法: {invalid_transfer[0]}"
        )
