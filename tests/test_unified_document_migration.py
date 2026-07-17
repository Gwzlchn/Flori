"""Document 内容族迁移的无损、优先级和故障回滚测试。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from shared.db import Database
from shared.migrations.runner import MigrationExecutionError, run_migrations


def _seed_v6(database: Database) -> None:
    connection = database._conn
    now = "2026-07-17T00:00:00+00:00"
    for job_id, content_type, pipeline in (
        ("job-paper", "paper", "paper"),
        ("job-article", "article", "article"),
        ("job-video", "video", "video"),
    ):
        connection.execute(
            """INSERT INTO jobs
               (id,content_type,pipeline,title,domain,status,created_at,updated_at)
               VALUES (?,?,?,?,?,'done',?,?)""",
            (job_id, content_type, pipeline, job_id, "ml", now, now),
        )
    connection.executemany(
        "INSERT INTO job_steps(job_id,step,status) VALUES (?,?,'done')",
        [
            ("job-paper", "05_smart_paper"),
            ("job-article", "04_smart_article"),
        ],
    )
    for job_id, content_type in (("job-paper", "paper"), ("job-article", "article")):
        connection.execute(
            """INSERT INTO notes_fts5
               (job_id,content_type,note_type,collection_id,domain,title,body)
               VALUES (?,?, 'smart','','ml',?,?)""",
            (job_id, content_type, job_id, "body"),
        )
        connection.execute(
            """INSERT INTO note_chunks
               (chunk_id,job_id,note_type,content_type,domain,title,section,
                chunk_index,body,created_at,updated_at)
               VALUES (?,?, 'smart',?,'ml',?,'s',0,'body',?,?)""",
            (f"chunk-{job_id}", job_id, content_type, job_id, now, now),
        )
        connection.execute(
            """INSERT INTO note_chunks_fts5
               (chunk_id,job_id,note_type,content_type,collection_id,domain,
                title,section,body,evidence_json)
               VALUES (?,?, 'smart',?,'','ml',?,'s','body','{}')""",
            (f"chunk-{job_id}", job_id, content_type, job_id),
        )
    connection.execute(
        """INSERT INTO glossary
           (domain,term,definition,occurrences,created_at,updated_at)
           VALUES ('ml','attention','',?, ?, ?)""",
        (
            json.dumps([
                {"job_id": "job-paper", "content_type": "paper", "location": "p1"},
                {"job_id": "job-article", "content_type": "article", "location": "p2"},
            ]),
            now,
            now,
        ),
    )
    for pipeline, step, content in (
        ("paper", "05_smart_paper", "paper prompt"),
        ("article", "04_smart_article", "article prompt"),
    ):
        connection.execute(
            """INSERT INTO prompt_overrides
               (scope,domain,pipeline,step,content,version,updated_at)
               VALUES ('global','',?,?,?,1,?)""",
            (pipeline, step, content, now),
        )
        connection.execute(
            """INSERT INTO prompt_override_versions
               (scope,domain,pipeline,step,version,content,note,created_at)
               VALUES ('global','',?,?,1,?,'seed',?)""",
            (pipeline, step, content, now),
        )
    connection.commit()


def _v6_database(tmp_path, name: str) -> Database:
    database = Database(tmp_path / name)
    run_migrations(database._conn, database._migration_steps(), target_version=6)
    _seed_v6(database)
    return database


def test_v7_migrates_jobs_steps_fts_occurrences_and_prompt_history(tmp_path):
    database = _v6_database(tmp_path, "document-v7.db")
    before = {
        table: database._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "jobs", "job_steps", "notes_fts5", "note_chunks",
            "note_chunks_fts5", "prompt_overrides", "prompt_override_versions",
        )
    }

    assert run_migrations(
        database._conn, database._migration_steps(), target_version=7
    ) == 7
    after = {
        table: database._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before
    rows = {
        row["id"]: (row["content_type"], row["pipeline"], row["document_kind"])
        for row in database._conn.execute(
            "SELECT id,content_type,pipeline,document_kind FROM jobs"
        )
    }
    assert rows["job-paper"] == ("document", "document", "research_paper")
    assert rows["job-article"] == ("document", "document", "article")
    assert rows["job-video"] == ("video", "video", "")
    assert {
        tuple(row) for row in database._conn.execute(
            "SELECT job_id,step FROM job_steps ORDER BY job_id"
        )
    } == {("job-paper", "05_smart"), ("job-article", "05_smart")}
    for table in ("notes_fts5", "note_chunks", "note_chunks_fts5"):
        assert database._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE content_type='document'"
        ).fetchone()[0] == 2
    occurrences = json.loads(database._conn.execute(
        "SELECT occurrences FROM glossary WHERE domain='ml' AND term='attention'"
    ).fetchone()[0])
    assert {item["content_type"] for item in occurrences} == {"document"}
    assert {item["document_kind"] for item in occurrences} == {"research_paper", "article"}
    assert database.get_prompt_override_version(
        "global", "", "document", "05_smart", 1, "research_paper",
    )["content"] == "paper prompt"
    assert database.get_prompt_override_version(
        "global", "", "document", "05_smart", 1, "article",
    )["content"] == "article prompt"
    database.close()


def test_document_prompt_resolution_precedence_is_kind_then_domain(tmp_path):
    database = Database(tmp_path / "prompt-precedence.db")
    database.init_schema()
    database.set_prompt_override("global", None, "document", "05_smart", "common")
    database.set_prompt_override("domain", "ml", "document", "05_smart", "domain common")
    database.set_prompt_override(
        "global", None, "document", "05_smart", "kind", document_kind="whitepaper",
    )
    database.set_prompt_override(
        "domain", "ml", "document", "05_smart", "domain kind",
        document_kind="whitepaper",
    )

    assert database.resolve_prompt_overrides("document", "ml", "whitepaper")["05_smart"]["content"] == "domain kind"
    assert database.resolve_prompt_overrides("document", "other", "whitepaper")["05_smart"]["content"] == "kind"
    assert database.resolve_prompt_overrides("document", "ml", "article")["05_smart"]["content"] == "domain common"
    database.close()


def test_v7_fault_rolls_back_schema_data_fts_and_prompt_tables(tmp_path):
    database = _v6_database(tmp_path, "document-v7-fault.db")
    before = database._conn.execute(
        "SELECT content_type,pipeline FROM jobs WHERE id='job-paper'"
    ).fetchone()

    def fail(version: int, _connection: sqlite3.Connection) -> None:
        if version == 7:
            raise RuntimeError("injected v7 failure")

    with pytest.raises(MigrationExecutionError, match="pending chain.*v6"):
        run_migrations(
            database._conn, database._migration_steps(), fault_injector=fail,
        )
    assert database.schema_version() == 6
    assert tuple(database._conn.execute(
        "SELECT content_type,pipeline FROM jobs WHERE id='job-paper'"
    ).fetchone()) == tuple(before)
    assert "document_kind" not in {
        row[1] for row in database._conn.execute("PRAGMA table_info(jobs)")
    }
    assert database._conn.execute(
        "SELECT content FROM prompt_overrides WHERE pipeline='paper'"
    ).fetchone()[0] == "paper prompt"
    assert database._conn.execute(
        "SELECT COUNT(*) FROM notes_fts5 WHERE content_type='paper'"
    ).fetchone()[0] == 1
    database.close()
