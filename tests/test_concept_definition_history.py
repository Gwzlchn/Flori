"""验证概念定义历史和正规化 occurrence 的数据库不变量。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from shared.db import (
    ConceptConflictError,
    ConceptEvidenceError,
    ConceptNotFoundError,
    Database,
    SCHEMA_VERSION,
)
from shared.migrations.registry import current_migration_module


# 断言的是"当前 schema 的不变量", 不是某个具体版本号.
migration_current = current_migration_module()

_NOW = "2026-07-14T00:00:00+00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_job(
    database: Database,
    job_id: str,
    *,
    domain: str = "ml",
    content_type: str = "document",
) -> None:
    database._conn.execute(
        """INSERT INTO jobs
           (id, content_type, document_kind, pipeline, title, domain, status, is_current,
            created_at, updated_at)
           VALUES (?, ?, 'article', 'document', ?, ?, 'done', 1, ?, ?)""",
        (job_id, content_type, job_id, domain, _NOW, _NOW),
    )
    database._conn.commit()


def _insert_evidence(
    database: Database,
    job_id: str,
    segment_id: str,
    *,
    note_type: str = "smart",
) -> str:
    existing_count = int(
        database._conn.execute(
            "SELECT count(*) FROM note_chunks WHERE job_id=? AND note_type=?",
            (job_id, note_type),
        ).fetchone()[0]
    )
    chunk_id = f"{job_id}:{note_type}:{existing_count}"
    body = f"evidence body {segment_id}"
    body_sha = _sha(body)
    evidence_fingerprint = _sha(f"fingerprint:{job_id}:{segment_id}")
    identity = json.dumps(
        {
            "schema_version": 1,
            "job_id": job_id,
            "note_type": note_type,
            "chunk_id": chunk_id,
            "source_ref": "document:body",
            "source_segment_id": segment_id,
            "evidence_fingerprint": evidence_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_id = "ce_" + _sha(identity)
    locator_json = json.dumps(
        {
            "dom_path": None,
            "exact": segment_id,
            "kind": "text",
            "prefix": "",
            "suffix": "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    database._conn.execute(
        """INSERT INTO note_chunks
           (chunk_id, job_id, note_type, content_type, collection_id, domain,
            title, section, chunk_index, char_start, char_end, body,
            evidence_json, created_at, updated_at)
           SELECT ?, ?, ?, content_type, '', domain, title, 'section', ?, 0, ?, ?,
                  '{}', ?, ?
           FROM jobs WHERE id=?""",
        (
            chunk_id,
            job_id,
            note_type,
            existing_count,
            len(body),
            body,
            _NOW,
            _NOW,
            job_id,
        ),
    )
    database._conn.execute(
        """INSERT INTO canonical_evidence
           (evidence_id, schema_version, job_id, note_type, chunk_id, section,
            source_ref, source_segment_id, source_path, source_sha256,
            source_revision, note_path, note_sha256, provenance_path,
            provenance_sha256, chunk_body_sha256, chunk_char_start,
            chunk_char_end, locator_kind, locator_json, evidence_fingerprint,
            source_fingerprint, status, invalid_reason, validated_at,
            created_at, updated_at)
           VALUES (?,1,?,?,?,'section','document:body',?,'input/source.html',?,
                   NULL,'output/notes.md',?,'output/provenance/smart.json',?,
                   ?,0,?,'text',?,?,?,'valid',NULL,?,?,?)""",
        (
            evidence_id,
            job_id,
            note_type,
            chunk_id,
            segment_id,
            _sha(f"source:{job_id}"),
            _sha(f"note:{job_id}"),
            _sha(f"provenance:{job_id}"),
            body_sha,
            len(body),
            locator_json,
            evidence_fingerprint,
            _sha(f"source-fingerprint:{job_id}:{segment_id}"),
            _NOW,
            _NOW,
            _NOW,
        ),
    )
    row = database._conn.execute(
        "SELECT evidence_json FROM note_chunks WHERE chunk_id=?",
        (chunk_id,),
    ).fetchone()
    projection = json.loads(row["evidence_json"])
    projection["canonical_evidence_ids"] = [evidence_id]
    database._conn.execute(
        "UPDATE note_chunks SET evidence_json=? WHERE chunk_id=?",
        (
            json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            chunk_id,
        ),
    )
    database._conn.commit()
    return evidence_id


def _raw_insert_definition_version(
    database: Database,
    *,
    domain: str,
    term: str,
    definition: str,
    evidence_ids: list[str],
    strategy: str = "raw_test",
    supersedes_version_id: str | None = None,
    forced_version: int | None = None,
) -> str:
    source_json = json.dumps(
        sorted(set(evidence_ids)), ensure_ascii=False, separators=(",", ":")
    )
    version = forced_version
    if version is None:
        version = int(
            database._conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 "
                "FROM concept_definition_versions WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()[0]
        )
    version_id = "cdv_" + _sha(
        f"{domain}:{term}:{version}:{definition}:{source_json}:{strategy}"
    )
    database._conn.execute(
        """INSERT INTO concept_definition_versions (
               definition_version_id, domain, term, version, definition,
               source_evidence_ids_json, source_set_fingerprint, strategy,
               provider, model, prompt_hash, input_hash,
               supersedes_version_id, actor, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                     ?, 'test:raw', ?)""",
        (
            version_id,
            domain,
            term,
            version,
            definition,
            source_json,
            _sha(source_json),
            strategy,
            supersedes_version_id,
            _NOW,
        ),
    )
    return version_id


def test_current_schema_keeps_frozen_concept_and_document_invariants(db: Database) -> None:
    assert db.schema_version() == SCHEMA_VERSION
    migration_current.validate(db._conn)
    tables = {
        str(row[0])
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"concept_definition_versions", "concept_occurrences"} <= tables
    glossary_columns = {
        str(row["name"])
        for row in db._conn.execute("PRAGMA table_info(glossary)").fetchall()
    }
    assert {"current_definition_version_id", "lock_revision"} <= glossary_columns


def test_job_reconciliation_keeps_multiple_evidence_and_removes_disappeared_terms(
    db: Database,
) -> None:
    _insert_job(db, "job-1")
    first = _insert_evidence(db, "job-1", "segment:1")
    second = _insert_evidence(db, "job-1", "segment:2")
    db.upsert_glossary_term("ml", "RRF", "rank fusion")
    db.upsert_glossary_term("ml", "BM25", "lexical retrieval")

    mapping = {"RRF": [second, first], "BM25": [first]}
    assert db.replace_job_concept_occurrences(
        domain="ml", job_id="job-1", mapping=mapping
    )
    assert not db.replace_job_concept_occurrences(
        domain="ml", job_id="job-1", mapping=mapping
    )
    assert [
        item["evidence_id"] for item in db.list_concept_occurrences("ml", "RRF")
    ] == sorted([first, second])
    assert db.remove_concept_occurrence(
        domain="ml", term="RRF", job_id="job-1", evidence_id=second
    )
    assert not db.remove_concept_occurrence(
        domain="ml", term="RRF", job_id="job-1", evidence_id=second
    )
    assert db.upsert_concept_occurrence(
        domain="ml", term="RRF", job_id="job-1", evidence_id=second
    )
    assert not db.upsert_concept_occurrence(
        domain="ml", term="RRF", job_id="job-1", evidence_id=second
    )
    assert db.replace_concept_occurrences_for_job(
        domain="ml", term="RRF", job_id="job-1", evidence_ids=[second]
    )
    assert not db.replace_concept_occurrences_for_job(
        domain="ml", term="RRF", job_id="job-1", evidence_ids=[second]
    )

    assert db.replace_job_concept_occurrences(
        domain="ml", job_id="job-1", mapping={"RRF": [second]}
    )
    assert [
        item["evidence_id"] for item in db.list_concept_occurrences("ml", "RRF")
    ] == [second]
    assert db.list_concept_occurrences("ml", "BM25") == []


def test_occurrence_rejects_cross_job_domain_and_invalid_evidence(db: Database) -> None:
    _insert_job(db, "job-ml")
    _insert_job(db, "job-other", domain="other")
    first = _insert_evidence(db, "job-ml", "segment:1")
    other = _insert_evidence(db, "job-other", "segment:2")
    db.upsert_glossary_term("ml", "RRF", "rank fusion")
    db.upsert_glossary_term("other", "RRF", "other")

    with pytest.raises(ConceptEvidenceError, match="跨 job"):
        db.replace_job_concept_occurrences(
            domain="ml", job_id="job-ml", mapping={"RRF": [other]}
        )
    with pytest.raises(ConceptEvidenceError, match="domain"):
        db.replace_job_concept_occurrences(
            domain="other", job_id="job-ml", mapping={"RRF": [first]}
        )
    db._conn.execute(
        """UPDATE canonical_evidence
           SET status='stale', invalid_reason='changed' WHERE evidence_id=?""",
        (first,),
    )
    db._conn.commit()
    with pytest.raises(ConceptEvidenceError, match="当前无效"):
        db.replace_job_concept_occurrences(
            domain="ml", job_id="job-ml", mapping={"RRF": [first]}
        )

    with pytest.raises(sqlite3.IntegrityError):
        with db._conn:
            db._conn.execute(
                """INSERT INTO concept_occurrences
                   (domain,term,job_id,evidence_id,created_at)
                   VALUES ('ml','RRF','job-ml',?,?)""",
                (other, _NOW),
            )


def test_source_segment_mapping_only_returns_current_valid_ids(db: Database) -> None:
    _insert_job(db, "job-map")
    first = _insert_evidence(db, "job-map", "segment:1")
    second = _insert_evidence(db, "job-map", "segment:2")
    assert db.canonical_evidence_ids_for_source_segments(
        job_id="job-map",
        note_type="smart",
        source_segment_ids=["segment:2", "missing", "segment:1"],
    ) == {
        "segment:2": [second],
        "missing": [],
        "segment:1": [first],
    }
    db._conn.execute(
        """UPDATE canonical_evidence
           SET status='stale', invalid_reason='changed' WHERE evidence_id=?""",
        (second,),
    )
    db._conn.commit()
    assert db.canonical_evidence_ids_for_source_segments(
        job_id="job-map",
        note_type="smart",
        source_segment_ids=["segment:2", "segment:1"],
    ) == {"segment:2": [], "segment:1": [first]}


def test_concept_occurrence_projects_bound_chunk_excerpt(db: Database) -> None:
    _insert_job(db, "job-excerpt")
    evidence_id = _insert_evidence(db, "job-excerpt", "media fact")
    db.upsert_glossary_term("ml", "RRF", "rank fusion")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-excerpt", mapping={"RRF": [evidence_id]},
    )

    occurrence = db.list_concept_occurrences("ml", "RRF")[0]
    assert occurrence["evidence_excerpt"] == "evidence body media fact"
    assert occurrence["chunk_body_sha256"] == _sha(occurrence["evidence_excerpt"])


def test_definition_append_source_fingerprint_lock_and_history_are_cas_safe(
    db: Database,
) -> None:
    _insert_job(db, "job-def")
    evidence_id = _insert_evidence(db, "job-def", "segment:1")
    db.upsert_glossary_term("ml", "RRF", "old")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-def", mapping={"RRF": [evidence_id]}
    )
    initial = db.current_concept_definition("ml", "RRF")
    assert initial is not None

    synthesized = db.append_concept_definition_version(
        domain="ml",
        term="RRF",
        definition="new",
        evidence_ids=[evidence_id],
        strategy="cross_source",
        actor="test:synthesizer",
        expected_current_version_id=initial["definition_version_id"],
        expected_lock_revision=0,
    )
    assert synthesized["created"]
    no_op = db.append_concept_definition_version(
        domain="ml",
        term="RRF",
        definition="must not replace",
        evidence_ids=[evidence_id],
        strategy="cross_source",
        actor="test:synthesizer",
        expected_current_version_id=synthesized["definition_version_id"],
        expected_lock_revision=0,
    )
    assert not no_op["created"]
    assert db.get_glossary_term("ml", "RRF")["definition"] == "new"

    lock = db.set_concept_definition_lock(
        domain="ml",
        term="RRF",
        locked=True,
        expected_current_version_id=synthesized["definition_version_id"],
        expected_lock_revision=0,
    )
    assert lock == {
        "current_definition_version_id": synthesized["definition_version_id"],
        "lock_revision": 1,
        "locked": True,
        "changed": True,
    }
    with pytest.raises(ConceptConflictError, match="已锁定"):
        db.append_concept_definition_version(
            domain="ml",
            term="RRF",
            definition="blocked",
            evidence_ids=[],
            strategy="automatic",
            actor="test:synthesizer",
            expected_current_version_id=synthesized["definition_version_id"],
            expected_lock_revision=1,
        )
    with pytest.raises(ConceptConflictError, match="已变化"):
        db.set_concept_definition_lock(
            domain="ml",
            term="RRF",
            locked=False,
            expected_current_version_id=synthesized["definition_version_id"],
            expected_lock_revision=0,
        )
    db.set_concept_definition_lock(
        domain="ml",
        term="RRF",
        locked=False,
        expected_current_version_id=synthesized["definition_version_id"],
        expected_lock_revision=1,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db._conn.execute(
            """UPDATE concept_definition_versions SET definition='tampered'
               WHERE definition_version_id=?""",
            (initial["definition_version_id"],),
        )
    assert len(db.list_concept_definition_versions("ml", "RRF")) == 2


def test_definition_history_rejects_pointer_downgrade_bogus_sources_and_delete(
    db: Database,
) -> None:
    db.upsert_glossary_term("ml", "RRF", "definition")
    initial = db.current_concept_definition("ml", "RRF")
    assert initial is not None
    db.upsert_glossary_term("ml", "RRF", "latest")
    current = db.current_concept_definition("ml", "RRF")
    assert current is not None and current["version"] == 2

    with pytest.raises(sqlite3.IntegrityError, match="pointer mismatch"):
        with db._conn:
            db._conn.execute(
                """UPDATE glossary
                   SET current_definition_version_id=?, definition='definition'
                   WHERE domain='ml' AND term='RRF'""",
                (initial["definition_version_id"],),
            )
    assert db.current_concept_definition("ml", "RRF") == current

    with pytest.raises(sqlite3.IntegrityError, match="must not be null"):
        with db._conn:
            db._conn.execute(
                """UPDATE glossary
                   SET current_definition_version_id=NULL, definition='rewritten'
                   WHERE domain='ml' AND term='RRF'"""
            )
    assert db.current_concept_definition("ml", "RRF") == current

    bogus_sources = '["bogus"]'
    with pytest.raises(sqlite3.IntegrityError, match="source evidence id invalid"):
        with db._conn:
            db._conn.execute(
                """INSERT INTO concept_definition_versions (
                       definition_version_id, domain, term, version, definition,
                       source_evidence_ids_json, source_set_fingerprint, strategy,
                       provider, model, prompt_hash, input_hash,
                       supersedes_version_id, actor, created_at
                   ) VALUES (?, 'ml', 'RRF', 3, 'forged', ?, ?, 'test',
                             NULL, NULL, NULL, NULL, ?, 'test', ?)""",
                (
                    "cdv_" + "a" * 64,
                    bogus_sources,
                    _sha(bogus_sources),
                    current["definition_version_id"],
                    _NOW,
                ),
            )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db._conn:
            db._conn.execute(
                "DELETE FROM concept_definition_versions WHERE definition_version_id=?",
                (current["definition_version_id"],),
            )
    assert db.current_concept_definition("ml", "RRF") == current


def test_manual_definition_cas_clears_inherited_evidence_and_is_atomic(
    db: Database,
) -> None:
    _insert_job(db, "job-manual")
    evidence_id = _insert_evidence(db, "job-manual", "segment:manual")
    db.upsert_glossary_term("ml", "RRF", "initial")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-manual", mapping={"RRF": [evidence_id]},
    )
    initial = db.current_concept_definition("ml", "RRF")
    assert initial is not None
    synthesized = db.append_concept_definition_version(
        domain="ml",
        term="RRF",
        definition="evidence backed",
        evidence_ids=[evidence_id],
        strategy="cross_source",
        actor="test:synthesizer",
        expected_current_version_id=initial["definition_version_id"],
        expected_lock_revision=0,
    )

    manual = db.update_glossary_definition_cas(
        domain="ml",
        term="RRF",
        definition="human rewrite",
        related=[{"term": "BM25", "rel": "related"}],
        expected_current_version_id=synthesized["definition_version_id"],
        expected_lock_revision=0,
        actor="api:manual_edit",
    )

    assert manual["created"] is True
    assert manual["source_evidence_ids"] == []
    assert manual["source_set_fingerprint"] == _sha("[]")
    row = db.get_glossary_term("ml", "RRF")
    assert row is not None
    assert row["definition"] == "human rewrite"
    assert row["related"] == [{"term": "BM25", "rel": "related"}]
    assert row["current_definition_version_id"] == manual["definition_version_id"]

    before = len(db.list_concept_definition_versions("ml", "RRF"))
    db.update_glossary_definition_cas(
        domain="ml",
        term="RRF",
        definition=None,
        related=[{"term": "RRF paper", "rel": "related"}],
        expected_current_version_id=None,
        expected_lock_revision=None,
        actor="api:manual_edit",
    )
    assert len(db.list_concept_definition_versions("ml", "RRF")) == before
    assert db.get_glossary_term("ml", "RRF")["definition"] == "human rewrite"


def test_legacy_upsert_cannot_rewrite_locked_definition_and_clears_evidence(
    db: Database,
) -> None:
    _insert_job(db, "job-upsert")
    evidence_id = _insert_evidence(db, "job-upsert", "segment:upsert")
    db.upsert_glossary_term("ml", "RRF", "initial")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-upsert", mapping={"RRF": [evidence_id]},
    )
    initial = db.current_concept_definition("ml", "RRF")
    assert initial is not None
    synthesized = db.append_concept_definition_version(
        domain="ml",
        term="RRF",
        definition="evidence backed",
        evidence_ids=[evidence_id],
        strategy="cross_source",
        actor="test:synthesizer",
        expected_current_version_id=initial["definition_version_id"],
        expected_lock_revision=0,
    )

    db.upsert_glossary_term("ml", "RRF", "manual rewrite")
    manual = db.current_concept_definition("ml", "RRF")
    assert manual is not None
    assert manual["source_evidence_ids"] == []
    assert manual["supersedes_version_id"] == synthesized[
        "definition_version_id"
    ]
    lock = db.set_concept_definition_lock(
        domain="ml",
        term="RRF",
        locked=True,
        expected_current_version_id=manual["definition_version_id"],
        expected_lock_revision=0,
    )
    with pytest.raises(ConceptConflictError, match="已锁定"):
        db.upsert_glossary_term("ml", "RRF", "blocked rewrite")
    after = db.get_glossary_term("ml", "RRF")
    assert after is not None
    assert after["definition"] == "manual rewrite"
    assert after["lock_revision"] == lock["lock_revision"]

def test_raw_lock_revision_transitions_reject_stale_jump_and_spurious_bumps(
    db: Database,
) -> None:
    db.upsert_glossary_term("ml", "RRF", "definition")

    for locked, revision in ((1, 0), (1, 2), (0, 1)):
        with pytest.raises(sqlite3.IntegrityError, match="lock revision"):
            with db._conn:
                db._conn.execute(
                    """UPDATE glossary SET definition_locked=?, lock_revision=?
                       WHERE domain='ml' AND term='RRF'""",
                    (locked, revision),
                )

    with db._conn:
        db._conn.execute(
            """UPDATE glossary SET definition_locked=1, lock_revision=1
               WHERE domain='ml' AND term='RRF'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="without lock transition"):
        with db._conn:
            db._conn.execute(
                """UPDATE glossary SET lock_revision=2
                   WHERE domain='ml' AND term='RRF'"""
            )
    with pytest.raises(sqlite3.IntegrityError, match="advance exactly once"):
        with db._conn:
            db._conn.execute(
                """UPDATE glossary SET definition_locked=0, lock_revision=0
                   WHERE domain='ml' AND term='RRF'"""
            )
    with db._conn:
        db._conn.execute(
            """UPDATE glossary SET definition_locked=0, lock_revision=2
               WHERE domain='ml' AND term='RRF'"""
        )
    assert db.get_glossary_term("ml", "RRF")["lock_revision"] == 2


