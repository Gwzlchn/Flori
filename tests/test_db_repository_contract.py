"""冻结数据库拆分前后的公开面和 schema 行为。"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

import shared.db as db_module
from shared.db import Database, SCHEMA_VERSION
from shared.repositories.jobs import JobsReadRepository
from shared.repositories.maintenance import MaintenanceRepository


_PUBLIC_CONTRACT_SHA256 = (
    "42574e687a271d0a636ca1c787b0f83db47ddbf1bbf178200be1a035e792d40b"
)
# 按 schema 版本冻结指纹: 已登记版本上的任何 schema 漂移都会失败.
# 新增迁移时在这里补一行新版本的摘要, 补这一行就是"我确认 schema 变了"的显式动作.
_SCHEMA_CONTRACT_SHA256_BY_VERSION = {
    8: "906a33e4b9119fe384b1342c67ceb139de1f3b83f8a308abfca3120f77c81c2c",
    9: "974a89066d89d9fe12be2c9ad17111b357501699e2fd68bf45a0953cd22743c6",
}


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def test_database_public_exports_and_signatures_are_frozen():
    methods = {
        name: str(inspect.signature(value))
        for name, value in inspect.getmembers(Database, inspect.isfunction)
        if not name.startswith("_")
    }
    exports = sorted(name for name in vars(db_module) if not name.startswith("_"))
    assert _digest({"exports": exports, "methods": methods}) == (
        _PUBLIC_CONTRACT_SHA256
    )


def test_database_schema_foreign_keys_indexes_triggers_and_fts_are_frozen(db):
    schema = [
        tuple(row)
        for row in db._conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    foreign_keys = {
        name: [
            tuple(row)
            for row in db._conn.execute(f"PRAGMA foreign_key_list({name})")
        ]
        for (name,) in db._conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    }
    actual = _digest({"schema": schema, "fks": foreign_keys})
    expected = _SCHEMA_CONTRACT_SHA256_BY_VERSION.get(SCHEMA_VERSION)
    assert expected is not None, (
        f"schema v{SCHEMA_VERSION} 未登记指纹; 确认本次 schema 变更符合预期后, "
        f"把 {actual} 登记进 _SCHEMA_CONTRACT_SHA256_BY_VERSION"
    )
    assert actual == expected


def test_database_runtime_is_the_single_connection_and_lock_owner(db):
    assert db._conn is db._runtime.connection
    assert db._lock is db._runtime.lock


def test_runtime_rolls_back_failed_outer_write_but_preserves_nested_owner(db):
    db._conn.execute("CREATE TABLE runtime_rollback_probe(value TEXT NOT NULL)")
    db._conn.commit()

    def write_then_fail(_owner, connection: sqlite3.Connection, value: str):
        connection.execute(
            "INSERT INTO runtime_rollback_probe(value) VALUES (?)", (value,)
        )
        raise RuntimeError("fault after write")

    with pytest.raises(RuntimeError, match="fault after write"):
        db._runtime.run_transaction(
            db,
            write_then_fail,
            ("outer",),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )
    assert not db._conn.in_transaction
    assert db._conn.execute(
        "SELECT value FROM runtime_rollback_probe"
    ).fetchall() == []

    db._conn.execute("BEGIN IMMEDIATE")
    db._conn.execute(
        "INSERT INTO runtime_rollback_probe(value) VALUES ('owner')"
    )
    with pytest.raises(RuntimeError, match="fault after write"):
        db._runtime.run_transaction(
            db,
            write_then_fail,
            ("nested",),
            {},
            begin_immediate=False,
            commit_on_success=True,
            commit_if_false=True,
            rollback_on_error=False,
        )
    assert db._conn.in_transaction
    assert [row[0] for row in db._conn.execute(
        "SELECT value FROM runtime_rollback_probe ORDER BY rowid"
    )] == ["owner", "nested"]
    db._conn.rollback()


def test_jobs_repository_is_commit_free_and_facade_is_explicit():
    source = inspect.getsource(JobsReadRepository)
    assert ".commit(" not in source
    assert ".rollback(" not in source
    for name in (
        "get_job",
        "jobs_brief",
        "list_jobs",
        "lineage_versions",
        "lineage_counts",
        "count_jobs_by_status",
        "job_facets",
        "glossary_for_job",
    ):
        assert name in Database.__dict__


def test_all_domain_repositories_are_commit_free():
    classes = [JobsReadRepository, MaintenanceRepository]
    for module_name, class_name in (
        ("jobs", "JobsRepository"),
        ("workers", "WorkersRepository"),
        ("credentials", "CredentialsRepository"),
        ("telemetry", "TelemetryRepository"),
        ("prompts", "PromptsRepository"),
        ("collections", "CollectionsRepository"),
        ("concepts", "ConceptsRepository"),
        ("search", "SearchRepository"),
        ("study", "StudyRepository"),
    ):
        module = importlib.import_module(f"shared.repositories.{module_name}")
        classes.append(getattr(module, class_name))
    for repository in classes:
        source = inspect.getsource(repository)
        assert ".commit(" not in source
        assert ".rollback(" not in source
        assert "sqlite3.connect(" not in source


def test_cross_domain_transactions_have_one_explicit_owner():
    from shared.repositories.aggregates import DatabaseAggregates

    expected = {
        "activate_imported_job",
        "create_job",
        "promote_lineage_current",
        "update_job",
        "move_job_to_collection",
        "delete_job_cascade",
        "delete_collection",
        "rename_domain",
        "replace_concept_occurrences_for_job",
        "replace_job_concept_occurrences",
        "append_concept_definition_version",
        "merge_glossary_terms",
        "index_job_notes",
        "is_imported_job_activated",
        "create_study_suggestion_batch",
        "materialize_study_suggestions",
        "apply_study_suggestion_operations",
        "record_study_review",
        "upsert_concept_occurrence",
        "set_concept_definition_lock",
        "update_glossary_definition_cas",
        "upsert_glossary_term",
        "add_glossary_suggestion",
        "mark_study_suggestion_batch_queued",
        "fail_study_suggestion_batch",
        "retry_study_suggestion_batch",
        "create_study_card",
        "set_study_card_status",
        "delete_study_card",
    }
    aggregate_methods = {
        name
        for name, value in vars(DatabaseAggregates).items()
        if callable(value) and not name.startswith("_")
    }
    assert expected == aggregate_methods
    assert expected <= Database.__dict__.keys()


def test_database_facade_keeps_only_lifecycle_sql():
    source = inspect.getsource(Database)
    assert "self._conn.execute(" not in source
    assert "self._conn.commit(" not in source
    assert "self._conn.rollback(" not in source
    assert "__getattr__" not in source


def test_in_tx_methods_take_explicit_connection_and_owners_are_the_only_committers():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "shared/repositories").glob("*.py"):
        source = path.read_text()
        if path.name not in {"aggregates.py", "runtime.py"}:
            assert ".commit(" not in source
            assert ".rollback(" not in source
    for module_name in (
        "jobs",
        "workers",
        "credentials",
        "telemetry",
        "prompts",
        "collections",
        "concepts",
        "search",
        "study",
    ):
        module = importlib.import_module(f"shared.repositories.{module_name}")
        for repository in (
            value
            for value in vars(module).values()
            if inspect.isclass(value) and value.__name__.endswith("Repository")
        ):
            for name, method in vars(repository).items():
                if name.endswith("_in_tx"):
                    assert "connection" in inspect.signature(method).parameters


def test_maintenance_scripts_do_not_reach_into_connection():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/backfill_concept_edges.py",
        "scripts/backfill_zh_names.py",
        "scripts/merge_glossary_entities.py",
        "scripts/reencrypt-credentials.sh",
    ):
        assert "db._conn" not in (root / relative).read_text()
