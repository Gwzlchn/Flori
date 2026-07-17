"""验证完整灾备快照的校验、回滚与空环境恢复边界."""

from __future__ import annotations

import base64
import importlib.util
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

from shared.db import Database
from shared.migrations import migration_steps, run_migrations


_MODULE_PATH = Path(__file__).parents[1] / "scripts" / "dr_snapshot.py"
_SCHEMA_MANIFEST_PATH = Path(__file__).parents[1] / "shared" / "migrations" / "manifest.json"
_MIGRATION_PACKAGE = _SCHEMA_MANIFEST_PATH.parent
_CURRENT_SCHEMA_VERSION = int(json.loads(
    _SCHEMA_MANIFEST_PATH.read_text(encoding="utf-8")
)["current_version"])
_MIGRATION_FIXTURES = Path(__file__).parent / "fixtures" / "migrations"
_DR_FIXTURES = Path(__file__).parent / "fixtures" / "dr"
_LEGACY_GLOSSARY_TABLE = "glossary_bak_clean_20260617"
_FTS_SHADOW_TABLES = (
    "notes_fts5_config",
    "notes_fts5_content",
    "notes_fts5_data",
    "notes_fts5_docsize",
    "notes_fts5_idx",
    "note_chunks_fts5_config",
    "note_chunks_fts5_content",
    "note_chunks_fts5_data",
    "note_chunks_fts5_docsize",
    "note_chunks_fts5_idx",
)
_SPEC = importlib.util.spec_from_file_location("flori_dr_snapshot", _MODULE_PATH)
assert _SPEC and _SPEC.loader
dr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dr)