def test_raw_definition_sources_require_live_bound_canonical_evidence(
    db: Database,
) -> None:
    _insert_job(db, "job-bound")
    bound = _insert_evidence(db, "job-bound", "segment:bound")
    unbound = _insert_evidence(db, "job-bound", "segment:unbound")
    db.upsert_glossary_term("ml", "RRF", "definition")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-bound", mapping={"RRF": [bound]}
    )
    current = db.current_concept_definition("ml", "RRF")
    assert current is not None

    nonexistent = "ce_" + "f" * 64
    for evidence_id in (nonexistent, unbound):
        with pytest.raises(sqlite3.IntegrityError, match="not currently bound"):
            with db._conn:
                _raw_insert_definition_version(
                    db,
                    domain="ml",
                    term="RRF",
                    definition="forged",
                    evidence_ids=[evidence_id],
                    supersedes_version_id=current["definition_version_id"],
                )

    _insert_job(db, "job-other", domain="other")
    cross_domain = _insert_evidence(db, "job-other", "segment:other")
    db.upsert_glossary_term("other", "RRF", "other definition")
    db.replace_job_concept_occurrences(
        domain="other", job_id="job-other", mapping={"RRF": [cross_domain]}
    )
    with pytest.raises(sqlite3.IntegrityError, match="not currently bound"):
        with db._conn:
            _raw_insert_definition_version(
                db,
                domain="ml",
                term="RRF",
                definition="cross-domain",
                evidence_ids=[cross_domain],
                supersedes_version_id=current["definition_version_id"],
            )

    db.upsert_glossary_term("ml", "BM25", "other term")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-bound", mapping={"BM25": [unbound]}
    )
    with pytest.raises(sqlite3.IntegrityError, match="not currently bound"):
        with db._conn:
            _raw_insert_definition_version(
                db,
                domain="ml",
                term="RRF",
                definition="cross-term",
                evidence_ids=[unbound],
                supersedes_version_id=current["definition_version_id"],
            )

    db._conn.execute(
        """UPDATE canonical_evidence SET status='stale', invalid_reason='changed'
           WHERE evidence_id=?""",
        (bound,),
    )
    db._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="not currently bound"):
        with db._conn:
            _raw_insert_definition_version(
                db,
                domain="ml",
                term="RRF",
                definition="stale",
                evidence_ids=[bound],
                supersedes_version_id=current["definition_version_id"],
            )

    empty_version = _raw_insert_definition_version(
        db,
        domain="ml",
        term="RRF",
        definition="manual",
        evidence_ids=[],
        supersedes_version_id=current["definition_version_id"],
    )
    db._conn.execute(
        """UPDATE glossary SET definition='manual', current_definition_version_id=?
           WHERE domain='ml' AND term='RRF'""",
        (empty_version,),
    )
    db._conn.commit()
    assert db.current_concept_definition("ml", "RRF")["source_evidence_ids"] == []


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("gap", "chain must be contiguous"),
        ("missing_predecessor", "requires predecessor"),
        ("foreign_predecessor", "previous identity version"),
    ],
)
def test_definition_insert_trigger_rejects_broken_version_chain(
    db: Database,
    case: str,
    message: str,
) -> None:
    db.upsert_glossary_term("ml", "RRF", "rrf-v1")
    db.upsert_glossary_term("ml", "BM25", "bm25-v1")
    current = db.current_concept_definition("ml", "RRF")
    foreign = db.current_concept_definition("ml", "BM25")
    assert current is not None and foreign is not None
    forced_version = 3 if case == "gap" else 2
    predecessor = {
        "gap": current["definition_version_id"],
        "missing_predecessor": None,
        "foreign_predecessor": foreign["definition_version_id"],
    }[case]

    with pytest.raises(sqlite3.IntegrityError, match=message):
        with db._conn:
            _raw_insert_definition_version(
                db,
                domain="ml",
                term="RRF",
                definition=f"broken-{case}",
                evidence_ids=[],
                supersedes_version_id=predecessor,
                forced_version=forced_version,
            )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("gap", "version chain 不连续"),
        ("missing_predecessor", "supersedes 非法"),
        ("foreign_predecessor", "supersedes 非法"),
    ],
)
def test_definition_validator_rejects_broken_version_chain_without_trigger(
    db: Database,
    case: str,
    message: str,
) -> None:
    db.upsert_glossary_term("ml", "RRF", "rrf-v1")
    db.upsert_glossary_term("ml", "BM25", "bm25-v1")
    current = db.current_concept_definition("ml", "RRF")
    foreign = db.current_concept_definition("ml", "BM25")
    assert current is not None and foreign is not None
    trigger_sql = db._conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type='trigger'
             AND name='trg_concept_definition_versions_chain_insert'"""
    ).fetchone()[0]
    db._conn.execute("DROP TRIGGER trg_concept_definition_versions_chain_insert")
    forced_version = 3 if case == "gap" else 2
    predecessor = {
        "gap": current["definition_version_id"],
        "missing_predecessor": None,
        "foreign_predecessor": foreign["definition_version_id"],
    }[case]
    version_id = _raw_insert_definition_version(
        db,
        domain="ml",
        term="RRF",
        definition=f"broken-{case}",
        evidence_ids=[],
        supersedes_version_id=predecessor,
        forced_version=forced_version,
    )
    db._conn.execute(
        """UPDATE glossary
           SET definition=?, current_definition_version_id=?
           WHERE domain='ml' AND term='RRF'""",
        (f"broken-{case}", version_id),
    )
    db._conn.execute(trigger_sql)
    db._conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match=message):
        migration_current.validate(db._conn)


def test_domain_rename_and_merge_append_identity_transfer_versions(db: Database) -> None:
    _insert_job(db, "job-src")
    _insert_job(db, "job-dst")
    src_evidence = _insert_evidence(db, "job-src", "segment:src")
    dst_evidence = _insert_evidence(db, "job-dst", "segment:dst")
    db.upsert_glossary_term("ml", "RRF alias", "source definition")
    db.upsert_glossary_term("ml", "RRF", "destination")
    db.replace_job_concept_occurrences(
        domain="ml",
        job_id="job-src",
        mapping={"RRF alias": [src_evidence]},
    )
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-dst", mapping={"RRF": [dst_evidence]}
    )
    src_before = db.current_concept_definition("ml", "RRF alias")
    dst_before = db.current_concept_definition("ml", "RRF")
    assert src_before and dst_before
    dst_lock_revision = db.get_glossary_term("ml", "RRF")["lock_revision"]

    merged = db.merge_glossary_terms("ml", "RRF alias", "RRF")
    assert merged["current_definition_version_id"] not in {
        src_before["definition_version_id"],
        dst_before["definition_version_id"],
    }
    assert db.current_concept_definition("ml", "RRF")["strategy"] == "concept_merge"
    merged_history = db.list_concept_definition_versions("ml", "RRF")
    assert sorted(item["version"] for item in merged_history) == list(
        range(1, len(merged_history) + 1)
    )
    assert merged["lock_revision"] == dst_lock_revision + 1
    assert {
        item["evidence_id"] for item in db.list_concept_occurrences("ml", "RRF")
    } == {src_evidence, dst_evidence}
    assert db.get_glossary_term("ml", "RRF alias") is None
    assert db.list_concept_definition_versions("ml", "RRF alias")

    pre_rename_revision = merged["lock_revision"]
    result = db.rename_domain("ml", "retrieval")
    assert result["concept_definition_versions"] == 1
    renamed = db.current_concept_definition("retrieval", "RRF")
    assert renamed is not None and renamed["strategy"] == "domain_rename"
    assert renamed["version"] == 1
    assert renamed["supersedes_version_id"] == merged[
        "current_definition_version_id"
    ]
    assert (
        db.get_glossary_term("retrieval", "RRF")["lock_revision"]
        == pre_rename_revision + 1
    )
    assert db.list_concept_definition_versions("ml", "RRF")
    assert {
        item["evidence_id"]
        for item in db.list_concept_occurrences("retrieval", "RRF")
    } == {src_evidence, dst_evidence}
    migration_current.validate(db._conn)


def test_job_and_concept_delete_remove_occurrences_but_preserve_history(
    db: Database,
) -> None:
    _insert_job(db, "job-delete")
    evidence_id = _insert_evidence(db, "job-delete", "segment:1")
    db.upsert_glossary_term("ml", "RRF", "definition")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-delete", mapping={"RRF": [evidence_id]}
    )
    initial = db.current_concept_definition("ml", "RRF")
    assert initial is not None
    sourced = db.append_concept_definition_version(
        domain="ml",
        term="RRF",
        definition="evidence-backed",
        evidence_ids=[evidence_id],
        strategy="test",
        actor="test:history",
        expected_current_version_id=initial["definition_version_id"],
        expected_lock_revision=0,
    )
    assert sourced["source_evidence_ids"] == [evidence_id]
    db._conn.execute(
        """UPDATE canonical_evidence SET status='stale', invalid_reason='changed'
           WHERE evidence_id=?""",
        (evidence_id,),
    )
    db._conn.commit()
    migration_current.validate(db._conn)
    db.delete_job_cascade("job-delete")
    assert db.list_concept_occurrences("ml", "RRF", include_invalid=True) == []
    assert db.list_concept_definition_versions("ml", "RRF")
    migration_current.validate(db._conn)

    db.rename_domain("ml", "retrieval")
    renamed = db.current_concept_definition("retrieval", "RRF")
    assert renamed is not None and renamed["source_evidence_ids"] == [evidence_id]
    migration_current.validate(db._conn)

    db.delete_glossary_term("retrieval", "RRF")
    assert db.get_glossary_term("retrieval", "RRF") is None
    assert db.list_concept_definition_versions("ml", "RRF")
    assert db.list_concept_definition_versions("retrieval", "RRF")
    migration_current.validate(db._conn)


def test_invalid_reconciliation_rolls_back_existing_mapping(db: Database) -> None:
    _insert_job(db, "job-rollback")
    evidence_id = _insert_evidence(db, "job-rollback", "segment:1")
    db.upsert_glossary_term("ml", "RRF", "definition")
    db.replace_job_concept_occurrences(
        domain="ml", job_id="job-rollback", mapping={"RRF": [evidence_id]}
    )
    with pytest.raises(ConceptNotFoundError):
        db.replace_job_concept_occurrences(
            domain="ml",
            job_id="job-rollback",
            mapping={"missing": [evidence_id]},
        )
    assert [
        item["evidence_id"] for item in db.list_concept_occurrences("ml", "RRF")
    ] == [evidence_id]

    db.upsert_glossary_term("ml", "rejected", "do not attach", status="rejected")
    with pytest.raises(ConceptConflictError, match="rejected"):
        db.replace_job_concept_occurrences(
            domain="ml",
            job_id="job-rollback",
            mapping={"rejected": [evidence_id]},
        )
    assert [
        item["evidence_id"] for item in db.list_concept_occurrences("ml", "RRF")
    ] == [evidence_id]