def _fixture_roots(
    root: Path,
    *,
    user_version: int = 0,
    schema_manifest_path: Path | None = None,
    include_legacy_glossary_backup: bool = False,
) -> dict[str, Path]:
    roots = {name: root / name for name in ("data", "redis", "minio", "config")}
    for path in roots.values():
        path.mkdir(parents=True)
    db_path = roots["data"] / "db" / "analyzer.db"
    db_path.parent.mkdir(parents=True)
    local_manifest = json.loads(_SCHEMA_MANIFEST_PATH.read_text(encoding="utf-8"))
    local_current = int(local_manifest["current_version"])
    if user_version == 0:
        connection = sqlite3.connect(db_path)
        connection.executescript(
            (_MIGRATION_FIXTURES / "v0000_unversioned.sql").read_text(
                encoding="utf-8"
            )
        )
    else:
        database = Database(db_path)
        run_migrations(
            database._conn,
            database._migration_steps(),
            target_version=min(user_version, local_current),
        )
        connection = database._conn
    if user_version >= 7:
        connection.execute(
            "INSERT INTO jobs "
            "(id, content_type, document_kind, pipeline, title, domain, created_at, updated_at) "
            "VALUES ('jobs_test', 'document', 'article', 'document', '灾备测试', 'general', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
    else:
        connection.execute(
            "INSERT INTO jobs "
            "(id, content_type, pipeline, title, domain, created_at, updated_at) "
            "VALUES ('jobs_test', 'article', 'article', '灾备测试', 'general', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
    if user_version > local_current:
        migration_manifest = json.loads(
            (schema_manifest_path or _SCHEMA_MANIFEST_PATH).read_text(encoding="utf-8")
        )
        connection.executemany(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, 'fixture')",
            [
                (entry["version"], entry["name"], entry["checksum"])
                for entry in migration_manifest["migrations"][local_current:user_version]
            ],
        )
        connection.execute(f"PRAGMA user_version={user_version}")
    if include_legacy_glossary_backup:
        connection.executescript(
            (_MIGRATION_FIXTURES / "legacy_glossary_backup.sql").read_text(
                encoding="utf-8"
            )
        )
    connection.commit()
    connection.close()
    (roots["data"] / "jobs" / "jobs_test").mkdir(parents=True)
    (roots["data"] / "jobs" / "jobs_test" / "note.md").write_text("笔记\n", encoding="utf-8")
    (roots["data"] / "prompts" / "profiles").mkdir(parents=True)
    (roots["data"] / "prompts" / "profiles" / "general.yaml").write_text("role: test\n", encoding="utf-8")
    (roots["redis"] / "dump.rdb").write_bytes(b"REDIS-RDB")
    (roots["redis"] / "appendonlydir").mkdir()
    (roots["redis"] / "appendonlydir" / "appendonly.aof").write_bytes(b"AOF")
    (roots["minio"] / "flori" / "jobs_test").mkdir(parents=True)
    (roots["minio"] / "flori" / "jobs_test" / "artifact.bin").write_bytes(b"OBJECT")
    (roots["config"] / "pipelines.yaml").write_text("pipelines: {}\n", encoding="utf-8")
    return roots


def _create(
    root: Path,
    *,
    user_version: int = 0,
    redis_mode: str = "offline-volume",
    schema_manifest_path: Path | None = None,
    include_legacy_glossary_backup: bool = False,
) -> tuple[Path, dict]:
    sources = _fixture_roots(
        root / "source",
        user_version=user_version,
        schema_manifest_path=schema_manifest_path,
        include_legacy_glossary_backup=include_legacy_glossary_backup,
    )
    archive = root / "backups" / "snapshot.tar.gz"
    result = dr.create_snapshot(
        data_root=sources["data"],
        redis_root=sources["redis"],
        minio_root=sources["minio"],
        config_root=sources["config"],
        output=archive,
        generation="test-generation",
        app_version="test",
        redis_mode=redis_mode,
        schema_manifest_path=schema_manifest_path,
    )
    return archive, result


def _extended_schema_manifest(root: Path, current_version: int) -> Path:
    package = root / f"schema-v{current_version}"
    package.mkdir()
    for source in _MIGRATION_PACKAGE.iterdir():
        if source.is_file() and source.suffix in {".py", ".json"}:
            shutil.copy2(source, package / source.name)
    manifest = json.loads(_SCHEMA_MANIFEST_PATH.read_text(encoding="utf-8"))
    module_names = [
        migration.apply.__module__.rsplit(".", 1)[-1]
        for migration in migration_steps()
    ]
    assert len(module_names) == manifest["current_version"]
    for version in range(manifest["current_version"] + 1, current_version + 1):
        previous = module_names[-1]
        module_name = f"v{version:04d}_fixture"
        source = (
            '"""测试专用的前缀兼容迁移。"""\n\n'
            "from pathlib import Path\n"
            f"from . import {previous} as previous\n\n"
            f"VERSION = {version}\n"
            f"NAME = 'fixture-v{version}'\n\n"
            "def source_payload() -> str:\n"
            "    return Path(__file__).read_text(encoding='utf-8')\n\n"
            "def apply(connection) -> None:\n"
            "    return None\n\n"
            "def validate(connection) -> None:\n"
            "    previous.validate(connection)\n"
        )
        (package / f"{module_name}.py").write_text(source, encoding="utf-8")
        manifest["migrations"].append(
            {
                "version": version,
                "name": f"fixture-v{version}",
                "checksum": hashlib.sha256(source.encode()).hexdigest(),
            }
        )
        module_names.append(module_name)
    manifest["current_version"] = current_version
    imports = "\n".join(
        f"from . import {module} as migration_v{version}"
        for version, module in enumerate(module_names, start=1)
    )
    entries = ",\n".join(
        "        Migration("
        f"migration_v{version}.VERSION, migration_v{version}.NAME, "
        f"migration_v{version}.source_payload(), migration_v{version}.apply, "
        f"migration_v{version}.validate)"
        for version in range(1, len(module_names) + 1)
    )
    registry = (
        '"""测试专用 migration registry。"""\n\n'
        "from .runner import Migration\n"
        f"{imports}\n\n"
        "def migration_steps():\n"
        f"    return (\n{entries},\n    )\n"
    )
    (package / "registry.py").write_text(registry, encoding="utf-8")
    path = package / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def _rewrite_manifest(
    root: Path,
    archive: Path,
    mutate,
) -> Path:
    extracted = root / "rewrite"
    extracted.mkdir()
    dr._extract_archive(archive, extracted)
    manifest_path = extracted / dr.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    rewritten = root / "rewritten.tar.gz"
    dr._tar_stage(extracted, rewritten)
    digest = hashlib.sha256(rewritten.read_bytes()).hexdigest()
    rewritten.with_suffix(rewritten.suffix + ".sha256").write_text(
        f"{digest}  {rewritten.name}\n", encoding="utf-8"
    )
    return rewritten


def _rewrite_sqlite(
    root: Path,
    archive: Path,
    mutate,
    *,
    suffix: str = "corrupt",
) -> Path:
    extracted = root / f"rewrite-db-{suffix}"
    extracted.mkdir()
    dr._extract_archive(archive, extracted)
    manifest_path = extracted / dr.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    db_rel = manifest["sqlite"]["path"]
    db_path = extracted / db_rel
    connection = sqlite3.connect(db_path)
    mutate(connection)
    connection.commit()
    manifest["sqlite"]["schema_sha256"] = dr._sqlite_schema_hash(connection)
    connection.close()
    manifest["files"][db_rel]["size"] = db_path.stat().st_size
    manifest["files"][db_rel]["sha256"] = hashlib.sha256(
        db_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    rewritten = root / f"rewritten-db-{suffix}.tar.gz"
    dr._tar_stage(extracted, rewritten)
    digest = hashlib.sha256(rewritten.read_bytes()).hexdigest()
    rewritten.with_suffix(rewritten.suffix + ".sha256").write_text(
        f"{digest}  {rewritten.name}\n", encoding="utf-8"
    )
    return rewritten


def _rewrite_sqlite_ledger(root: Path, archive: Path) -> Path:
    def tamper(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,)
        )

    return _rewrite_sqlite(root, archive, tamper, suffix="ledger")


def _rebuild_table(
    connection: sqlite3.Connection,
    table: str,
    transform,
    *,
    extra_values: dict[str, str] | None = None,
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    assert row and row[0]
    original_sql = str(row[0])
    columns = [
        str(column[1])
        for column in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    ]
    indexes = [
        str(index[0])
        for index in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL ORDER BY name",
            (table,),
        ).fetchall()
    ]
    old = f"__corrupt_{table}"
    connection.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
    corrupted_sql = transform(original_sql)
    assert corrupted_sql != original_sql
    connection.execute(corrupted_sql)
    extras = extra_values or {}
    target_columns = [*columns, *extras]
    names = ", ".join(f'"{column}"' for column in target_columns)
    selected = ", ".join(
        [*(f'"{column}"' for column in columns), *extras.values()]
    )
    connection.execute(
        f'INSERT INTO "{table}" ({names}) SELECT {selected} FROM "{old}"'
    )
    connection.execute(f'DROP TABLE "{old}"')
    for index_sql in indexes:
        connection.execute(index_sql)


def _replace_ddl(value: str, old: str, new: str) -> str:
    assert old in value
    return value.replace(old, new, 1)


def _missing_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE workers")


def _wrong_column_type(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "app_credentials",
        lambda sql: _replace_ddl(sql, "key TEXT PRIMARY KEY", "key INTEGER PRIMARY KEY"),
    )


def _wrong_not_null(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "app_credentials",
        lambda sql: _replace_ddl(sql, "value TEXT", "value TEXT NOT NULL"),
    )


def _wrong_default(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "workers",
        lambda sql: _replace_ddl(
            sql,
            "status TEXT NOT NULL DEFAULT 'offline'",
            "status TEXT NOT NULL DEFAULT 'broken'",
        ),
    )


def _wrong_primary_key(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "app_credentials",
        lambda sql: _replace_ddl(sql, "key TEXT PRIMARY KEY", "key TEXT"),
    )


def _wrong_foreign_key(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "study_reviews",
        lambda sql: _replace_ddl(
            sql,
            "REFERENCES study_cards(card_id) ON DELETE CASCADE",
            "REFERENCES study_cards(card_id) ON DELETE RESTRICT",
        ),
    )


def _wrong_named_index(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX idx_jobs_status")
    connection.execute("CREATE INDEX idx_jobs_status ON jobs(title)")


def _ordinary_table_masquerades_as_fts5(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE notes_fts5")
    connection.execute(
        "CREATE TABLE notes_fts5("
        "job_id, content_type, note_type, collection_id, domain, title, body)"
    )


def _foreign_key_violation(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO study_reviews "
        "(card_id, due_at, interval_days, ease, repetitions, lapses, updated_at) "
        "VALUES ('missing-card', '2026-01-01', 1, 2.5, 1, 0, '2026-01-01')"
    )


def _empty_applied_at(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE schema_migrations SET applied_at=' ' WHERE version=1"
    )


def _append_table_constraint(sql: str, constraint: str) -> str:
    closing = sql.rfind(")")
    assert closing > 0
    return sql[:closing] + f", {constraint}" + sql[closing:]


def _rewrite_schema_sql(
    connection: sqlite3.Connection,
    name: str,
    transform,
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (name,)
    ).fetchone()
    assert row and row[0]
    original = str(row[0])
    rewritten = transform(original)
    assert rewritten != original
    schema_version = int(
        connection.execute("PRAGMA schema_version").fetchone()[0]
    )
    connection.execute("PRAGMA writable_schema=ON")
    try:
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE name=?",
            (rewritten, name),
        )
    finally:
        connection.execute("PRAGMA writable_schema=OFF")
    connection.execute(f"PRAGMA schema_version={schema_version + 1}")


def _add_shadow_column(sql: str, table: str) -> str:
    if table.endswith("_idx"):
        marker = ", PRIMARY KEY"
        assert marker in sql
        return sql.replace(marker, ", poison TEXT, PRIMARY KEY", 1)
    return _append_table_constraint(sql, "poison TEXT")


def _shadow_extra_column(table: str):
    def mutate(connection: sqlite3.Connection) -> None:
        _rewrite_schema_sql(
            connection,
            table,
            lambda sql: _add_shadow_column(sql, table),
        )

    return mutate


def _shadow_blocking_check(table: str):
    def mutate(connection: sqlite3.Connection) -> None:
        _rewrite_schema_sql(
            connection,
            table,
            lambda sql: _append_table_constraint(
                sql,
                "CHECK(c0 <> 'blocked')",
            ),
        )

    return mutate


def _unexpected_poison_column(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "jobs",
        lambda sql: _append_table_constraint(sql, "poison TEXT NOT NULL"),
        extra_values={"poison": "'seed'"},
    )


def _unexpected_check(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "jobs",
        lambda sql: _append_table_constraint(
            sql, "CHECK(title IS NULL OR length(title) > 0)"
        ),
    )


def _unexpected_unique(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "jobs",
        lambda sql: _append_table_constraint(sql, "UNIQUE(status)"),
    )


def _unexpected_partial_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX poison_jobs_partial ON jobs(status) WHERE status='pending'"
    )


def _unexpected_trigger(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TRIGGER poison_jobs BEFORE INSERT ON jobs "
        "BEGIN SELECT RAISE(ABORT, 'poison'); END"
    )


def _unexpected_view(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE VIEW poison_jobs_view AS SELECT id FROM jobs")


def _unexpected_strict_flag(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "app_credentials",
        lambda sql: sql + " STRICT",
    )


def _unexpected_without_rowid(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "app_credentials",
        lambda sql: sql + " WITHOUT ROWID",
    )


def _wrong_default_literal_case(connection: sqlite3.Connection) -> None:
    _rewrite_schema_sql(
        connection,
        "workers",
        lambda sql: _replace_ddl(
            sql,
            "DEFAULT 'offline'",
            "DEFAULT 'OFFLINE'",
        ),
    )


def _sqlite_sequence_extra_column(connection: sqlite3.Connection) -> None:
    _rewrite_schema_sql(
        connection,
        "sqlite_sequence",
        lambda sql: _append_table_constraint(sql, "poison TEXT"),
    )


def _sqlite_sequence_blocking_check(connection: sqlite3.Connection) -> None:
    _rewrite_schema_sql(
        connection,
        "sqlite_sequence",
        lambda sql: _append_table_constraint(sql, "CHECK(name <> 'Poison')"),
    )


def _unexpected_sqlite_poison(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE poison(value TEXT)")
    schema_version = int(
        connection.execute("PRAGMA schema_version").fetchone()[0]
    )
    connection.execute("PRAGMA writable_schema=ON")
    try:
        connection.execute(
            "UPDATE sqlite_master SET name='sqlite_poison', "
            "tbl_name='sqlite_poison', sql='CREATE TABLE sqlite_poison(value TEXT)' "
            "WHERE type='table' AND name='poison'"
        )
    finally:
        connection.execute("PRAGMA writable_schema=OFF")
    connection.execute(f"PRAGMA schema_version={schema_version + 1}")


def _legacy_preserve_wrong_shape(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE glossary_bak_clean_20260617(domain TEXT)"
    )


def _sqlite_stat1_extra_column(connection: sqlite3.Connection) -> None:
    connection.execute("ANALYZE")
    _rewrite_schema_sql(
        connection,
        "sqlite_stat1",
        lambda sql: _append_table_constraint(sql, "poison TEXT"),
    )


def _missing_ledger_check(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "schema_migrations",
        lambda sql: _replace_ddl(
            sql, "CHECK(version > 0)", "CHECK(version >= 0)"
        ),
    )


def _wrong_ledger_column_type(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "schema_migrations",
        lambda sql: _replace_ddl(
            sql, "checksum TEXT NOT NULL", "checksum BLOB NOT NULL"
        ),
    )


def _wrong_ledger_primary_key(connection: sqlite3.Connection) -> None:
    _rebuild_table(
        connection,
        "schema_migrations",
        lambda sql: _replace_ddl(
            sql,
            "version INTEGER PRIMARY KEY CHECK(version > 0)",
            "version INTEGER UNIQUE CHECK(version > 0)",
        ),
    )


_INVALID_APPLICATION_SCHEMAS = (
    ("missing-table", _missing_table),
    ("wrong-type", _wrong_column_type),
    ("wrong-not-null", _wrong_not_null),
    ("wrong-default", _wrong_default),
    ("wrong-primary-key", _wrong_primary_key),
    ("wrong-foreign-key", _wrong_foreign_key),
    ("wrong-named-index", _wrong_named_index),
    ("ordinary-table-as-fts5", _ordinary_table_masquerades_as_fts5),
    ("foreign-key-violation", _foreign_key_violation),
    ("wrong-ledger-column-type", _wrong_ledger_column_type),
    ("wrong-ledger-primary-key", _wrong_ledger_primary_key),
    ("missing-ledger-check", _missing_ledger_check),
    ("empty-applied-at", _empty_applied_at),
    ("unexpected-poison-column", _unexpected_poison_column),
    ("unexpected-check", _unexpected_check),
    ("unexpected-unique", _unexpected_unique),
    ("unexpected-partial-index", _unexpected_partial_index),
    ("unexpected-trigger", _unexpected_trigger),
    ("unexpected-view", _unexpected_view),
    ("unexpected-strict", _unexpected_strict_flag),
    ("unexpected-without-rowid", _unexpected_without_rowid),
    ("wrong-default-literal-case", _wrong_default_literal_case),
    ("sqlite-sequence-extra-column", _sqlite_sequence_extra_column),
    ("sqlite-sequence-blocking-check", _sqlite_sequence_blocking_check),
    ("unexpected-sqlite-poison", _unexpected_sqlite_poison),
    ("legacy-preserve-wrong-shape", _legacy_preserve_wrong_shape),
    ("sqlite-stat1-extra-column", _sqlite_stat1_extra_column),
    *tuple(
        (f"{table}-extra-column", _shadow_extra_column(table))
        for table in _FTS_SHADOW_TABLES
    ),
    *tuple(
        (f"{table}-blocking-check", _shadow_blocking_check(table))
        for table in ("notes_fts5_content", "note_chunks_fts5_content")
    ),
)


def _target_roots(root: Path) -> dict[str, Path]:
    targets = {name: root / name for name in ("data", "redis", "minio", "config")}
    for path in targets.values():
        path.mkdir(parents=True)
    return targets


def _inventory(root: Path) -> list[tuple[str, bytes]]:
    return sorted(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )


def _tree_state(root: Path) -> list[tuple[str, str, bytes]]:
    state: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            state.append((rel, "symlink", str(path.readlink()).encode()))
        elif path.is_dir():
            state.append((rel, "directory", b""))
        elif path.is_file():
            state.append((rel, "file", path.read_bytes()))
    return state


def _transaction_marker_fixture(
    target: Path,
    *,
    generation: str = "marker-generation",
    asset: str = "data",
) -> dict:
    target.mkdir(parents=True, exist_ok=True)
    base_name = f"{dr.STAGE_PREFIX}{generation}"
    base = target / base_name
    (base / "old").mkdir(parents=True)
    (base / "new").mkdir()
    (base / "guard.bin").write_bytes(b"BASE-GUARD")
    (target / "current.bin").write_bytes(b"CURRENT")
    marker = {
        "format": dr.FORMAT_NAME,
        "generation": generation,
        "asset": asset,
        "base": base_name,
        "status": "switching",
        "old_names": [],
        "new_names": [],
        "preserve_names": [],
        "moved_old": [],
        "moved_new": [],
    }
    (target / dr.TRANSACTION_FILE).write_text(
        json.dumps(marker, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return marker


def _replace_transaction_marker(target: Path, marker: dict) -> None:
    (target / dr.TRANSACTION_FILE).write_text(
        json.dumps(marker, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_snapshot_covers_all_persistent_assets_and_restores_empty_environment(tmp_path: Path):
    archive, backup = _create(tmp_path)

    manifest = dr.validate_archive(archive)
    assert backup["status"] == "success"
    assert backup["archive_sha256"]
    assert archive.with_suffix(archive.suffix + ".sha256").is_file()
    assert manifest["sqlite"]["integrity_check"] == "ok"
    assert manifest["assets"]["data"]["included"] is True
    assert manifest["assets"]["redis"]["included"] is True
    assert manifest["assets"]["minio"]["included"] is True
    assert manifest["assets"]["config"]["included"] is True
    declared = set(manifest["files"])
    assert "assets/data/jobs/jobs_test/note.md" in declared
    assert "assets/data/prompts/profiles/general.yaml" in declared
    assert "assets/data/db/analyzer.db" in declared
    assert "assets/redis/dump.rdb" in declared
    assert "assets/redis/appendonlydir/appendonly.aof" in declared
    assert "assets/minio/flori/jobs_test/artifact.bin" in declared
    assert "assets/config/pipelines.yaml" in declared

    targets = _target_roots(tmp_path / "empty-target")
    result = dr.restore_snapshot(archive_path=archive, targets=targets)

    assert result["status"] == "success"
    assert result["restored_assets"] == ["data", "redis", "minio", "config"]
    assert result["checks"]["atomic_switch"] == "ok"
    connection = sqlite3.connect(targets["data"] / "db" / "analyzer.db")
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("SELECT title FROM jobs WHERE id='jobs_test'").fetchone() == ("灾备测试",)
    connection.close()
    assert (targets["data"] / "jobs" / "jobs_test" / "note.md").read_text(encoding="utf-8") == "笔记\n"
    assert (targets["data"] / "prompts" / "profiles" / "general.yaml").is_file()
    assert (targets["redis"] / "dump.rdb").read_bytes() == b"REDIS-RDB"
    assert (targets["minio"] / "flori" / "jobs_test" / "artifact.bin").read_bytes() == b"OBJECT"
    assert (targets["config"] / "pipelines.yaml").is_file()
    assert not list((tmp_path / "empty-target").rglob(".flori-dr-*"))


def test_running_redis_mode_archives_materialized_rdb_and_aof(tmp_path: Path):
    archive, _ = _create(tmp_path, redis_mode="materialized-rdb-aof")

    manifest = dr.validate_archive(archive)

    assert manifest["assets"]["redis"]["capture_mode"] == "materialized-rdb-aof"
    redis_files = sorted(path for path in manifest["files"] if path.startswith("assets/redis/"))
    assert redis_files == [
        "assets/redis/appendonlydir/appendonly.aof",
        "assets/redis/dump.rdb",
    ]


def test_cli_accepts_materialized_redis_mode():
    args = dr._parser().parse_args([
        "create",
        "--data", "/data",
        "--redis", "/redis",
        "--output", "/output/backup.tar.gz",
        "--generation", "test-generation",
        "--redis-mode", "materialized-rdb-aof",
    ])

    assert args.redis_mode == "materialized-rdb-aof"


def test_nested_minio_mount_is_not_duplicated_or_removed_by_data_switch(tmp_path: Path):
    sources = _fixture_roots(tmp_path / "source")
    nested_minio = sources["data"] / "minio"
    (nested_minio / "flori" / "nested").mkdir(parents=True)
    (nested_minio / "flori" / "nested" / "object.bin").write_bytes(b"NESTED")
    archive = tmp_path / "backups" / "nested.tar.gz"
    dr.create_snapshot(
        data_root=sources["data"],
        redis_root=sources["redis"],
        minio_root=nested_minio,
        config_root=sources["config"],
        output=archive,
        generation="nested-generation",
    )
    manifest = dr.validate_archive(archive)
    assert manifest["assets"]["data"]["excluded_external_subtrees"] == ["minio"]
    assert not [path for path in manifest["files"] if path.startswith("assets/data/minio/")]

    data_target = tmp_path / "target" / "data"
    targets = {
        "data": data_target,
        "redis": tmp_path / "target" / "redis",
        "minio": data_target / "minio",
        "config": tmp_path / "target" / "config",
    }
    for path in targets.values():
        path.mkdir(parents=True, exist_ok=True)
    (targets["minio"] / "old-object").write_bytes(b"OLD")

    result = dr.restore_snapshot(archive_path=archive, targets=targets)

    assert result["preserved_target_entries"] == {"data": ["minio"]}
    assert (targets["data"] / "db" / "analyzer.db").is_file()
    assert not (targets["minio"] / "old-object").exists()
    assert (targets["minio"] / "flori" / "nested" / "object.bin").read_bytes() == b"NESTED"


def test_corrupt_archive_is_fail_closed_before_target_change(tmp_path: Path):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    sentinel = targets["data"] / "sentinel"
    sentinel.write_bytes(b"current")
    corrupt = tmp_path / "corrupt.tar.gz"
    raw = archive.read_bytes()
    corrupt.write_bytes(raw[: len(raw) // 2])

    with pytest.raises(dr.SnapshotError):
        dr.restore_snapshot(archive_path=corrupt, targets=targets)

    assert sentinel.read_bytes() == b"current"
    assert _inventory(targets["redis"]) == []
    assert not list((tmp_path / "target").rglob(".flori-dr-*"))


def test_mid_commit_failure_rolls_back_each_prepared_target_once_in_reverse(
    tmp_path: Path,
    monkeypatch,
):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    before = {name: _inventory(path) for name, path in targets.items()}
    original_replace = dr.os.replace
    original_rollback = dr._rollback_target
    rollback_calls: list[str | None] = []
    redis_old = (
        targets["redis"]
        / dr._stage_base_name("test-generation")
        / "old"
    )
    inject_failure = True

    def fail_inside_redis_commit(source, destination):
        nonlocal inject_failure
        if inject_failure and Path(destination).parent == redis_old:
            inject_failure = False
            raise OSError("injected mid-commit replace failure")
        return original_replace(source, destination)

    def record_rollback(
        target: Path,
        *,
        expected_asset: str | None = None,
        require_marker: bool = False,
    ):
        rollback_calls.append(expected_asset)
        return original_rollback(
            target,
            expected_asset=expected_asset,
            require_marker=require_marker,
        )

    monkeypatch.setattr(dr.os, "replace", fail_inside_redis_commit)
    monkeypatch.setattr(dr, "_rollback_target", record_rollback)

    with pytest.raises(OSError, match="mid-commit"):
        dr.restore_snapshot(archive_path=archive, targets=targets)

    assert rollback_calls == ["config", "minio", "redis", "data"]
    assert {name: _inventory(path) for name, path in targets.items()} == before
    for path in targets.values():
        assert not (path / dr.TRANSACTION_FILE).exists()
        assert not [child for child in path.iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


def test_partial_switch_marker_loss_blocks_current_and_restart_cleanup(
    tmp_path: Path,
    monkeypatch,
):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    original_persist = dr._persist_marker
    original_rollback = dr._rollback_target
    rollback_calls: list[str | None] = []
    marker_lost = False

    def lose_marker_after_moving_old(
        target: Path,
        marker: dict,
        *,
        expected_asset: str | None = None,
    ):
        nonlocal marker_lost
        if (
            expected_asset == "data"
            and marker.get("status") == "switching"
            and marker.get("moved_old")
            and not marker_lost
        ):
            marker_lost = True
            (target / dr.TRANSACTION_FILE).unlink()
            raise dr.SnapshotError("injected partial-switch marker loss")
        return original_persist(
            target,
            marker,
            expected_asset=expected_asset,
        )

    def record_rollback(
        target: Path,
        *,
        expected_asset: str | None = None,
        require_marker: bool = False,
    ):
        rollback_calls.append(expected_asset)
        return original_rollback(
            target,
            expected_asset=expected_asset,
            require_marker=require_marker,
        )

    monkeypatch.setattr(dr, "_persist_marker", lose_marker_after_moving_old)
    monkeypatch.setattr(dr, "_rollback_target", record_rollback)

    with pytest.raises(dr.SnapshotError, match="决策无法安全判定") as raised:
        dr.restore_snapshot(archive_path=archive, targets=targets)

    assert isinstance(raised.value.__cause__, dr.SnapshotError)
    assert rollback_calls == []
    assert marker_lost
    assert not (targets["data"] / dr.TRANSACTION_FILE).exists()
    stage = targets["data"] / dr._stage_base_name("test-generation")
    assert (stage / "old" / "current.txt").read_text(encoding="utf-8") == "data"
    assert not (targets["data"] / "current.txt").exists()
    for name in ("redis", "minio", "config"):
        marker = dr._load_marker(targets[name], expected_asset=name)
        assert marker is not None and marker["status"] == "prepared"
    before_restart = {name: _tree_state(path) for name, path in targets.items()}

    with pytest.raises(dr.SnapshotError, match="孤立 stage"):
        dr._recover_target_set(targets)

    assert {name: _tree_state(path) for name, path in targets.items()} == before_restart


def test_multiple_rollback_failures_still_attempt_all_and_recover_next_time(
    tmp_path: Path,
    monkeypatch,
):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    before = {name: _inventory(path) for name, path in targets.items()}
    original_rollback = dr._rollback_target
    rollback_calls: list[str | None] = []
    failed_assets = {"config", "redis"}

    def fail_selected_rollbacks(
        target: Path,
        *,
        expected_asset: str | None = None,
        require_marker: bool = False,
    ):
        rollback_calls.append(expected_asset)
        if expected_asset in failed_assets:
            raise dr.SnapshotError(f"injected {expected_asset} rollback failure")
        return original_rollback(
            target,
            expected_asset=expected_asset,
            require_marker=require_marker,
        )

    monkeypatch.setattr(dr, "_rollback_target", fail_selected_rollbacks)

    with pytest.raises(dr.SnapshotError, match="回滚未完成") as raised:
        dr.restore_snapshot(
            archive_path=archive,
            targets=targets,
            fail_after_commits=1,
        )

    assert rollback_calls == ["config", "minio", "redis", "data"]
    assert "config(SnapshotError)" in str(raised.value)
    assert "redis(SnapshotError)" in str(raised.value)
    assert isinstance(raised.value.__cause__, dr.SnapshotError)
    assert "故障注入" in str(raised.value.__cause__)
    for name in failed_assets:
        assert (targets[name] / dr.TRANSACTION_FILE).is_file()
    for name in ("data", "minio"):
        assert _inventory(targets[name]) == before[name]
        assert not (targets[name] / dr.TRANSACTION_FILE).exists()

    failed_assets.clear()
    dr._recover_target_set(targets)

    assert {name: _inventory(path) for name, path in targets.items()} == before
    for path in targets.values():
        assert not (path / dr.TRANSACTION_FILE).exists()
        assert not [child for child in path.iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


@pytest.mark.parametrize(
    "case",
    [
        "primary-keyboard-cleanup-ordinary",
        "primary-systemexit-cleanup-keyboard",
        "primary-ordinary-cleanup-systemexit",
    ],
)
def test_restore_control_flow_errors_are_not_swallowed_and_cleanup_continues(
    tmp_path: Path,
    monkeypatch,
    case: str,
):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    before = {name: _inventory(path) for name, path in targets.items()}
    if case == "primary-keyboard-cleanup-ordinary":
        primary: BaseException = KeyboardInterrupt("primary keyboard")
        cleanup: BaseException = dr.SnapshotError("cleanup ordinary")
        expected = primary
    elif case == "primary-systemexit-cleanup-keyboard":
        primary = SystemExit(23)
        cleanup = KeyboardInterrupt("cleanup keyboard")
        expected = primary
    else:
        primary = dr.SnapshotError("primary ordinary")
        cleanup = SystemExit(29)
        expected = cleanup
    original_rollback = dr._rollback_target
    rollback_calls: list[str | None] = []
    cleanup_enabled = True

    def fail_commit(*_args, **_kwargs):
        raise primary

    def cleanup_with_control_flow(
        target: Path,
        *,
        expected_asset: str | None = None,
        require_marker: bool = False,
    ):
        rollback_calls.append(expected_asset)
        if cleanup_enabled and expected_asset == "config":
            raise cleanup
        return original_rollback(
            target,
            expected_asset=expected_asset,
            require_marker=require_marker,
        )

    monkeypatch.setattr(dr, "_commit_target", fail_commit)
    monkeypatch.setattr(dr, "_rollback_target", cleanup_with_control_flow)

    with pytest.raises(type(expected)) as raised:
        dr.restore_snapshot(archive_path=archive, targets=targets)

    assert raised.value is expected
    assert rollback_calls == ["config", "minio", "redis", "data"]
    assert any(
        "config" in note for note in getattr(raised.value, "__notes__", [])
    )
    cleanup_enabled = False
    dr._recover_target_set(targets)
    assert {name: _inventory(path) for name, path in targets.items()} == before


def test_second_accept_failure_rolls_forward_all_assets_and_returns_success(
    tmp_path: Path,
    monkeypatch,
):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    expected = {
        name: _inventory(tmp_path / "source" / name)
        for name in ("redis", "minio", "config")
    }
    original_accept = dr._accept_target
    accept_calls: list[str | None] = []

    def fail_second_accept(target: Path, *, expected_asset: str | None = None):
        accept_calls.append(expected_asset)
        if len(accept_calls) == 2:
            raise dr.SnapshotError("injected second accept failure")
        return original_accept(target, expected_asset=expected_asset)

    monkeypatch.setattr(dr, "_accept_target", fail_second_accept)

    result = dr.restore_snapshot(archive_path=archive, targets=targets)

    assert accept_calls == ["data", "redis", "redis", "minio", "config"]
    assert result["status"] == "success"
    assert result["commit_recovered_after_error"] is True
    assert result["error_type"] == "SnapshotError"
    assert {
        name: _inventory(targets[name]) for name in expected
    } == expected
    connection = sqlite3.connect(targets["data"] / "db" / "analyzer.db")
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT title FROM jobs WHERE id='jobs_test'"
        ).fetchone() == ("灾备测试",)
    finally:
        connection.close()
    assert not (targets["data"] / "current.txt").exists()
    for path in targets.values():
        assert not (path / dr.TRANSACTION_FILE).exists()
        assert not [child for child in path.iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


def test_accept_roll_forward_failure_is_explicit_and_preserves_decision(
    tmp_path: Path,
    monkeypatch,
):
    archive, _ = _create(tmp_path)
    targets = _target_roots(tmp_path / "target")
    for name, path in targets.items():
        (path / "current.txt").write_text(name, encoding="utf-8")
    original_accept = dr._accept_target
    original_remove = dr._remove
    accept_calls = 0
    finalize_calls: list[str] = []
    failed_finalizes = {"data", "minio"}

    def fail_second_accept(target: Path, *, expected_asset: str | None = None):
        nonlocal accept_calls
        accept_calls += 1
        if accept_calls == 2:
            raise dr.SnapshotError("injected second accept failure")
        return original_accept(target, expected_asset=expected_asset)

    stage_paths = {
        name: path / dr._stage_base_name("test-generation")
        for name, path in targets.items()
    }

    def fail_selected_stage_removals(candidate: Path):
        for name, stage_path in stage_paths.items():
            if candidate == stage_path:
                marker_path = targets[name] / dr.TRANSACTION_FILE
                if marker_path.exists():
                    marker = dr._load_marker(targets[name], expected_asset=name)
                    if marker is not None and marker["status"] == "finalizing":
                        finalize_calls.append(name)
                        if name in failed_finalizes:
                            original_remove(stage_path / "new")
                            raise dr.SnapshotError(
                                f"injected {name} finalize failure"
                            )
                break
        return original_remove(candidate)

    monkeypatch.setattr(dr, "_accept_target", fail_second_accept)
    monkeypatch.setattr(dr, "_remove", fail_selected_stage_removals)

    with pytest.raises(dr.SnapshotError, match="全局提交待继续") as raised:
        dr.restore_snapshot(archive_path=archive, targets=targets)

    assert isinstance(raised.value.__cause__, dr.SnapshotError)
    assert finalize_calls == ["data", "redis", "minio", "config"]
    for name in failed_finalizes:
        marker = dr._load_marker(targets[name], expected_asset=name)
        assert marker is not None and marker["status"] == "finalizing"
        assert stage_paths[name].is_dir()
        assert not (stage_paths[name] / "new").exists()
    for name in ("redis", "config"):
        assert not (targets[name] / dr.TRANSACTION_FILE).exists()

    failed_finalizes.clear()
    dr._recover_target_set(targets)
    for path in targets.values():
        assert not (path / dr.TRANSACTION_FILE).exists()
        assert not (path / "current.txt").exists()
        assert not [child for child in path.iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


def test_interrupted_transaction_recovery_uses_global_commit_decision(tmp_path: Path):
    archive, _ = _create(tmp_path)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    dr._extract_archive(archive, extracted)
    manifest = dr.validate_extracted(extracted)
    generation = manifest["generation"]

    rollback_targets = _target_roots(tmp_path / "rollback-target")
    for name in ("data", "redis"):
        (rollback_targets[name] / "current.txt").write_text(name, encoding="utf-8")
        dr._prepare_target(extracted / "assets" / name, rollback_targets[name], generation, name)
    dr._commit_target(rollback_targets["data"])

    dr._recover_target_set([rollback_targets["data"], rollback_targets["redis"]])

    assert (rollback_targets["data"] / "current.txt").read_text(encoding="utf-8") == "data"
    assert (rollback_targets["redis"] / "current.txt").read_text(encoding="utf-8") == "redis"

    accepted_targets = _target_roots(tmp_path / "accepted-target")
    for name in ("data", "redis"):
        (accepted_targets[name] / "current.txt").write_text(name, encoding="utf-8")
        dr._prepare_target(extracted / "assets" / name, accepted_targets[name], generation, name)
        dr._commit_target(accepted_targets[name])
    dr._accept_target(accepted_targets["data"])

    dr._recover_target_set([accepted_targets["data"], accepted_targets["redis"]])

    assert (accepted_targets["data"] / "db" / "analyzer.db").is_file()
    assert (accepted_targets["redis"] / "dump.rdb").is_file()
    for name in ("data", "redis"):
        assert not (accepted_targets[name] / dr.TRANSACTION_FILE).exists()
        assert not [child for child in accepted_targets[name].iterdir() if child.name.startswith(dr.STAGE_PREFIX)]


@pytest.mark.parametrize(
    "case",
    [
        "format-bool",
        "generation-bool",
        "asset-bool",
        "asset-unknown",
        "base-parent",
        "base-absolute",
        "base-nested",
        "base-generation-mismatch",
        "status-bool",
        "status-unknown",
        "old-list-string",
        "old-name-bool",
        "old-name-parent",
        "new-name-absolute",
        "new-name-nested",
        "new-name-backslash",
        "new-name-null",
        "preserve-stage-name",
        "moved-not-subset",
        "duplicate-name",
        "unordered-names",
        "unknown-field",
        "missing-field",
    ],
)
def test_invalid_transaction_marker_is_fail_closed_before_any_cleanup(
    tmp_path: Path,
    case: str,
):
    target = tmp_path / "target"
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "sentinel.bin").write_bytes(b"SIBLING-SENTINEL")
    marker = _transaction_marker_fixture(target)

    if case == "format-bool":
        marker["format"] = True
    elif case == "generation-bool":
        marker["generation"] = True
    elif case == "asset-bool":
        marker["asset"] = True
    elif case == "asset-unknown":
        marker["asset"] = "unknown"
    elif case == "base-parent":
        marker["base"] = "../sibling"
    elif case == "base-absolute":
        marker["base"] = str(sibling.resolve())
    elif case == "base-nested":
        marker["base"] = f"{marker['base']}/nested"
    elif case == "base-generation-mismatch":
        marker["generation"] = "other-generation"
    elif case == "status-bool":
        marker["status"] = True
    elif case == "status-unknown":
        marker["status"] = "finished"
    elif case == "old-list-string":
        marker["old_names"] = "current.bin"
    elif case == "old-name-bool":
        marker["old_names"] = [True]
    elif case == "old-name-parent":
        marker["old_names"] = ["../sibling"]
    elif case == "new-name-absolute":
        marker["new_names"] = [str(sibling.resolve())]
    elif case == "new-name-nested":
        marker["new_names"] = ["nested/file.bin"]
    elif case == "new-name-backslash":
        marker["new_names"] = ["nested\\file.bin"]
    elif case == "new-name-null":
        marker["new_names"] = ["bad\x00name"]
    elif case == "preserve-stage-name":
        marker["preserve_names"] = [f"{dr.STAGE_PREFIX}escape"]
    elif case == "moved-not-subset":
        marker["moved_new"] = ["ghost.bin"]
    elif case == "duplicate-name":
        marker["new_names"] = ["same.bin", "same.bin"]
    elif case == "unordered-names":
        marker["old_names"] = ["z.bin", "a.bin"]
    elif case == "unknown-field":
        marker["poison"] = "value"
    else:
        del marker["moved_old"]
    _replace_transaction_marker(target, marker)
    before_target = _tree_state(target)
    before_sibling = _tree_state(sibling)

    with pytest.raises(dr.SnapshotError, match="恢复事务"):
        dr._recover_target(target)

    assert _tree_state(target) == before_target
    assert _tree_state(sibling) == before_sibling


@pytest.mark.parametrize(
    "symlink_case",
    ["marker", "base", "old", "new", "current-name"],
)
def test_transaction_marker_symlink_escape_is_fail_closed(
    tmp_path: Path,
    symlink_case: str,
):
    target = tmp_path / "target"
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    sentinel = sibling / "sentinel.bin"
    sentinel.write_bytes(b"SIBLING-SENTINEL")
    marker = _transaction_marker_fixture(target)
    base = target / marker["base"]

    if symlink_case == "marker":
        external_marker = sibling / "marker.json"
        external_marker.write_text(json.dumps(marker), encoding="utf-8")
        (target / dr.TRANSACTION_FILE).unlink()
        (target / dr.TRANSACTION_FILE).symlink_to(external_marker)
    elif symlink_case == "base":
        shutil.rmtree(base)
        base.symlink_to(sibling, target_is_directory=True)
    elif symlink_case in {"old", "new"}:
        shutil.rmtree(base / symlink_case)
        (base / symlink_case).symlink_to(sibling, target_is_directory=True)
    else:
        marker["old_names"] = ["linked.bin"]
        _replace_transaction_marker(target, marker)
        (target / "linked.bin").symlink_to(sentinel)
    before_target = _tree_state(target)
    before_sibling = _tree_state(sibling)

    with pytest.raises(dr.SnapshotError, match="恢复事务"):
        dr._recover_target(target)

    assert _tree_state(target) == before_target
    assert _tree_state(sibling) == before_sibling


def _prepare_transaction_pair(
    tmp_path: Path,
    *,
    left_generation: str,
    right_generation: str,
    right_asset: str = "redis",
) -> tuple[Path, Path]:
    left_source = tmp_path / "left-source"
    right_source = tmp_path / "right-source"
    left_source.mkdir()
    right_source.mkdir()
    (left_source / "new.bin").write_bytes(b"LEFT-NEW")
    (right_source / "new.bin").write_bytes(b"RIGHT-NEW")
    left = tmp_path / "left-target"
    right = tmp_path / "right-target"
    (left / "old.bin").parent.mkdir(parents=True)
    (right / "old.bin").parent.mkdir(parents=True)
    (left / "old.bin").write_bytes(b"LEFT-OLD")
    (right / "old.bin").write_bytes(b"RIGHT-OLD")
    dr._prepare_target(left_source, left, left_generation, "data")
    dr._prepare_target(right_source, right, right_generation, right_asset)
    return left, right


def test_recover_set_rejects_mixed_generation_before_cross_target_finalize(
    tmp_path: Path,
):
    left, right = _prepare_transaction_pair(
        tmp_path,
        left_generation="generation-a",
        right_generation="generation-b",
    )
    dr._commit_target(left)
    dr._accept_target(left)
    dr._commit_target(right)
    before = (_tree_state(left), _tree_state(right))

    with pytest.raises(dr.SnapshotError, match="混合 generation"):
        dr._recover_target_set({"data": left, "redis": right})

    assert (_tree_state(left), _tree_state(right)) == before


def test_recover_set_rejects_duplicate_or_mismatched_asset_before_cleanup(
    tmp_path: Path,
):
    left, right = _prepare_transaction_pair(
        tmp_path,
        left_generation="same-generation",
        right_generation="same-generation",
        right_asset="data",
    )
    before = (_tree_state(left), _tree_state(right))

    with pytest.raises(dr.SnapshotError, match="重复 asset"):
        dr._recover_target_set([left, right])
    assert (_tree_state(left), _tree_state(right)) == before

    with pytest.raises(dr.SnapshotError, match="asset 与目标不一致"):
        dr._recover_target_set({"redis": left})
    assert (_tree_state(left), _tree_state(right)) == before


def test_recover_set_rejects_accepted_and_uncommitted_status_mix(
    tmp_path: Path,
):
    left, right = _prepare_transaction_pair(
        tmp_path,
        left_generation="same-generation",
        right_generation="same-generation",
    )
    dr._commit_target(left)
    dr._accept_target(left)
    before = (_tree_state(left), _tree_state(right))

    with pytest.raises(dr.SnapshotError, match="未提交目标混合"):
        dr._recover_target_set({"data": left, "redis": right})

    assert (_tree_state(left), _tree_state(right)) == before


@pytest.mark.parametrize("user_version", range(_CURRENT_SCHEMA_VERSION + 1))
def test_supported_database_versions_pass_backup_compatibility_matrix(
    tmp_path: Path, user_version: int
):
    archive, _ = _create(tmp_path, user_version=user_version)

    manifest = dr.validate_archive(archive)

    assert manifest["sqlite"]["user_version"] == user_version
    assert len(
        manifest["compatibility"]["database_schema"]["migration_history"]
    ) == user_version


def test_dr_preserves_live_shape_legacy_glossary_through_restore_and_upgrade(
    tmp_path: Path,
):
    archive, _ = _create(
        tmp_path,
        user_version=1,
        include_legacy_glossary_backup=True,
    )
    manifest = dr.validate_archive(archive)
    targets = _target_roots(tmp_path / "legacy-preserve-target")
    result = dr.restore_snapshot(archive_path=archive, targets=targets)

    assert manifest["sqlite"]["user_version"] == 1
    assert result["status"] == "success"
    database_path = targets["data"] / "db" / "analyzer.db"
    restored = sqlite3.connect(database_path)
    before = restored.execute(
        f'SELECT rowid, * FROM "{_LEGACY_GLOSSARY_TABLE}" ORDER BY rowid'
    ).fetchall()
    before_sql = restored.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (_LEGACY_GLOSSARY_TABLE,),
    ).fetchone()[0]
    restored.close()
    assert len(before) == 249
    assert isinstance(before[0][3], bytes)

    database = Database(database_path)
    database.init_schema()
    try:
        after = database._conn.execute(
            f'SELECT rowid, * FROM "{_LEGACY_GLOSSARY_TABLE}" ORDER BY rowid'
        ).fetchall()
        assert [tuple(row) for row in after] == before
        assert database._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (_LEGACY_GLOSSARY_TABLE,),
        ).fetchone()[0] == before_sql
        database.init_schema()
        repeated = database._conn.execute(
            f'SELECT rowid, * FROM "{_LEGACY_GLOSSARY_TABLE}" ORDER BY rowid'
        ).fetchall()
        assert [tuple(row) for row in repeated] == before
    finally:
        database.close()

    reopened = Database(database_path)
    reopened.init_schema()
    try:
        repeated = reopened._conn.execute(
            f'SELECT rowid, * FROM "{_LEGACY_GLOSSARY_TABLE}" ORDER BY rowid'
        ).fetchall()
        assert [tuple(row) for row in repeated] == before
    finally:
        reopened.close()


def test_unsupported_sqlite_version_fails_compatibility_gate(tmp_path: Path):
    future_version = _CURRENT_SCHEMA_VERSION + 1
    future_manifest = _extended_schema_manifest(tmp_path, future_version)
    archive, _ = _create(
        tmp_path, user_version=future_version, schema_manifest_path=future_manifest
    )

    with pytest.raises(dr.SnapshotError, match="不在当前恢复程序范围"):
        dr.validate_archive(archive)


def test_future_program_accepts_current_snapshot_when_history_prefix_matches(
    tmp_path: Path,
):
    archive, _ = _create(tmp_path, user_version=_CURRENT_SCHEMA_VERSION)
    future_manifest = _extended_schema_manifest(
        tmp_path, _CURRENT_SCHEMA_VERSION + 1,
    )

    manifest = dr.validate_archive(
        archive, schema_manifest_path=future_manifest
    )

    assert manifest["sqlite"]["user_version"] == _CURRENT_SCHEMA_VERSION


def test_same_user_version_with_divergent_migration_checksum_is_rejected(tmp_path: Path):
    archive, _ = _create(tmp_path, user_version=2)

    def diverge(manifest: dict) -> None:
        database_schema = manifest["compatibility"]["database_schema"]
        database_schema["migration_history"][1]["checksum"] = "0" * 64
        database_schema["migration_history_sha256"] = dr._migration_history_fingerprint(
            database_schema["migration_history"]
        )

    rewritten = _rewrite_manifest(tmp_path, archive, diverge)

    with pytest.raises(dr.SnapshotError, match="迁移历史.*分叉"):
        dr.validate_archive(rewritten)


def test_source_with_tampered_sqlite_ledger_is_not_published_as_backup(tmp_path: Path):
    sources = _fixture_roots(tmp_path / "source", user_version=2)
    database = sources["data"] / "db" / "analyzer.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,)
    )
    connection.commit()
    connection.close()
    archive = tmp_path / "backup" / "tampered-source.tar.gz"

    with pytest.raises(dr.SnapshotError, match="schema_migrations.*不一致"):
        dr.create_snapshot(
            data_root=sources["data"],
            redis_root=sources["redis"],
            minio_root=sources["minio"],
            config_root=sources["config"],
            output=archive,
            generation="tampered-source",
        )

    assert not archive.exists()
    assert not archive.with_suffix(archive.suffix + ".sha256").exists()


def test_archive_with_tampered_sqlite_ledger_fails_before_target_change(tmp_path: Path):
    archive, _ = _create(tmp_path, user_version=2)
    rewritten = _rewrite_sqlite_ledger(tmp_path, archive)
    targets = _target_roots(tmp_path / "ledger-target")
    sentinel = targets["data"] / "sentinel"
    sentinel.write_bytes(b"current")

    with pytest.raises(dr.SnapshotError, match="schema_migrations.*manifest.sqlite"):
        dr.restore_snapshot(archive_path=rewritten, targets=targets)

    assert sentinel.read_bytes() == b"current"
    assert _inventory(targets["redis"]) == []
    assert not list((tmp_path / "ledger-target").rglob(".flori-dr-*"))


@pytest.mark.parametrize(
    ("case", "mutate"),
    _INVALID_APPLICATION_SCHEMAS,
    ids=[case for case, _ in _INVALID_APPLICATION_SCHEMAS],
)
def test_backup_create_rejects_schema_that_cannot_start_application(
    tmp_path: Path,
    case: str,
    mutate,
):
    sources = _fixture_roots(tmp_path / "source", user_version=2)
    database = sources["data"] / "db" / "analyzer.db"
    connection = sqlite3.connect(database)
    mutate(connection)
    connection.commit()
    connection.close()
    archive = tmp_path / "backups" / f"invalid-{case}.tar.gz"

    with pytest.raises(dr.SnapshotError, match="migration chain"):
        dr.create_snapshot(
            data_root=sources["data"],
            redis_root=sources["redis"],
            minio_root=sources["minio"],
            config_root=sources["config"],
            output=archive,
            generation=f"invalid-{case}",
        )

    assert not archive.exists()
    assert not archive.with_suffix(archive.suffix + ".sha256").exists()


@pytest.mark.parametrize(
    ("case", "mutate"),
    _INVALID_APPLICATION_SCHEMAS,
    ids=[case for case, _ in _INVALID_APPLICATION_SCHEMAS],
)
def test_restore_rejects_unstartable_schema_before_any_target_switch(
    tmp_path: Path,
    case: str,
    mutate,
):
    archive, _ = _create(tmp_path, user_version=2)
    rewritten = _rewrite_sqlite(tmp_path, archive, mutate, suffix=case)
    targets = _target_roots(tmp_path / "invalid-target")
    for name, target in targets.items():
        (target / "current.txt").write_text(name, encoding="utf-8")
    before = {name: _inventory(target) for name, target in targets.items()}

    with pytest.raises(dr.SnapshotError, match="migration chain"):
        dr.restore_snapshot(archive_path=rewritten, targets=targets)

    assert {name: _inventory(target) for name, target in targets.items()} == before
    assert not list((tmp_path / "invalid-target").rglob(".flori-dr-*"))


def test_format_v2_missing_history_is_fail_closed(tmp_path: Path):
    archive, _ = _create(tmp_path, user_version=2)

    def remove_history(manifest: dict) -> None:
        del manifest["compatibility"]["database_schema"]["migration_history"]

    rewritten = _rewrite_manifest(tmp_path, archive, remove_history)

    with pytest.raises(dr.SnapshotError, match="database_schema"):
        dr.validate_archive(rewritten)


@pytest.mark.parametrize(
    "field",
    [
        "sqlite.user_version",
        "compatibility.sqlite_user_version",
        "database_schema.migration.version",
    ],
)
def test_json_bool_cannot_impersonate_archive_integer_fields_after_refingerprint(
    tmp_path: Path,
    field: str,
):
    archive, _ = _create(tmp_path, user_version=1)

    def replace_integer_with_bool(manifest: dict) -> None:
        if field == "sqlite.user_version":
            manifest["sqlite"]["user_version"] = True
        elif field == "compatibility.sqlite_user_version":
            manifest["compatibility"]["sqlite_user_version"] = True
        else:
            schema = manifest["compatibility"]["database_schema"]
            schema["migration_history"][0]["version"] = True
            schema["migration_history_sha256"] = dr._migration_history_fingerprint(
                schema["migration_history"]
            )

    rewritten = _rewrite_manifest(tmp_path, archive, replace_integer_with_bool)

    with pytest.raises(dr.SnapshotError, match="整数|版本|migration_history"):
        dr.validate_archive(rewritten)


def test_fixed_legacy_format_v1_archive_remains_restorable(tmp_path: Path):
    legacy = tmp_path / "legacy-format-v1.tar.gz"
    legacy.write_bytes(
        base64.b64decode(
            (_DR_FIXTURES / "legacy-format-v1.tar.gz.base64").read_text(
                encoding="ascii"
            )
        )
    )
    shutil.copy2(
        _DR_FIXTURES / "legacy-format-v1.tar.gz.sha256",
        legacy.with_suffix(legacy.suffix + ".sha256"),
    )

    validated = dr.validate_archive(legacy)
    targets = _target_roots(tmp_path / "legacy-target")
    result = dr.restore_snapshot(archive_path=legacy, targets=targets)

    assert validated["format_version"] == 1
    assert validated["sqlite"]["user_version"] == 0
    assert "migration_history" not in validated["sqlite"]
    assert "database_schema" not in validated["compatibility"]
    assert result["status"] == "success"
    connection = sqlite3.connect(targets["data"] / "db" / "analyzer.db")
    try:
        assert connection.execute(
            "SELECT title FROM jobs WHERE id='jobs_test'"
        ).fetchone() == ("灾备测试",)
    finally:
        connection.close()


def test_archive_path_traversal_is_rejected(tmp_path: Path):
    archive = tmp_path / "escape.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        payload = b"escape"
        member = tarfile.TarInfo("../escape")
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )

    with pytest.raises(dr.SnapshotError, match="路径非法"):
        dr.validate_archive(archive)

    assert not (tmp_path.parent / "escape").exists()


def test_existing_archive_is_never_overwritten(tmp_path: Path):
    sources = _fixture_roots(tmp_path / "source")
    archive = tmp_path / "snapshot.tar.gz"
    archive.write_bytes(b"known-good")

    with pytest.raises(dr.SnapshotError, match="拒绝覆盖"):
        dr.create_snapshot(
            data_root=sources["data"],
            redis_root=sources["redis"],
            output=archive,
            generation="same-generation",
        )

    assert archive.read_bytes() == b"known-good"


def test_empty_environment_drill_writes_machine_readable_rpo_rto(tmp_path: Path):
    result_path = tmp_path / "drill-result.json"

    result = dr.run_empty_environment_drill(result_path)

    stored = json.loads(result_path.read_text(encoding="utf-8"))
    assert result == stored
    assert stored["status"] == "success"
    assert stored["rpo_seconds"] >= 0
    assert stored["rto_seconds"] >= 0
    assert stored["checks"] == {
        "backup_atomic_publish": "ok",
        "corrupt_snapshot_fail_closed": "ok",
        "cross_asset_rollback": "ok",
        "empty_environment_restore": "ok",
    }


def test_operational_shell_wrappers_are_valid_bash():
    for name in ("backup.sh", "restore.sh", "dr-drill.sh"):
        completed = subprocess.run(
            ["bash", "-n", str(_MODULE_PATH.parent / name)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
