"""验证 SQLite 逐版迁移、失败回滚与历史兼容。"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

import shared.db as db_module
import shared.ids as ids_module
from shared.db import Database, SCHEMA_VERSION, UnsupportedSchemaVersionError
from shared.migrations import v0001_legacy_baseline as migration_v1
from shared.migrations import v0002_immutable_ledger as migration_v2
from shared.migrations import v0003_srs_consistency as migration_v3
from shared.migrations import v0004_study_suggestions as migration_v4
from shared.migrations import v0005_canonical_evidence as migration_v5
from shared.migrations import v0006_concept_definition_history as migration_v6
from shared.migrations import v0007_unified_document as migration_v7
from shared.migrations import v0008_multipart_jobs as migration_v8
from shared.migrations import (
    Migration,
    MigrationExecutionError,
    MigrationHistoryError,
    load_manifest,
    run_migrations,
    validate_registry,
)


FIXTURES = Path(__file__).parent / "fixtures" / "migrations"
LEGACY_GLOSSARY_TABLE = "glossary_bak_clean_20260617"
FTS_SHADOW_TABLES = (
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


def _hashed_lineage(prefix: str, url: str) -> str:
    return f"jobs_{prefix}_{hashlib.sha1(url.encode()).hexdigest()[:10]}"


_BASE_89DC_LINEAGE_GOLDEN = (
    (
        "https://b23.tv/short-link",
        "video",
        None,
        _hashed_lineage("bili", "https://b23.tv/short-link"),
    ),
    ("BV1ab411c7mD", "video", None, "jobs_bili_BV1ab411c7mD"),
    (
        "https://www.bilibili.com/video/BV1ab411c7mD?p=1",
        "video",
        None,
        "jobs_bili_BV1ab411c7mD",
    ),
    (
        "https://example.com/watch/BV1ab411c7mD",
        "video",
        None,
        _hashed_lineage("article", "https://example.com/watch/BV1ab411c7mD"),
    ),
    (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "video",
        None,
        "jobs_yt_dQw4w9WgXcQ",
    ),
    (
        "https://youtu.be/dQw4w9WgXcQ",
        "video",
        None,
        "jobs_yt_dQw4w9WgXcQ",
    ),
    (
        "https://arxiv.org/abs/2301.00001v2",
        "paper",
        None,
        "jobs_arxiv_2301.00001v2",
    ),
    (
        "https://files.example.net/Paper.PDF?download=1",
        "paper",
        None,
        _hashed_lineage("paper", "https://files.example.net/Paper.PDF?download=1"),
    ),
    (
        "https://cdn.example.net/Episode.MP3?token=1",
        "audio",
        None,
        _hashed_lineage("audio", "https://cdn.example.net/Episode.MP3?token=1"),
    ),
    *tuple(
        (
            f"https://example.net/{content_type}",
            content_type,
            None,
            _hashed_lineage("article", f"https://example.net/{content_type}"),
        )
        for content_type in ("video", "paper", "article", "audio")
    ),
    (
        "https://elsewhere.test/BV1ab411c7mD",
        "video",
        "bilibili",
        "jobs_bili_BV1ab411c7mD",
    ),
    (
        "2301.00001v3",
        "paper",
        "arxiv",
        "jobs_arxiv_2301.00001v3",
    ),
    (
        "https://example.net/no-youtube-id",
        "video",
        "youtube",
        _hashed_lineage("yt", "https://example.net/no-youtube-id"),
    ),
    (
        "https://example.net/landing",
        "audio",
        "podcast",
        _hashed_lineage("audio", "https://example.net/landing"),
    ),
    (
        "https://example.net/landing",
        "audio",
        "http_article",
        _hashed_lineage("article", "https://example.net/landing"),
    ),
    (
        "https://example.net/file.pdf",
        "paper",
        "direct_pdf",
        _hashed_lineage("paper", "https://example.net/file.pdf"),
    ),
    (
        "file:///data/inbox/movie.mp4",
        "video",
        "upload",
        _hashed_lineage("video", "file:///data/inbox/movie.mp4"),
    ),
    (
        "https://example.net/article",
        "video",
        "other",
        _hashed_lineage("video", "https://example.net/article"),
    ),
)


def _load_fixture(path: Path, name: str) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript((FIXTURES / name).read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    return path


def _apply_fixture_fragment(path: Path, name: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript((FIXTURES / name).read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def _legacy_glossary_rows(connection: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in connection.execute(
            f'SELECT rowid, * FROM "{LEGACY_GLOSSARY_TABLE}" ORDER BY rowid'
        ).fetchall()
    ]


def _append_table_item(sql: str, item: str) -> str:
    closing = sql.rfind(")")
    assert closing > 0
    return sql[:closing] + f", {item}" + sql[closing:]


def _add_shadow_column(sql: str, table: str) -> str:
    if table.endswith("_idx"):
        marker = ", PRIMARY KEY"
        assert marker in sql
        return sql.replace(marker, ", poison TEXT, PRIMARY KEY", 1)
    return _append_table_item(sql, "poison TEXT")


def _rewrite_schema_sql(connection: sqlite3.Connection, name: str, transform) -> None:
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
    connection.commit()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger(database: Database) -> list[tuple[int, str, str]]:
    return [
        (int(row[0]), str(row[1]), str(row[2]))
        for row in database._conn.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]


def _initialize_in_process(path: str, queue) -> None:
    try:
        database = Database(path)
        database.init_schema()
        version = database.schema_version()
        database.close()
        queue.put(("ok", version))
    except BaseException as exc:
        queue.put(("error", type(exc).__name__, str(exc)))


def _leave_committed_wal(path: str, user_version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE crash_wal(value TEXT NOT NULL)")
    connection.execute("INSERT INTO crash_wal VALUES ('committed')")
    connection.commit()
    connection.execute(f"PRAGMA user_version={user_version}")
    connection.commit()
    os._exit(0)


def _leave_reused_wal(path: str, user_version: int) -> None:
    """用真实 RESTART checkpoint 留下未截断的上一代物理尾帧。"""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "CREATE TABLE reused_wal(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.commit()
    for index in range(300):
        connection.execute(
            "INSERT INTO reused_wal(value) VALUES (?)",
            (f"public-{index}",),
        )
        connection.commit()
    connection.execute(f"PRAGMA user_version={user_version - 1}")
    connection.commit()
    checkpoint = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
        os._exit(2)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE reused_wal SET value='current' WHERE id=1")
    connection.execute(f"PRAGMA user_version={user_version}")
    connection.commit()
    os._exit(0)


def _leave_reused_uncommitted_wal(path: str, user_version: int) -> None:
    """RESTART 后强制 cache spill,留下当前代未提交前缀与旧代尾帧。"""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("PRAGMA cache_size=5")
    connection.execute("PRAGMA cache_spill=ON")
    connection.execute(
        "CREATE TABLE reused_uncommitted(id INTEGER PRIMARY KEY, value BLOB NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO reused_uncommitted(value) VALUES (?)",
        [(bytes([index % 251]) * 3000,) for index in range(300)],
    )
    connection.execute(f"PRAGMA user_version={user_version}")
    connection.commit()
    checkpoint = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
        os._exit(2)
    connection.execute("BEGIN IMMEDIATE")
    for index in range(1, 301):
        connection.execute(
            "UPDATE reused_uncommitted SET value=? WHERE id=?",
            (bytes([(index + 7) % 251]) * 3000, index),
        )
    os._exit(0)


def _leave_checkpointed_wal(path: str, user_version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "CREATE TABLE checkpointed_wal(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    for index in range(100):
        connection.execute(
            "INSERT INTO checkpointed_wal(value) VALUES (?)",
            (f"public-{index}",),
        )
        connection.commit()
    connection.execute(f"PRAGMA user_version={user_version}")
    connection.commit()
    checkpoint = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
        os._exit(2)
    os._exit(0)


def _leave_three_generation_reused_wal(path: str, user_version: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "CREATE TABLE three_generation(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO three_generation VALUES (1, 'initial')")
    connection.commit()
    for index in range(300):
        connection.execute(
            "UPDATE three_generation SET value=? WHERE id=1",
            (f"generation-a-{index}",),
        )
        connection.commit()
    connection.execute(f"PRAGMA user_version={user_version - 1}")
    connection.commit()
    first = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    if first is None or first[0] != 0 or first[1] != first[2]:
        os._exit(2)
    connection.execute("UPDATE three_generation SET value='generation-b' WHERE id=1")
    connection.commit()
    second = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
    if second is None or second[0] != 0 or second[1] != second[2]:
        os._exit(3)
    for index in range(10):
        connection.execute(
            "UPDATE three_generation SET value=? WHERE id=1",
            (f"generation-c-{index}",),
        )
        connection.commit()
    connection.execute(f"PRAGMA user_version={user_version}")
    connection.commit()
    os._exit(0)


def _spawn_wal_writer(path: Path, target, *args) -> Path:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=target, args=(str(path), *args))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    wal = path.with_name(path.name + "-wal")
    assert wal.stat().st_size > 32
    return wal


def _reference_wal_checksum(
    payload: bytes,
    checksum: tuple[int, int],
    byteorder: str,
) -> tuple[int, int]:
    assert len(payload) % 8 == 0
    first, second = checksum
    for offset in range(0, len(payload), 8):
        first_word = int.from_bytes(payload[offset : offset + 4], byteorder)
        second_word = int.from_bytes(
            payload[offset + 4 : offset + 8], byteorder
        )
        first = (first + first_word + second) & 0xFFFFFFFF
        second = (second + second_word + first) & 0xFFFFFFFF
    return first, second


def _rewrite_wal_index_header(shm: Path, mutate) -> None:
    payload = bytearray(shm.read_bytes())
    header = bytearray(payload[:48])
    mutate(header)
    checksum = _reference_wal_checksum(
        bytes(header[:40]),
        (0, 0),
        sys.byteorder,
    )
    header[40:44] = checksum[0].to_bytes(4, sys.byteorder)
    header[44:48] = checksum[1].to_bytes(4, sys.byteorder)
    payload[:48] = header
    payload[48:96] = header
    shm.write_bytes(payload)


def _reset_wal_header_without_new_frame(payload: bytes) -> bytes:
    header = bytearray(payload[:32])
    checkpoint_sequence = (struct.unpack(">I", header[12:16])[0] + 1) & 0xFFFFFFFF
    salt_one = (struct.unpack(">I", header[16:20])[0] + 1) & 0xFFFFFFFF
    salt_two = struct.unpack(">I", header[20:24])[0] ^ 0xA5A5A5A5
    header[12:16] = struct.pack(">I", checkpoint_sequence)
    header[16:20] = struct.pack(">I", salt_one)
    header[20:24] = struct.pack(">I", salt_two)
    magic = struct.unpack(">I", header[:4])[0]
    byteorder = "big" if magic == 0x377F0683 else "little"
    checksum = _reference_wal_checksum(bytes(header[:24]), (0, 0), byteorder)
    header[24:32] = struct.pack(">II", *checksum)
    return bytes(header) + payload[32:]


def _wal_page_size(payload: bytes) -> int:
    page_size = struct.unpack(">I", payload[8:12])[0]
    return 65536 if page_size == 1 else page_size


def _rechecksum_wal(payload: bytes, magic: int) -> bytes:
    rewritten = bytearray(payload)
    rewritten[:4] = struct.pack(">I", magic)
    byteorder = "big" if magic == 0x377F0683 else "little"
    checksum = _reference_wal_checksum(bytes(rewritten[:24]), (0, 0), byteorder)
    rewritten[24:32] = struct.pack(">II", *checksum)
    page_size = _wal_page_size(rewritten)
    frame_size = 24 + page_size
    assert (len(rewritten) - 32) % frame_size == 0
    for offset in range(32, len(rewritten), frame_size):
        frame = rewritten[offset : offset + frame_size]
        checksum = _reference_wal_checksum(
            bytes(frame[:8] + frame[24:]),
            checksum,
            byteorder,
        )
        rewritten[offset + 16 : offset + 24] = struct.pack(">II", *checksum)
    return bytes(rewritten)


def _wal_frames(payload: bytes) -> list[tuple[int, int, int | None, bytes]]:
    page_size = _wal_page_size(payload)
    frame_size = 24 + page_size
    frames: list[tuple[int, int, int | None, bytes]] = []
    for offset in range(32, len(payload), frame_size):
        frame = payload[offset : offset + frame_size]
        assert len(frame) == frame_size
        page_number, database_pages = struct.unpack(">II", frame[:8])
        version = (
            struct.unpack(">I", frame[24 + 60 : 24 + 64])[0]
            if page_number == 1
            else None
        )
        frames.append((page_number, database_pages, version, bytes(frame)))
    return frames


def _append_valid_page_one(
    payload: bytes,
    version: int,
    database_pages: int,
) -> bytes:
    frames = _wal_frames(payload)
    page_one = bytearray(
        next(frame[3] for frame in reversed(frames) if frame[0] == 1)
    )
    page_one[4:8] = struct.pack(">I", database_pages)
    page_one[24 + 60 : 24 + 64] = struct.pack(">I", version)
    magic = struct.unpack(">I", payload[:4])[0]
    byteorder = "big" if magic == 0x377F0683 else "little"
    previous_checksum = struct.unpack(">II", frames[-1][3][16:24])
    checksum = _reference_wal_checksum(
        bytes(page_one[:8] + page_one[24:]),
        previous_checksum,
        byteorder,
    )
    page_one[16:24] = struct.pack(">II", *checksum)
    return payload + page_one


def _append_valid_uncommitted_page_one(payload: bytes, version: int) -> bytes:
    return _append_valid_page_one(payload, version, 0)


def _sqlite_bundle_state(path: Path) -> dict[str, tuple[str, bytes]]:
    state: dict[str, tuple[str, bytes]] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path.with_name(path.name + suffix)
        if candidate.is_symlink():
            state[suffix] = ("symlink", str(candidate.readlink()).encode())
        elif candidate.is_dir():
            state[suffix] = ("directory", b"")
        elif candidate.is_file():
            state[suffix] = ("file", candidate.read_bytes())
        else:
            state[suffix] = ("missing", b"")
    return state


def _leave_hot_journal(
    path: str,
    original_version: int,
    crash_version: int,
) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA cache_size=10")
    connection.execute("CREATE TABLE hot_journal(id INTEGER PRIMARY KEY, body BLOB)")
    connection.executemany(
        "INSERT INTO hot_journal(body) VALUES (?)",
        [(b"x" * 4096,) for _ in range(64)],
    )
    connection.execute(f"PRAGMA user_version={original_version}")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(f"PRAGMA user_version={crash_version}")
    connection.execute("UPDATE hot_journal SET body=?", (b"y" * 4096,))
    os._exit(0)


def _patch_main_header_user_version(path: Path, version: int) -> None:
    with path.open("r+b") as stream:
        stream.seek(60)
        stream.write(struct.pack(">I", version))
        stream.flush()
        os.fsync(stream.fileno())


def _create_hot_journal(
    path: Path,
    *,
    original_version: int,
    crash_version: int,
) -> Path:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_leave_hot_journal,
        args=(str(path), original_version, crash_version),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    journal = path.with_name(path.name + "-journal")
    assert journal.stat().st_size > 512
    assert journal.read_bytes()[:8] == db_module._ROLLBACK_JOURNAL_MAGIC
    return journal


def test_registry_is_contiguous_and_matches_immutable_manifest(tmp_path: Path):
    database = Database(tmp_path / "registry.db")
    try:
        manifest = validate_registry(database._migration_steps())
    finally:
        database.close()

    assert manifest == load_manifest()
    assert SCHEMA_VERSION == 8
    assert [entry["version"] for entry in manifest["migrations"]] == list(range(1, 9))
    assert len({entry["checksum"] for entry in manifest["migrations"]}) == 8


def test_database_rejects_registry_divergence_before_filesystem_touch(
    tmp_path: Path, monkeypatch
):
    migrations = list(db_module.migration_steps())
    original = migrations[0]
    migrations[0] = Migration(
        version=original.version,
        name=original.name,
        payload=original.payload + "\n-- divergent payload",
        apply=original.apply,
        validate=original.validate,
    )
    monkeypatch.setattr(db_module, "migration_steps", lambda: tuple(migrations))
    path = tmp_path / "untouched" / "registry.db"

    with pytest.raises(MigrationHistoryError, match="checksum"):
        Database(path)

    assert not path.parent.exists()


def test_code_registry_rejects_bool_migration_version(tmp_path: Path):
    database = Database(tmp_path / "registry-bool.db")
    migrations = list(database._migration_steps())
    original = migrations[0]
    migrations[0] = Migration(
        version=True,
        name=original.name,
        payload=original.payload,
        apply=original.apply,
        validate=original.validate,
    )
    database.close()

    with pytest.raises(MigrationHistoryError, match="version 必须是整数"):
        validate_registry(migrations)


def test_future_schema_definition_and_external_id_helper_do_not_change_history_checksums(
    tmp_path: Path, monkeypatch
):
    database = Database(tmp_path / "frozen.db")
    before = [migration.checksum for migration in database._migration_steps()]

    monkeypatch.setattr(db_module, "_SCHEMA_SQL", "CREATE TABLE future_only(x)", raising=False)
    monkeypatch.setattr(Database, "_EXPECTED_COLUMNS", {"future_only": {}}, raising=False)
    monkeypatch.setattr(ids_module, "lineage_key", lambda *_args: "future-helper")

    after = [migration.checksum for migration in database._migration_steps()]
    database.close()
    assert after == before

    path = _load_fixture(tmp_path / "external-helper.db", "v0000_unversioned.sql")
    upgraded = Database(path)
    upgraded.init_schema()
    try:
        expected = "jobs_article_" + hashlib.sha1(
            b"https://example.com/v0"
        ).hexdigest()[:10]
        assert upgraded.get_job("legacy-v0-job").lineage_key == expected
    finally:
        upgraded.close()


@pytest.mark.parametrize(
    ("url", "content_type", "source", "expected"),
    _BASE_89DC_LINEAGE_GOLDEN,
)
def test_frozen_lineage_helper_matches_execution_base_golden_matrix(
    url: str,
    content_type: str,
    source: str | None,
    expected: str,
):
    assert migration_v1._frozen_lineage_key(url, content_type, source) == expected


@pytest.mark.parametrize(
    ("fixture", "old_version"),
    [("v0000_unversioned.sql", 0), ("v0001_pre_ledger.sql", 1)],
)
def test_v0_and_v1_lineage_backfill_match_frozen_execution_base(
    tmp_path: Path,
    fixture: str,
    old_version: int,
):
    path = _load_fixture(tmp_path / f"lineage-v{old_version}.db", fixture)
    connection = sqlite3.connect(path)
    try:
        for index, (url, content_type, source, _expected) in enumerate(
            _BASE_89DC_LINEAGE_GOLDEN
        ):
            columns = (
                "id, content_type, pipeline, url, title, domain, source, "
                "created_at, updated_at"
            )
            values: tuple[object, ...] = (
                f"lineage-case-{index}",
                content_type,
                content_type,
                url,
                f"lineage case {index}",
                "general",
                source,
                f"2026-03-01T00:00:{index:02d}+00:00",
                f"2026-03-01T00:00:{index:02d}+00:00",
            )
            if old_version == 1:
                columns += ", lineage_key, is_current"
                values += (None, 1)
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                values,
            )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    database.init_schema()
    try:
        actual = {
            str(row[0]): str(row[1])
            for row in database._conn.execute(
                "SELECT id, lineage_key FROM jobs WHERE id LIKE 'lineage-case-%'"
            ).fetchall()
        }
        assert actual == {
            f"lineage-case-{index}": expected
            for index, (_url, _content_type, _source, expected) in enumerate(
                _BASE_89DC_LINEAGE_GOLDEN
            )
        }
    finally:
        database.close()


def test_tampered_historical_execution_payload_is_rejected_before_database_change(
    tmp_path: Path, monkeypatch
):
    database = Database(tmp_path / "payload.db")
    before_version = database.schema_version()
    original = migration_v1.source_payload
    monkeypatch.setattr(
        migration_v1,
        "source_payload",
        lambda: original() + "\n# tampered",
    )

    with pytest.raises(MigrationHistoryError, match="checksum"):
        database.init_schema()
    try:
        assert database.schema_version() == before_version == 0
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone() is None
    finally:
        database.close()


def test_fresh_database_runs_every_version_without_creating_safety_backup(tmp_path: Path):
    path = tmp_path / "fresh.db"
    database = Database(path)
    database.init_schema()
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert [row[0] for row in _ledger(database)] == list(
            range(1, SCHEMA_VERSION + 1)
        )
        assert database._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        database.close()

    assert not (tmp_path / "migration-backups").exists()


def test_v3_migrates_legacy_srs_rows_to_epoch_revision_and_audit_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "study-v2.db"
    database = Database(path)
    run_migrations(database._conn, database._migration_steps(), target_version=2)
    database._conn.execute(
        """INSERT INTO study_cards
           (card_id, domain, card_type, front, back, explanation, evidence_json,
            status, source, created_at, updated_at)
           VALUES ('legacy-card','ml','basic','Q','A','','[]','active','manual',
                   '2026-07-09T00:00:00','2026-07-09T00:00:00')"""
    )
    database._conn.execute(
        """INSERT INTO study_reviews
           (card_id,due_at,interval_days,ease,repetitions,lapses,last_grade,
            last_reviewed_at,updated_at)
           VALUES ('legacy-card','2026-07-09T08:00:00+08:00',1,2.5,1,0,'good',
                   '2026-07-08T19:00:00-05:00','2026-07-09T00:00:00Z')"""
    )
    database._conn.execute(
        """INSERT INTO study_review_logs
           (id,card_id,grade,reviewed_at,response_ms,scheduled_due_at,next_due_at,
            interval_days,ease,repetitions,lapses)
           VALUES ('legacy-log','legacy-card','good','2026-07-09T00:00:00',100,
                   '2026-07-09T08:00:00+08:00','2026-07-10T00:00:00Z',
                   1,2.5,1,0)"""
    )
    database._conn.commit()

    database.init_schema()
    try:
        card = database.get_study_card("legacy-card")
        assert card["revision"] == 2
        assert card["review"]["due_at"] == "2026-07-09T00:00:00+00:00"
        log = database._conn.execute(
            """SELECT request_id,request_fingerprint,reviewed_at_epoch_us,
                      scheduled_due_at_epoch_us,revision_before,revision_after,
                      outcome_json FROM study_review_logs WHERE id='legacy-log'"""
        ).fetchone()
        assert log["request_id"] == "legacy:legacy-log"
        assert len(log["request_fingerprint"]) == 64
        assert log["reviewed_at_epoch_us"] == log["scheduled_due_at_epoch_us"]
        assert (log["revision_before"], log["revision_after"]) == (1, 2)
        outcome = json.loads(log["outcome_json"])
        assert outcome["legacy_migrated"] is True
        assert outcome["front"] == "Q"
        assert outcome["status"] == "active"
        assert outcome["revision"] == 2
        replay = database.record_study_review(
            request_id="legacy:legacy-log",
            card_id="legacy-card",
            grade="good",
            expected_revision=1,
            response_ms=100,
            reviewed_at="2026-07-12T00:00:00+00:00",
        )
        assert replay == outcome
        assert database._conn.execute(
            "SELECT COUNT(*) FROM study_review_logs WHERE card_id='legacy-card'"
        ).fetchone()[0] == 1
        migration_v8.validate(database._conn)
    finally:
        database.close()


def test_v3_assigns_legacy_revisions_by_instant_not_iso_text_order(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "study-offset-order-v2.db")
    run_migrations(database._conn, database._migration_steps(), target_version=2)
    database._conn.execute(
        """INSERT INTO study_cards
           (card_id,domain,card_type,front,back,explanation,evidence_json,status,
            source,created_at,updated_at)
           VALUES ('offset-order','ml','basic','Q','A','','[]','active','manual',
                   '2026-07-08T00:00:00Z','2026-07-08T00:00:00Z')"""
    )
    database._conn.executemany(
        """INSERT INTO study_review_logs
           (id,card_id,grade,reviewed_at,response_ms,scheduled_due_at,next_due_at,
            interval_days,ease,repetitions,lapses)
           VALUES (?,'offset-order','good',?,NULL,NULL,?,1,2.5,1,0)""",
        [
            (
                "actual-first",
                "2026-07-09T00:30:00+02:00",
                "2026-07-09T22:30:00+00:00",
            ),
            (
                "actual-second",
                "2026-07-08T23:00:00+00:00",
                "2026-07-09T23:00:00+00:00",
            ),
        ],
    )
    database._conn.commit()

    database.init_schema()
    try:
        revisions = database._conn.execute(
            """SELECT id,revision_before,revision_after
               FROM study_review_logs WHERE card_id='offset-order'
               ORDER BY revision_before"""
        ).fetchall()
        assert [tuple(row) for row in revisions] == [
            ("actual-first", 1, 2),
            ("actual-second", 2, 3),
        ]
        assert database.get_study_card("offset-order")["revision"] == 3
    finally:
        database.close()


def test_v3_has_own_complete_validator_and_does_not_precreate_stage_b_schema(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "study-v3.db")
    run_migrations(database._conn, database._migration_steps(), target_version=3)
    try:
        migration_v3.validate(database._conn)
        with pytest.raises(sqlite3.DatabaseError):
            migration_v2.validate(database._conn)
        tables = {
            str(row[0])
            for row in database._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert not {name for name in tables if "suggest" in name or "embedding" in name}
    finally:
        database.close()


def test_v3_to_v4_creates_suggestion_schema_without_vector_placeholders(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "study-v4.db")
    run_migrations(database._conn, database._migration_steps(), target_version=3)

    assert run_migrations(
        database._conn, database._migration_steps(), target_version=4
    ) == 4
    try:
        migration_v4.validate(database._conn)
        tables = {
            str(row[0])
            for row in database._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "study_suggestion_batches",
            "study_suggestion_inputs",
            "study_suggestion_evidence",
            "study_suggestions",
            "study_suggestion_evidence_links",
            "study_suggestion_operations",
        } <= tables
        assert not {
            name for name in tables if "vector" in name or "embedding" in name
        }
        assert [row[0] for row in _ledger(database)] == [1, 2, 3, 4]
    finally:
        database.close()


def test_v3_to_v4_failure_rolls_back_schema_ledger_and_version(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "study-v4-fault.db")
    run_migrations(database._conn, database._migration_steps(), target_version=3)

    def fail(version: int, _connection: sqlite3.Connection) -> None:
        if version == 4:
            raise RuntimeError("v4 fault")

    with pytest.raises(MigrationExecutionError, match="回滚到 v3"):
        run_migrations(
            database._conn,
            database._migration_steps(),
            fault_injector=fail,
        )
    try:
        assert database.schema_version() == 3
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='study_suggestions'"
        ).fetchone() is None
        assert database._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=4"
        ).fetchone() is None
        migration_v3.validate(database._conn)
        assert not database._conn.in_transaction
    finally:
        database.close()


def test_v4_to_v5_adds_canonical_evidence_without_vector_schema(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "canonical-v5.db")
    run_migrations(database._conn, database._migration_steps(), target_version=4)

    assert run_migrations(
        database._conn, database._migration_steps(), target_version=5
    ) == 5
    try:
        migration_v5.validate(database._conn)
        tables = {
            str(row[0])
            for row in database._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "canonical_evidence" in tables
        assert not {
            name for name in tables if "vector" in name or "embedding" in name
        }
        columns = {
            str(row["name"])
            for row in database._conn.execute(
                "PRAGMA table_info(study_suggestion_evidence)"
            ).fetchall()
        }
        assert "canonical_evidence_id" in columns
        assert [row[0] for row in _ledger(database)] == [1, 2, 3, 4, 5]
    finally:
        database.close()


def test_v5_to_v6_seeds_definition_history_without_forging_occurrences(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "concept-v6.db")
    run_migrations(database._conn, database._migration_steps(), target_version=5)
    database._conn.execute(
        """INSERT INTO glossary (
               domain, term, definition, occurrences, related, status,
               created_at, updated_at
           ) VALUES ('ml', 'RRF', '倒数排名融合',
                     '[{"job_id":"legacy-job","location":"old"}]',
                     '[]', 'accepted', '2026-01-01T00:00:00+00:00',
                     '2026-01-02T00:00:00+00:00')"""
    )
    database._conn.commit()

    assert run_migrations(
        database._conn, database._migration_steps(), target_version=6,
    ) == 6
    migration_v6.validate(database._conn)

    glossary = database._conn.execute(
        "SELECT * FROM glossary WHERE domain='ml' AND term='RRF'"
    ).fetchone()
    assert glossary is not None
    version = database._conn.execute(
        "SELECT * FROM concept_definition_versions "
        "WHERE definition_version_id=?",
        (glossary["current_definition_version_id"],),
    ).fetchone()
    assert version is not None
    assert version["definition"] == "倒数排名融合"
    assert version["strategy"] == "legacy_migration"
    assert version["version"] == 1
    assert version["supersedes_version_id"] is None
    assert version["source_evidence_ids_json"] == "[]"
    assert glossary["occurrences"] == (
        '[{"job_id":"legacy-job","location":"old"}]'
    )
    assert database._conn.execute(
        "SELECT count(*) FROM concept_occurrences"
    ).fetchone()[0] == 0
    database.close()


def test_v5_to_v6_failure_rolls_back_schema_seed_ledger_and_version(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "concept-v6-fault.db")
    run_migrations(database._conn, database._migration_steps(), target_version=5)
    database._conn.execute(
        "INSERT INTO glossary (domain, term, definition) VALUES ('ml','RRF','old')"
    )
    database._conn.commit()

    def fail(version: int, _connection: sqlite3.Connection) -> None:
        if version == 6:
            raise RuntimeError("v6 fault")

    with pytest.raises(MigrationExecutionError, match="回滚到 v5"):
        run_migrations(
            database._conn,
            database._migration_steps(),
            fault_injector=fail,
        )
    assert database.schema_version() == 5
    assert database._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='concept_definition_versions'"
    ).fetchone() is None
    assert database._conn.execute(
        "SELECT definition FROM glossary WHERE domain='ml' AND term='RRF'"
    ).fetchone()[0] == "old"
    assert [row[0] for row in _ledger(database)] == [1, 2, 3, 4, 5]
    migration_v5.validate(database._conn)
    database.close()


def test_v4_to_v5_failure_rolls_back_schema_ledger_and_version(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "canonical-v5-fault.db")
    run_migrations(database._conn, database._migration_steps(), target_version=4)

    def fail(version: int, _connection: sqlite3.Connection) -> None:
        if version == 5:
            raise RuntimeError("v5 fault")

    with pytest.raises(MigrationExecutionError, match="回滚到 v4"):
        run_migrations(
            database._conn,
            database._migration_steps(),
            fault_injector=fail,
        )
    try:
        assert database.schema_version() == 4
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_evidence'"
        ).fetchone() is None
        columns = {
            str(row["name"])
            for row in database._conn.execute(
                "PRAGMA table_info(study_suggestion_evidence)"
            ).fetchall()
        }
        assert "canonical_evidence_id" not in columns
        assert [row[0] for row in _ledger(database)] == [1, 2, 3, 4]
    finally:
        database.close()


def test_v2_to_v3_failure_rolls_back_schema_data_ledger_and_version(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "study-v3-fault.db")
    run_migrations(database._conn, database._migration_steps(), target_version=2)
    database._conn.execute(
        """INSERT INTO study_cards
           (card_id,domain,card_type,front,back,explanation,evidence_json,status,
            source,created_at,updated_at)
           VALUES ('rollback-card','ml','basic','Q','A','','[]','active','manual',
                   '2026-07-09T00:00:00+00:00','2026-07-09T00:00:00+00:00')"""
    )
    database._conn.commit()

    def fail(version: int, _connection: sqlite3.Connection) -> None:
        if version == 3:
            raise RuntimeError("v3 fault")

    with pytest.raises(MigrationExecutionError, match="回滚到 v2"):
        run_migrations(
            database._conn,
            database._migration_steps(),
            fault_injector=fail,
        )
    try:
        assert database.schema_version() == 2
        columns = {
            str(row[1])
            for row in database._conn.execute("PRAGMA table_info(study_cards)").fetchall()
        }
        assert "revision" not in columns
        assert database._conn.execute(
            "SELECT front FROM study_cards WHERE card_id='rollback-card'"
        ).fetchone()[0] == "Q"
        assert database._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=3"
        ).fetchone() is None
        assert not database._conn.in_transaction
    finally:
        database.close()


@pytest.mark.parametrize(
    ("fixture", "old_version", "job_id", "prompt"),
    [
        ("v0000_unversioned.sql", 0, "legacy-v0-job", "无版本历史 Prompt"),
        ("v0001_pre_ledger.sql", 1, "legacy-v1-job", "v1 历史 Prompt"),
    ],
)
def test_historical_fixtures_upgrade_across_all_missing_versions(
    tmp_path: Path,
    fixture: str,
    old_version: int,
    job_id: str,
    prompt: str,
):
    path = _load_fixture(tmp_path / f"v{old_version}.db", fixture)
    database = Database(path)
    database.init_schema()
    try:
        assert database.schema_version() == SCHEMA_VERSION
        job = database.get_job(job_id)
        assert job is not None and job.title
        history = database.get_prompt_override_version(
            "global", "", job.pipeline, "05_smart", 1,
            document_kind=job.document_kind,
        )
        assert history is not None and history["content"] == prompt
        tables = {
            row[0]
            for row in database._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "jobs",
            "workers",
            "note_chunks",
            "study_cards",
            "schema_migrations",
        } <= tables
        expected = load_manifest()["migrations"]
        assert _ledger(database) == [
            (entry["version"], entry["name"], entry["checksum"])
            for entry in expected
        ]
    finally:
        database.close()

    backup = (
        tmp_path
        / "migration-backups"
        / f"v{old_version}.pre-v{old_version}-to-v{SCHEMA_VERSION}.db"
    )
    assert backup.is_file()
    connection = sqlite3.connect(backup)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA user_version").fetchone() == (old_version,)
        assert connection.execute(
            "SELECT id FROM jobs WHERE id=?", (job_id,)
        ).fetchone() == (job_id,)
    finally:
        connection.close()
    assert not list((tmp_path / "migration-backups").glob(".*.tmp"))


def test_live_shape_legacy_glossary_backup_is_preserved_byte_for_byte(
    tmp_path: Path,
):
    path = _load_fixture(tmp_path / "legacy-preserve.db", "v0001_pre_ledger.sql")
    _apply_fixture_fragment(path, "legacy_glossary_backup.sql")
    before_connection = sqlite3.connect(path)
    before = _legacy_glossary_rows(before_connection)
    before_sql = before_connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (LEGACY_GLOSSARY_TABLE,),
    ).fetchone()[0]
    before_connection.close()
    assert len(before) == 249
    assert isinstance(before[0][3], bytes)

    database = Database(path)
    database.init_schema()
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert _legacy_glossary_rows(database._conn) == before
        assert database._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (LEGACY_GLOSSARY_TABLE,),
        ).fetchone()[0] == before_sql
        database.init_schema()
        assert _legacy_glossary_rows(database._conn) == before
    finally:
        database.close()

    reopened = Database(path)
    reopened.init_schema()
    try:
        assert _legacy_glossary_rows(reopened._conn) == before
    finally:
        reopened.close()
    backup = tmp_path / "migration-backups" / "legacy-preserve.pre-v1-to-v8.db"
    backup_connection = sqlite3.connect(backup)
    try:
        assert _legacy_glossary_rows(backup_connection) == before
    finally:
        backup_connection.close()


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "CREATE TABLE glossary_bak_clean_20260617(domain TEXT)",
        migration_v1.LEGACY_PRESERVED_TABLES[LEGACY_GLOSSARY_TABLE]
        + " STRICT",
    ],
    ids=["wrong-columns", "strict-table"],
)
def test_legacy_preserve_allowlist_rejects_shape_drift(
    tmp_path: Path,
    unsafe_sql: str,
):
    path = tmp_path / "legacy-shape.db"
    database = Database(path)
    database.init_schema()
    database._conn.execute(unsafe_sql)

    with pytest.raises(sqlite3.DatabaseError, match="历史保留表"):
        migration_v8.validate(database._conn)
    database.close()


@pytest.mark.parametrize(
    "reference_type",
    ["index", "foreign-key", "trigger", "view"],
)
def test_legacy_preserve_allowlist_rejects_schema_references(
    tmp_path: Path,
    reference_type: str,
):
    database = Database(tmp_path / f"legacy-{reference_type}.db")
    database.init_schema()
    database._conn.execute(
        migration_v1.LEGACY_PRESERVED_TABLES[LEGACY_GLOSSARY_TABLE]
    )
    if reference_type == "index":
        database._conn.execute(
            f"CREATE INDEX legacy_reference ON {LEGACY_GLOSSARY_TABLE}(domain)"
        )
    elif reference_type == "foreign-key":
        database._conn.execute(
            "CREATE TABLE legacy_reference("
            "domain TEXT REFERENCES glossary_bak_clean_20260617(domain))"
        )
    elif reference_type == "trigger":
        database._conn.execute(
            "CREATE TRIGGER legacy_reference AFTER INSERT ON jobs BEGIN "
            f"SELECT count(*) FROM {LEGACY_GLOSSARY_TABLE}; END"
        )
    else:
        database._conn.execute(
            "CREATE VIEW legacy_reference AS "
            f"SELECT domain FROM {LEGACY_GLOSSARY_TABLE}"
        )

    with pytest.raises(sqlite3.DatabaseError):
        migration_v8.validate(database._conn)
    database.close()


def test_repeated_init_keeps_ledger_and_backup_stable(tmp_path: Path):
    path = _load_fixture(tmp_path / "repeat.db", "v0001_pre_ledger.sql")
    database = Database(path)
    database.init_schema()
    before = _ledger(database)
    backup = tmp_path / "migration-backups" / "repeat.pre-v1-to-v8.db"
    before_backup = _sha256(backup)

    database.init_schema()
    assert _ledger(database) == before
    database.close()

    reopened = Database(path)
    reopened.init_schema()
    try:
        assert _ledger(reopened) == before
    finally:
        reopened.close()
    assert _sha256(backup) == before_backup


def test_future_schema_is_rejected_before_wal_or_database_mutation(tmp_path: Path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel VALUES ('future')")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    before = _sha256(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="user_version"):
        Database(path)

    assert _sha256(path) == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_future_schema_in_committed_crash_wal_is_rejected_before_shm_creation(
    tmp_path: Path,
):
    path = tmp_path / "future-wal.db"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_leave_committed_wal,
        args=(str(path), SCHEMA_VERSION + 1),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    assert wal.stat().st_size > 32
    shm.unlink(missing_ok=True)
    before = (_sha256(path), _sha256(wal))

    with pytest.raises(UnsupportedSchemaVersionError, match="连接前拒绝"):
        Database(path)

    assert (_sha256(path), _sha256(wal)) == before
    assert not shm.exists()


@pytest.mark.parametrize("magic", [0x377F0682, 0x377F0683])
def test_wal_checksum_endian_follows_magic_and_stored_values_are_big_endian(
    tmp_path: Path,
    magic: int,
):
    path = tmp_path / f"wal-magic-{magic:x}.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    converted = _rechecksum_wal(wal.read_bytes(), magic)
    wal.write_bytes(converted)
    byteorder = "big" if magic == 0x377F0683 else "little"
    expected_header = _reference_wal_checksum(
        converted[:24],
        (0, 0),
        byteorder,
    )

    assert struct.unpack(">I", converted[:4])[0] == magic
    assert struct.unpack(">I", converted[4:8])[0] == 3_007_000
    assert struct.unpack(">II", converted[24:32]) == expected_header
    frames, commits, _logical_size = db_module._validate_wal_bytes(path, wal)
    assert frames > 0
    assert commits > 0
    path.with_name(path.name + "-shm").unlink(missing_ok=True)
    before = _sqlite_bundle_state(path)
    assert db_module._committed_wal_user_version(path) == SCHEMA_VERSION
    assert _sqlite_bundle_state(path) == before


def test_supported_committed_wal_probe_is_read_only_and_database_can_open(
    tmp_path: Path,
):
    path = tmp_path / "supported-wal.db"
    _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION
    assert _sqlite_bundle_state(path) == before

    database = Database(path)
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert database._conn.execute(
            "SELECT count(*) FROM crash_wal WHERE value='committed'"
        ).fetchone()[0] == 1
    finally:
        database.close()


@pytest.mark.parametrize("keep_shm", [True, False], ids=["with-shm", "no-shm"])
def test_restart_reused_wal_ignores_untruncated_old_generation_tail(
    tmp_path: Path,
    keep_shm: bool,
):
    path = tmp_path / f"restart-reused-{keep_shm}.db"
    wal = _spawn_wal_writer(path, _leave_reused_wal, SCHEMA_VERSION)
    shm = path.with_name(path.name + "-shm")
    wal_payload = wal.read_bytes()
    frames = _wal_frames(wal_payload)
    wal_salts = wal_payload[16:24]
    shm_payload = shm.read_bytes()
    assert shm_payload[:48] == shm_payload[48:96]
    mx_frame = int.from_bytes(shm_payload[16:20], sys.byteorder)
    assert 0 < mx_frame < len(frames)
    assert all(frame[3][8:16] == wal_salts for frame in frames[:mx_frame])
    assert frames[mx_frame - 1][1] > 0
    assert frames[mx_frame][3][8:16] != wal_salts
    if not keep_shm:
        shm.unlink()
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert _sqlite_bundle_state(path) == before


def test_no_shm_reset_header_before_first_new_frame_recovers_main_database(
    tmp_path: Path,
):
    path = tmp_path / "reset-zero-current-frames.db"
    wal = _spawn_wal_writer(path, _leave_checkpointed_wal, SCHEMA_VERSION)
    wal.write_bytes(_reset_wal_header_without_new_frame(wal.read_bytes()))
    path.with_name(path.name + "-shm").unlink()
    frames, commits, logical_size = db_module._validate_wal_bytes(path, wal)
    assert frames == 0
    assert commits == 0
    assert logical_size == 32
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert _sqlite_bundle_state(path) == before


@pytest.mark.parametrize("keep_shm", [True, False], ids=["with-shm", "no-shm"])
def test_three_generation_reuse_accepts_non_adjacent_old_salt_tail(
    tmp_path: Path,
    keep_shm: bool,
):
    path = tmp_path / f"three-generation-{keep_shm}.db"
    wal = _spawn_wal_writer(
        path,
        _leave_three_generation_reused_wal,
        SCHEMA_VERSION,
    )
    shm = path.with_name(path.name + "-shm")
    payload = wal.read_bytes()
    frames = _wal_frames(payload)
    mx_frame = int.from_bytes(shm.read_bytes()[16:20], sys.byteorder)
    assert 0 < mx_frame < len(frames)
    current_salt = struct.unpack(">I", payload[16:20])[0]
    first_old_salt = struct.unpack(">I", frames[mx_frame][3][8:12])[0]
    assert (current_salt - first_old_salt) & 0xFFFFFFFF > 1
    if not keep_shm:
        shm.unlink()
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert _sqlite_bundle_state(path) == before


def test_no_shm_reused_wal_accepts_valid_uncommitted_prefix_before_old_tail(
    tmp_path: Path,
):
    path = tmp_path / "restart-uncommitted-no-shm.db"
    wal = _spawn_wal_writer(
        path,
        _leave_reused_uncommitted_wal,
        SCHEMA_VERSION,
    )
    path.with_name(path.name + "-shm").unlink()
    wal_payload = wal.read_bytes()
    frames = _wal_frames(wal_payload)
    wal_salts = wal_payload[16:24]
    current_prefix = 0
    for frame in frames:
        if frame[3][8:16] != wal_salts:
            break
        current_prefix += 1
        assert frame[1] == 0
    assert 0 < current_prefix < len(frames)
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert _sqlite_bundle_state(path) == before


@pytest.mark.parametrize(
    "case",
    [
        "truncated",
        "duplicate-header",
        "bad-header-checksum",
        "bad-salt",
        "mx-frame-out-of-range",
        "bad-frame-checksum",
        "bad-npage",
        "bad-nbackfill",
        "mx-zero-hides-commit",
    ],
)
def test_invalid_or_stale_wal_index_is_advisory_and_source_remains_unchanged(
    tmp_path: Path,
    case: str,
):
    path = tmp_path / f"invalid-wal-index-{case}.db"
    wal = _spawn_wal_writer(path, _leave_reused_wal, SCHEMA_VERSION)
    shm = path.with_name(path.name + "-shm")
    payload = bytearray(shm.read_bytes())
    mx_frame = int.from_bytes(payload[16:20], sys.byteorder)
    if case == "truncated":
        shm.write_bytes(payload[:135])
    elif case == "duplicate-header":
        payload[48] ^= 0x01
        shm.write_bytes(payload)
    elif case == "bad-header-checksum":
        header = bytearray(payload[:48])
        header[40] ^= 0x01
        payload[:48] = header
        payload[48:96] = header
        shm.write_bytes(payload)
    elif case == "bad-salt":
        _rewrite_wal_index_header(shm, lambda header: header.__setitem__(32, header[32] ^ 1))
    elif case == "mx-frame-out-of-range":
        physical_frames = len(_wal_frames(wal.read_bytes()))
        _rewrite_wal_index_header(
            shm,
            lambda header: header.__setitem__(
                slice(16, 20),
                (physical_frames + 1).to_bytes(4, sys.byteorder),
            ),
        )
    elif case == "bad-frame-checksum":
        _rewrite_wal_index_header(
            shm,
            lambda header: header.__setitem__(24, header[24] ^ 1),
        )
    elif case == "bad-npage":
        _rewrite_wal_index_header(
            shm,
            lambda header: header.__setitem__(
                slice(20, 24),
                (int.from_bytes(header[20:24], sys.byteorder) + 1).to_bytes(
                    4, sys.byteorder
                ),
            ),
        )
    elif case == "bad-nbackfill":
        payload[96:100] = (mx_frame + 1).to_bytes(4, sys.byteorder)
        shm.write_bytes(payload)
    else:
        _rewrite_wal_index_header(
            shm,
            lambda header: header.__setitem__(slice(16, 20), b"\x00" * 4),
        )
    before = _sqlite_bundle_state(path)
    wal_header = wal.read_bytes()[:32]
    shm_prefix = shm.read_bytes()[:136]
    parser_invalid = {
        "truncated",
        "duplicate-header",
        "bad-header-checksum",
        "bad-salt",
        "bad-nbackfill",
    }
    if case in parser_invalid:
        with pytest.raises(db_module._WalIndexValidationError):
            db_module._validate_wal_index(
                shm_prefix,
                wal_header=wal_header,
                page_size=_wal_page_size(wal_header),
                checksum_byteorder=(
                    "big"
                    if struct.unpack(">I", wal_header[:4])[0] == 0x377F0683
                    else "little"
                ),
                file_size=shm.stat().st_size,
            )

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert _sqlite_bundle_state(path) == before


def test_sparse_wal_index_reads_only_fixed_advisory_prefix(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "sparse-wal-index.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    shm = path.with_name(path.name + "-shm")
    with shm.open("r+b") as stream:
        stream.truncate(64 * 1024 * 1024)
    before = (
        _sha256(path),
        _sha256(wal),
        db_module._file_snapshot_signature(shm),
    )
    original_read_bytes = Path.read_bytes

    def forbid_full_shm_read(candidate: Path):
        if candidate == shm:
            raise AssertionError("SHM must not be read in full")
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", forbid_full_shm_read)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert (
        _sha256(path),
        _sha256(wal),
        db_module._file_snapshot_signature(shm),
    ) == before


@pytest.mark.parametrize(
    "version",
    [SCHEMA_VERSION, SCHEMA_VERSION + 1],
    ids=["supported", "future"],
)
def test_unreadable_wal_index_is_advisory_but_future_wal_still_blocks(
    tmp_path: Path,
    monkeypatch,
    version: int,
):
    path = tmp_path / f"unreadable-wal-index-{version}.db"
    _spawn_wal_writer(path, _leave_committed_wal, version)
    before = _sqlite_bundle_state(path)

    def fail_advisory_read(_descriptor: int, _size: int):
        raise OSError(5, "injected SHM read failure")

    monkeypatch.setattr(db_module.os, "read", fail_advisory_read)
    if version == SCHEMA_VERSION:
        assert db_module._probe_schema_version_without_sqlite(path) == version
    else:
        with pytest.raises(UnsupportedSchemaVersionError, match="高于当前程序上限"):
            Database(path)

    assert _sqlite_bundle_state(path) == before


def test_regular_wal_index_inode_replacement_does_not_block_wal_recovery(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "replaced-wal-index.db"
    _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    shm = path.with_name(path.name + "-shm")
    before = _sqlite_bundle_state(path)
    original_read = db_module._read_wal_index_advisory
    replaced = False

    def replace_after_read(candidate: Path):
        nonlocal replaced
        result = original_read(candidate)
        if not replaced:
            replacement = candidate.with_name(candidate.name + ".replacement")
            replacement.write_bytes(candidate.read_bytes())
            os.replace(replacement, candidate)
            replaced = True
        return result

    monkeypatch.setattr(db_module, "_read_wal_index_advisory", replace_after_read)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION
    assert replaced
    assert _sqlite_bundle_state(path) == before


@pytest.mark.parametrize("sidecar", ["database", "wal"])
def test_database_or_wal_read_error_remains_fail_closed(
    tmp_path: Path,
    monkeypatch,
    sidecar: str,
):
    path = tmp_path / f"unreadable-{sidecar}.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    before = _sqlite_bundle_state(path)
    original_open = Path.open

    def fail_temp_read(candidate: Path, mode="r", *args, **kwargs):
        is_temp_copy = candidate.parent != path.parent
        selected = candidate.name == (path.name if sidecar == "database" else wal.name)
        if is_temp_copy and selected and "r" in mode:
            raise OSError(5, f"injected {sidecar} read failure")
        return original_open(candidate, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_temp_read)

    with pytest.raises(UnsupportedSchemaVersionError, match="无法安全预恢复"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before


def test_invalid_wal_index_does_not_hide_future_committed_schema(
    tmp_path: Path,
):
    path = tmp_path / "invalid-index-future-wal.db"
    _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION + 1)
    shm = path.with_name(path.name + "-shm")
    payload = bytearray(shm.read_bytes())
    payload[48] ^= 0x01
    shm.write_bytes(payload)
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="高于当前程序上限"):
        Database(path)

    assert _sqlite_bundle_state(path) == before


def test_wal_index_path_race_to_fifo_is_nonblocking_and_fail_closed(
    tmp_path: Path,
):
    shm = tmp_path / "raced.db-shm"
    os.mkfifo(shm)

    with pytest.raises(UnsupportedSchemaVersionError, match="不是普通文件"):
        db_module._read_wal_index_advisory(shm)


@pytest.mark.parametrize(
    "version",
    [SCHEMA_VERSION, SCHEMA_VERSION + 1],
    ids=["supported", "future"],
)
def test_valid_wal_commit_ahead_of_index_is_recovered_without_hiding_future(
    tmp_path: Path,
    version: int,
):
    path = tmp_path / f"commit-after-mxframe-{version}.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    payload = wal.read_bytes()
    database_pages = next(
        frame[1] for frame in reversed(_wal_frames(payload)) if frame[1]
    )
    wal.write_bytes(
        _append_valid_page_one(
            payload,
            version,
            database_pages,
        )
    )
    before = _sqlite_bundle_state(path)

    if version == SCHEMA_VERSION:
        assert db_module._probe_schema_version_without_sqlite(path) == version
    else:
        with pytest.raises(UnsupportedSchemaVersionError, match="高于当前程序上限"):
            Database(path)

    assert _sqlite_bundle_state(path) == before


def test_wal_index_rejects_truncated_physical_tail_after_mxframe(
    tmp_path: Path,
):
    path = tmp_path / "truncated-after-mxframe.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    wal.write_bytes(wal.read_bytes() + b"truncated")
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="trailing bytes"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before


def test_wal_index_rejects_current_salt_return_inside_old_generation_tail(
    tmp_path: Path,
):
    path = tmp_path / "current-salt-return.db"
    wal = _spawn_wal_writer(path, _leave_reused_wal, SCHEMA_VERSION)
    shm = path.with_name(path.name + "-shm")
    shm_payload = shm.read_bytes()
    mx_frame = int.from_bytes(shm_payload[16:20], sys.byteorder)
    payload = bytearray(wal.read_bytes())
    page_size = _wal_page_size(payload)
    frame_size = 24 + page_size
    physical_frames = (len(payload) - 32) // frame_size
    assert physical_frames >= mx_frame + 2
    returning_frame_offset = 32 + (mx_frame + 1) * frame_size
    payload[returning_frame_offset + 8 : returning_frame_offset + 16] = payload[16:24]
    wal.write_bytes(payload)
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="旧代物理尾结构"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before


def test_valid_uncommitted_future_wal_tail_does_not_override_supported_commit(
    tmp_path: Path,
):
    path = tmp_path / "uncommitted-future-tail.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    wal.write_bytes(
        _append_valid_uncommitted_page_one(
            wal.read_bytes(),
            SCHEMA_VERSION + 1,
        )
    )
    frames = _wal_frames(wal.read_bytes())
    last_commit = max(
        index for index, frame in enumerate(frames) if frame[1] != 0
    )
    assert any(
        frame[0] == 1 and frame[2] == SCHEMA_VERSION + 1
        for frame in frames[last_commit + 1 :]
    )
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION
    assert _sqlite_bundle_state(path) == before

    database = Database(path)
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert database._conn.execute(
            "SELECT count(*) FROM crash_wal WHERE value='committed'"
        ).fetchone()[0] == 1
    finally:
        database.close()


def test_probe_truncates_temp_wal_to_last_commit_before_sqlite_connect(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "temp-logical-boundary.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    committed_payload = wal.read_bytes()
    wal.write_bytes(
        _append_valid_uncommitted_page_one(
            committed_payload,
            SCHEMA_VERSION + 1,
        )
    )
    frames = _wal_frames(committed_payload)
    last_commit = max(index for index, frame in enumerate(frames, start=1) if frame[1])
    expected_size = 32 + last_commit * (24 + _wal_page_size(committed_payload))
    before = _sqlite_bundle_state(path)
    original_connect = db_module.sqlite3.connect
    observed_sizes: list[int] = []

    def inspect_temp_wal(database, *args, **kwargs):
        candidate = Path(database)
        if candidate.parent != path.parent and candidate.name == path.name:
            copied_wal = candidate.with_name(candidate.name + "-wal")
            observed_sizes.append(copied_wal.stat().st_size)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(db_module.sqlite3, "connect", inspect_temp_wal)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION

    assert observed_sizes == [expected_size]
    assert _sqlite_bundle_state(path) == before


@pytest.mark.parametrize(
    "case",
    [
        "truncated-header",
        "bad-magic",
        "bad-format-version",
        "bad-page-size",
        "wal-page-size-one",
        "bad-main-wal-mode",
        "bad-header-checksum",
        "bad-frame-checksum",
        "truncated-frame",
        "trailing-bytes",
    ],
)
def test_malformed_wal_is_rejected_without_real_file_or_shm_mutation(
    tmp_path: Path,
    case: str,
):
    path = tmp_path / f"malformed-{case}.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION)
    payload = bytearray(wal.read_bytes())
    if case == "truncated-header":
        payload = payload[:31]
    elif case == "bad-magic":
        payload[:4] = b"BAD!"
    elif case == "bad-format-version":
        payload[4:8] = struct.pack(">I", 3_007_001)
    elif case == "bad-page-size":
        current = _wal_page_size(payload)
        payload[8:12] = struct.pack(">I", 8192 if current != 8192 else 4096)
    elif case == "wal-page-size-one":
        payload[8:12] = struct.pack(">I", 1)
    elif case == "bad-main-wal-mode":
        with path.open("r+b") as stream:
            stream.seek(18)
            stream.write(b"\x01\x01")
            stream.flush()
            os.fsync(stream.fileno())
    elif case == "bad-header-checksum":
        payload[24] ^= 0x01
    elif case == "bad-frame-checksum":
        payload[32 + 16] ^= 0x01
    elif case == "truncated-frame":
        payload = payload[:-1]
    else:
        payload.extend(b"trailing")
    wal.write_bytes(payload)
    shm = path.with_name(path.name + "-shm")
    shm.unlink(missing_ok=True)
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="WAL"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before


def test_first_noncurrent_salt_is_a_safe_stale_generation_boundary(
    tmp_path: Path,
):
    path = tmp_path / "stale-generation-boundary.db"
    wal = _spawn_wal_writer(path, _leave_checkpointed_wal, SCHEMA_VERSION)
    wal.write_bytes(_reset_wal_header_without_new_frame(wal.read_bytes()))
    path.with_name(path.name + "-shm").unlink(missing_ok=True)
    before = _sqlite_bundle_state(path)

    frames, commits, logical_size = db_module._validate_wal_bytes(path, wal)
    assert (frames, commits, logical_size) == (0, 0, 32)
    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION
    assert _sqlite_bundle_state(path) == before


@pytest.mark.parametrize("keep_shm", [True, False], ids=["with-shm", "no-shm"])
def test_future_wal_prefix_with_checksum_bad_supported_tail_is_fail_closed(
    tmp_path: Path,
    keep_shm: bool,
):
    path = tmp_path / f"future-prefix-bad-tail-{keep_shm}.db"
    wal = _spawn_wal_writer(path, _leave_committed_wal, SCHEMA_VERSION + 1)
    payload = wal.read_bytes()
    page_one = next(
        frame[3] for frame in reversed(_wal_frames(payload)) if frame[0] == 1
    )
    forged = bytearray(page_one)
    forged[4:8] = struct.pack(">I", max(1, struct.unpack(">I", forged[4:8])[0]))
    forged[24 + 60 : 24 + 64] = struct.pack(">I", SCHEMA_VERSION)
    wal.write_bytes(payload + forged)
    shm = path.with_name(path.name + "-shm")
    if not keep_shm:
        shm.unlink(missing_ok=True)
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="frame checksum"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before


def test_empty_regular_wal_is_explicitly_treated_as_no_committed_frames(
    tmp_path: Path,
):
    path = tmp_path / "empty-wal.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    path.with_name(path.name + "-wal").touch()
    path.with_name(path.name + "-shm").touch()
    before = _sqlite_bundle_state(path)

    assert db_module._probe_schema_version_without_sqlite(path) == SCHEMA_VERSION
    assert _sqlite_bundle_state(path) == before


def test_wal_probe_retries_once_after_snapshot_change(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "wal-retry-once.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    before = _sqlite_bundle_state(path)
    original_assert = db_module._assert_file_signatures
    calls = 0

    def change_once(signatures):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise db_module._ProbeFilesChanged("deterministic change")
        original_assert(signatures)

    monkeypatch.setattr(db_module, "_assert_file_signatures", change_once)

    assert db_module._committed_wal_user_version(path) == SCHEMA_VERSION
    assert calls == 2
    assert _sqlite_bundle_state(path) == before


def test_wal_probe_uses_read_only_view_after_three_snapshot_changes(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "wal-retry-exhausted.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    before = _sqlite_bundle_state(path)
    calls = 0

    def always_changed(_signatures):
        nonlocal calls
        calls += 1
        raise db_module._ProbeFilesChanged("deterministic change")

    monkeypatch.setattr(db_module, "_assert_file_signatures", always_changed)

    assert db_module._committed_wal_user_version(path) == SCHEMA_VERSION
    assert calls == 3
    assert _sqlite_bundle_state(path) == before


def test_read_only_fallback_still_rejects_future_schema_before_writable_open(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "future-wal-read-only-fallback.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    before = _sqlite_bundle_state(path)

    def always_changed(_signatures):
        raise db_module._ProbeFilesChanged("active writer")

    monkeypatch.setattr(db_module, "_assert_file_signatures", always_changed)

    with pytest.raises(UnsupportedSchemaVersionError, match="高于当前程序上限"):
        Database(path)
    assert _sqlite_bundle_state(path) == before


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [
        ("-wal", "symlink"),
        ("-wal", "directory"),
        ("-journal", "symlink"),
        ("-shm", "symlink"),
        ("-shm", "directory"),
    ],
)
def test_sidecar_type_is_checked_before_no_or_empty_wal_early_return(
    tmp_path: Path,
    suffix: str,
    kind: str,
):
    path = tmp_path / f"sidecar-{suffix[1:]}-{kind}.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    sidecar = path.with_name(path.name + suffix)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"VICTIM")
    if kind == "symlink":
        sidecar.symlink_to(victim)
    else:
        sidecar.mkdir()
    before = _sqlite_bundle_state(path)
    victim_before = victim.read_bytes()

    with pytest.raises(UnsupportedSchemaVersionError, match="sidecar"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before
    assert victim.read_bytes() == victim_before


def test_empty_main_checks_empty_sidecar_type_before_returning_zero(
    tmp_path: Path,
):
    path = tmp_path / "empty-main-sidecar.db"
    path.touch()
    victim = tmp_path / "empty-victim.bin"
    victim.touch()
    path.with_name(path.name + "-wal").symlink_to(victim)
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="sidecar"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before


def test_hot_journal_with_shm_symlink_is_rejected_before_copy_or_recovery(
    tmp_path: Path,
):
    path = tmp_path / "journal-shm-symlink.db"
    _create_hot_journal(
        path,
        original_version=SCHEMA_VERSION,
        crash_version=SCHEMA_VERSION,
    )
    victim = tmp_path / "shm-victim.bin"
    victim.write_bytes(b"SHM-VICTIM")
    path.with_name(path.name + "-shm").symlink_to(victim)
    before = _sqlite_bundle_state(path)

    with pytest.raises(UnsupportedSchemaVersionError, match="sidecar"):
        db_module._probe_schema_version_without_sqlite(path)

    assert _sqlite_bundle_state(path) == before
    assert victim.read_bytes() == b"SHM-VICTIM"


def test_future_recovery_version_in_hot_journal_is_rejected_without_side_effect(
    tmp_path: Path,
):
    path = tmp_path / "future-journal.db"
    journal = _create_hot_journal(
        path,
        original_version=SCHEMA_VERSION + 1,
        crash_version=SCHEMA_VERSION,
    )
    _patch_main_header_user_version(path, SCHEMA_VERSION)
    before = (_sha256(path), _sha256(journal))

    with pytest.raises(UnsupportedSchemaVersionError, match="连接前拒绝"):
        Database(path)

    assert (_sha256(path), _sha256(journal)) == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_supported_hot_journal_overrides_future_crash_header_and_recovers(
    tmp_path: Path,
):
    path = tmp_path / "supported-journal.db"
    journal = _create_hot_journal(
        path,
        original_version=SCHEMA_VERSION,
        crash_version=SCHEMA_VERSION + 1,
    )
    _patch_main_header_user_version(path, SCHEMA_VERSION + 1)

    database = Database(path)
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert database._conn.execute(
            "SELECT count(*) FROM hot_journal WHERE body=?", (b"x" * 4096,)
        ).fetchone()[0] == 64
    finally:
        database.close()

    assert (
        not journal.exists()
        or journal.read_bytes()[:8] != db_module._ROLLBACK_JOURNAL_MAGIC
    )


def test_ordinary_supported_hot_journal_is_recovered_before_wal_switch(
    tmp_path: Path,
):
    path = tmp_path / "ordinary-journal.db"
    journal = _create_hot_journal(
        path,
        original_version=SCHEMA_VERSION,
        crash_version=SCHEMA_VERSION,
    )

    database = Database(path)
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert database._conn.execute(
            "SELECT count(*) FROM hot_journal WHERE body=?", (b"x" * 4096,)
        ).fetchone()[0] == 64
    finally:
        database.close()

    assert (
        not journal.exists()
        or journal.read_bytes()[:8] != db_module._ROLLBACK_JOURNAL_MAGIC
    )


@pytest.mark.parametrize(
    "journal_payload",
    [b"damaged-journal", db_module._ROLLBACK_JOURNAL_MAGIC + b"\x00" * 504],
    ids=["damaged", "stale-hot-header"],
)
def test_stale_or_damaged_rollback_journal_fails_before_real_database_open(
    tmp_path: Path,
    journal_payload: bytes,
):
    path = tmp_path / "bad-journal.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel VALUES ('supported')")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    connection.close()
    journal = path.with_name(path.name + "-journal")
    journal.write_bytes(journal_payload)
    before = (_sha256(path), _sha256(journal))

    with pytest.raises(UnsupportedSchemaVersionError, match="journal"):
        Database(path)

    assert (_sha256(path), _sha256(journal)) == before
    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal"])
def test_nonempty_sidecar_beside_empty_main_database_is_fail_closed(
    tmp_path: Path,
    suffix: str,
):
    path = tmp_path / "empty-main.db"
    path.touch()
    sidecar = path.with_name(path.name + suffix)
    sidecar.write_bytes(b"orphaned-sidecar")
    before = (_sha256(path), _sha256(sidecar))

    with pytest.raises(UnsupportedSchemaVersionError, match="sidecar"):
        Database(path)

    assert (_sha256(path), _sha256(sidecar)) == before
    assert not path.with_name(path.name + "-shm").exists()


def test_failure_after_real_migration_body_rolls_back_schema_data_ledger_and_version(
    tmp_path: Path,
):
    path = _load_fixture(tmp_path / "fault.db", "v0001_pre_ledger.sql")
    database = Database(path)

    def fail_after_apply(version: int, connection: sqlite3.Connection) -> None:
        if version == 2:
            connection.execute(
                "INSERT INTO jobs (id, content_type, pipeline, domain, created_at, updated_at) "
                "VALUES ('must-rollback', 'article', 'article', 'general', 'now', 'now')"
            )
            raise RuntimeError("故障注入")

    with pytest.raises(MigrationExecutionError, match="已回滚"):
        run_migrations(
            database._conn,
            database._migration_steps(),
            fault_injector=fail_after_apply,
        )

    assert database.schema_version() == 1
    assert database._conn.execute(
        "SELECT 1 FROM jobs WHERE id='must-rollback'"
    ).fetchone() is None
    assert database._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is None
    assert database._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workers'"
    ).fetchone() is None
    assert not database._conn.in_transaction

    database.init_schema()
    try:
        assert database.schema_version() == SCHEMA_VERSION
    finally:
        database.close()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_base_exception_rolls_back_chain_and_is_rethrown_unchanged(
    tmp_path: Path,
    interrupt: type[BaseException],
):
    path = _load_fixture(tmp_path / f"{interrupt.__name__}.db", "v0001_pre_ledger.sql")
    database = Database(path)

    def stop(_version: int, _connection: sqlite3.Connection) -> None:
        raise interrupt("停止")

    with pytest.raises(interrupt, match="停止"):
        run_migrations(
            database._conn,
            database._migration_steps(),
            fault_injector=stop,
        )
    try:
        assert database.schema_version() == 1
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None
        assert not database._conn.in_transaction
    finally:
        database.close()


def test_incomplete_v1_schema_fails_invariant_check_without_version_advance(tmp_path: Path):
    path = _load_fixture(tmp_path / "incomplete-v1.db", "v0001_pre_ledger.sql")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE workers(id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    database = Database(path)
    with pytest.raises(MigrationExecutionError, match="workers.*缺少列"):
        database.init_schema()
    try:
        assert database.schema_version() == 1
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is None
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='study_cards'"
        ).fetchone() is None
        assert {
            row["name"]
            for row in database._conn.execute("PRAGMA table_info(workers)").fetchall()
        } == {"id"}
    finally:
        database.close()


def test_conflicting_ledger_row_is_detected_before_commit_and_rolls_back_v9(
    tmp_path: Path,
):
    path = tmp_path / "ledger-conflict.db"
    database = Database(path)
    database.init_schema()
    database._conn.execute(
        """INSERT INTO jobs
           (id,content_type,document_kind,pipeline,title,domain,created_at,updated_at)
           VALUES ('sentinel','document','article','document','before','general','now','now')"""
    )
    database._conn.commit()

    payload = "future-v9-ledger-conflict-fixture"

    def apply_v9(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE future_v9(value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_v9 VALUES ('must-rollback')")
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
            "VALUES (9, 'conflict', ?, 'now')",
            ("0" * 64,),
        )

    migrations = (
        *database._migration_steps(),
        Migration(9, "future-v9", payload, apply_v9),
    )
    manifest = load_manifest()
    manifest["current_version"] = 9
    manifest["migrations"].append(
        {
            "version": 9,
            "name": "future-v9",
            "checksum": hashlib.sha256(payload.encode()).hexdigest(),
        }
    )
    manifest_path = tmp_path / "manifest-v9.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(MigrationHistoryError, match="v9.*不一致"):
        run_migrations(
            database._conn,
            migrations,
            manifest_path=manifest_path,
        )
    try:
        assert database.schema_version() == 8
        assert database._conn.execute(
            "SELECT title FROM jobs WHERE id='sentinel'"
        ).fetchone()[0] == "before"
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='future_v9'"
        ).fetchone() is None
        assert database._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=9"
        ).fetchone() is None
    finally:
        database.close()


def test_pending_v8_to_v10_chain_rolls_back_every_step_when_v10_fails(
    tmp_path: Path,
):
    path = tmp_path / "atomic-chain.db"
    database = Database(path)
    database.init_schema()

    def apply_v9(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE future_v9(value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_v9 VALUES ('v9')")

    def apply_v10(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE future_v10(value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_v10 VALUES ('v10')")

    schema_v9 = migration_v8.CURRENT_SCHEMA_SQL + "\nCREATE TABLE future_v9(value TEXT NOT NULL);\n"
    schema_v10 = schema_v9 + "\nCREATE TABLE future_v10(value TEXT NOT NULL);\n"

    def validate_v9(connection: sqlite3.Connection) -> None:
        migration_v1._validate_complete_schema(connection, schema_v9)

    def validate_v10(connection: sqlite3.Connection) -> None:
        migration_v1._validate_complete_schema(connection, schema_v10)

    payload_v9 = "synthetic-atomic-v9"
    payload_v10 = "synthetic-atomic-v10"
    migrations = (
        *database._migration_steps(),
        Migration(9, "synthetic-v9", payload_v9, apply_v9, validate_v9),
        Migration(10, "synthetic-v10", payload_v10, apply_v10, validate_v10),
    )
    manifest = load_manifest()
    manifest["current_version"] = 10
    manifest["migrations"].extend(
        [
            {
                "version": 9,
                "name": "synthetic-v9",
                "checksum": hashlib.sha256(payload_v9.encode()).hexdigest(),
            },
            {
                "version": 10,
                "name": "synthetic-v10",
                "checksum": hashlib.sha256(payload_v10.encode()).hexdigest(),
            },
        ]
    )
    manifest_path = tmp_path / "manifest-v10.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    def fail_v10(version: int, _connection: sqlite3.Connection) -> None:
        if version == 10:
            raise RuntimeError("v10 后段故障")

    with pytest.raises(MigrationExecutionError, match="回滚到 v8"):
        run_migrations(
            database._conn,
            migrations,
            manifest_path=manifest_path,
            fault_injector=fail_v10,
        )

    assert database.schema_version() == 8
    assert database._conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('future_v9', 'future_v10')"
    ).fetchall() == []
    assert [
        row[0]
        for row in database._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ] == list(range(1, 9))

    assert run_migrations(
        database._conn, migrations, manifest_path=manifest_path
    ) == 10
    try:
        assert database._conn.execute(
            "SELECT value FROM future_v9"
        ).fetchone()[0] == "v9"
        assert database._conn.execute(
            "SELECT value FROM future_v10"
        ).fetchone()[0] == "v10"
        assert [
            row[0]
            for row in database._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == list(range(1, 11))
    finally:
        database.close()


@pytest.mark.parametrize(
    ("object_type", "drop_sql", "tampered_sql"),
    [
        (
            "view",
            "DROP VIEW future_jobs",
            "CREATE VIEW future_jobs AS SELECT title FROM jobs",
        ),
        (
            "trigger",
            "DROP TRIGGER future_jobs_guard",
            "CREATE TRIGGER future_jobs_guard BEFORE DELETE ON jobs "
            "BEGIN SELECT RAISE(ABORT, 'tampered'); END",
        ),
    ],
)
def test_future_complete_validator_rejects_same_name_trigger_or_view_sql_tamper(
    tmp_path: Path,
    object_type: str,
    drop_sql: str,
    tampered_sql: str,
):
    database = Database(tmp_path / f"future-{object_type}.db")
    database.init_schema()
    future_objects = (
        "CREATE VIEW future_jobs AS SELECT id FROM jobs;\n"
        "CREATE TRIGGER future_jobs_guard BEFORE DELETE ON jobs "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END;\n"
    )
    expected_schema = migration_v8.CURRENT_SCHEMA_SQL + "\n" + future_objects
    migration_v1._execute_sql_script(database._conn, future_objects)
    migration_v1._validate_complete_schema(database._conn, expected_schema)

    database._conn.execute(drop_sql)
    database._conn.execute(tampered_sql)
    with pytest.raises(sqlite3.DatabaseError, match="trigger/view SQL"):
        migration_v1._validate_complete_schema(database._conn, expected_schema)
    database.close()


@pytest.mark.parametrize("shadow_table", FTS_SHADOW_TABLES)
def test_complete_validator_and_database_reject_fts_shadow_extra_column(
    tmp_path: Path,
    shadow_table: str,
):
    path = tmp_path / f"shadow-{shadow_table}.db"
    database = Database(path)
    database.init_schema()
    _rewrite_schema_sql(
        database._conn,
        shadow_table,
        lambda sql: _add_shadow_column(sql, shadow_table),
    )
    with pytest.raises(sqlite3.DatabaseError, match=shadow_table):
        migration_v8.validate(database._conn)
    database.close()

    reopened = None
    try:
        with pytest.raises(sqlite3.DatabaseError, match=shadow_table):
            reopened = Database(path)
            reopened.init_schema()
    finally:
        if reopened is not None:
            reopened.close()


@pytest.mark.parametrize(
    "content_table",
    ["notes_fts5_content", "note_chunks_fts5_content"],
)
def test_complete_validator_rejects_fts_content_blocking_check(
    tmp_path: Path,
    content_table: str,
):
    database = Database(tmp_path / f"shadow-check-{content_table}.db")
    database.init_schema()
    _rewrite_schema_sql(
        database._conn,
        content_table,
        lambda sql: _append_table_item(sql, "CHECK(c0 <> 'blocked')"),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match=content_table):
            migration_v8.validate(database._conn)
    finally:
        database.close()


def test_complete_validator_keeps_normal_fts_writes_working(tmp_path: Path):
    database = Database(tmp_path / "fts-write.db")
    database.init_schema()
    try:
        database._conn.execute(
            "INSERT INTO notes_fts5 "
            "(job_id, content_type, note_type, collection_id, domain, title, body) "
            "VALUES ('job-fts', 'document', 'note', 'col', 'general', "
            "'正常标题', '正常写入全文检索')"
        )
        database._conn.execute(
            "INSERT INTO note_chunks_fts5 "
            "(chunk_id, job_id, note_type, content_type, collection_id, domain, "
            "title, section, body, evidence_json) VALUES "
            "('chunk-fts', 'job-fts', 'note', 'document', 'col', 'general', "
            "'正常标题', '章节', '正常写入分块检索', '{}')"
        )
        database._conn.commit()
        migration_v8.validate(database._conn)
        assert database._conn.execute(
            "SELECT job_id FROM notes_fts5 WHERE notes_fts5 MATCH '正常写入'"
        ).fetchone()[0] == "job-fts"
        assert database._conn.execute(
            "SELECT chunk_id FROM note_chunks_fts5 "
            "WHERE note_chunks_fts5 MATCH '正常写入'"
        ).fetchone()[0] == "chunk-fts"
    finally:
        database.close()


@pytest.mark.parametrize(
    "table_item",
    ["poison TEXT", "CHECK(name <> 'Poison')"],
    ids=["extra-column", "blocking-check"],
)
def test_sqlite_sequence_shape_is_part_of_complete_schema(
    tmp_path: Path,
    table_item: str,
):
    database = Database(tmp_path / "sqlite-sequence.db")
    database.init_schema()
    _rewrite_schema_sql(
        database._conn,
        "sqlite_sequence",
        lambda sql: _append_table_item(sql, table_item),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="sqlite_sequence"):
            migration_v8.validate(database._conn)
    finally:
        database.close()


def test_reserved_sqlite_prefix_extra_object_is_not_globally_ignored(tmp_path: Path):
    database = Database(tmp_path / "sqlite-poison.db")
    database.init_schema()
    database._conn.execute("CREATE TABLE poison(value TEXT)")
    schema_version = int(
        database._conn.execute("PRAGMA schema_version").fetchone()[0]
    )
    database._conn.execute("PRAGMA writable_schema=ON")
    try:
        database._conn.execute(
            "UPDATE sqlite_master SET name='sqlite_poison', "
            "tbl_name='sqlite_poison', sql='CREATE TABLE sqlite_poison(value TEXT)' "
            "WHERE type='table' AND name='poison'"
        )
    finally:
        database._conn.execute("PRAGMA writable_schema=OFF")
    database._conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    database._conn.commit()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="sqlite_poison"):
            migration_v8.validate(database._conn)
    finally:
        database.close()


def test_sqlite_analyze_statistics_are_the_only_ignored_internal_tables(
    tmp_path: Path,
):
    database = Database(tmp_path / "sqlite-stats.db")
    database.init_schema()
    try:
        database._conn.execute("ANALYZE")
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0] == 1
        migration_v8.validate(database._conn)
    finally:
        database.close()


def test_ignored_sqlite_statistics_must_keep_native_shape(tmp_path: Path):
    database = Database(tmp_path / "sqlite-stats-shape.db")
    database.init_schema()
    database._conn.execute("ANALYZE")
    _rewrite_schema_sql(
        database._conn,
        "sqlite_stat1",
        lambda sql: _append_table_item(sql, "poison TEXT"),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="sqlite_stat1"):
            migration_v8.validate(database._conn)
    finally:
        database.close()


@pytest.mark.parametrize(
    "schema_sql",
    [
        "CREATE TABLE demo(a TEXT,b TEXT)",
        "CREATE TABLE demo(\n    a TEXT,\n    b TEXT\n)",
        "CREATE TABLE demo ( a TEXT , b TEXT )",
    ],
    ids=["compact", "multiline", "comma-spacing"],
)
def test_sql_normalization_ignores_nonsemantic_punctuation_spacing(
    schema_sql: str,
):
    assert migration_v1._normalize_sql(schema_sql) == (
        "create table demo(a text,b text)"
    )


def test_sql_normalization_preserves_quoted_literal_content():
    canonical = migration_v1._normalize_sql(
        "CREATE TABLE demo(value TEXT DEFAULT 'A B')"
    )

    assert migration_v1._normalize_sql(
        "CREATE TABLE demo(value TEXT DEFAULT 'A  B')"
    ) != canonical
    assert migration_v1._normalize_sql(
        "CREATE TABLE demo(value TEXT DEFAULT 'a b')"
    ) != canonical


@pytest.mark.parametrize(
    "commented",
    [
        "CREATE/* only formatting */TABLE demo(a TEXT,b TEXT)",
        "CREATE -- only formatting\nTABLE demo(a TEXT,b TEXT)",
        "CREATE TABLE demo(a TEXT /* column */,b TEXT)",
    ],
    ids=["block-between-tokens", "line-between-tokens", "block-before-comma"],
)
def test_sql_normalization_treats_comments_as_nonsemantic_spacing(
    commented: str,
):
    assert migration_v1._normalize_sql(commented) == (
        "create table demo(a text,b text)"
    )


def test_sql_markers_ignore_keywords_and_comment_syntax_inside_literal():
    literal = (
        "'AUTOINCREMENT CHECK COLLATE ON CONFLICT DEFERRABLE "
        "-- keep /* keep */ A''B  C'"
    )
    sql = f"CREATE TABLE demo(value TEXT DEFAULT {literal})"

    assert literal in migration_v1._normalize_sql(sql)
    assert migration_v1._normalize_default(literal) == literal
    assert migration_v1._normalize_default("'A  B'") != (
        migration_v1._normalize_default("'A B'")
    )
    assert migration_v1._write_semantic_markers(sql) == (
        (),
        (),
        (),
        (),
        0,
    )


def test_comment_cannot_impersonate_removed_autoincrement(tmp_path: Path):
    database = Database(tmp_path / "comment-autoincrement.db")
    database.init_schema()
    _rewrite_schema_sql(
        database._conn,
        "ai_usage",
        lambda sql: sql.replace(
            "PRIMARY KEY AUTOINCREMENT",
            "PRIMARY KEY /* AUTOINCREMENT */",
            1,
        ),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="ai_usage.*写语义"):
            migration_v8.validate(database._conn)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("object_name", "future_sql", "constraint"),
    [
        (
            "marker_check",
            "CREATE TABLE marker_check(value TEXT CHECK(length(value)>0));",
            "CHECK(length(value)>0)",
        ),
        (
            "marker_collate",
            "CREATE TABLE marker_collate(value TEXT COLLATE NOCASE);",
            "COLLATE NOCASE",
        ),
        (
            "marker_conflict",
            "CREATE TABLE marker_conflict("
            "value TEXT UNIQUE ON CONFLICT REPLACE);",
            "ON CONFLICT REPLACE",
        ),
        (
            "marker_child",
            "CREATE TABLE marker_parent(id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE marker_child(parent_id INTEGER REFERENCES "
            "marker_parent(id) DEFERRABLE INITIALLY DEFERRED);",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    ],
    ids=["check", "collate", "on-conflict", "deferrable"],
)
def test_comment_cannot_impersonate_removed_write_constraint(
    tmp_path: Path,
    object_name: str,
    future_sql: str,
    constraint: str,
):
    database = Database(tmp_path / f"comment-{object_name}.db")
    database.init_schema()
    expected_schema = migration_v8.CURRENT_SCHEMA_SQL + "\n" + future_sql
    migration_v1._execute_sql_script(database._conn, future_sql)
    migration_v1._validate_complete_schema(database._conn, expected_schema)
    _rewrite_schema_sql(
        database._conn,
        object_name,
        lambda sql: sql.replace(constraint, f"/* {constraint} */", 1),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            migration_v1._validate_complete_schema(
                database._conn,
                expected_schema,
            )
    finally:
        database.close()


def test_harmless_comment_does_not_change_real_constraint_semantics(
    tmp_path: Path,
):
    database = Database(tmp_path / "comment-harmless.db")
    database.init_schema()
    future_sql = (
        "CREATE TABLE marker_harmless("
        "value TEXT CHECK(length(value)>0));"
    )
    expected_schema = migration_v8.CURRENT_SCHEMA_SQL + "\n" + future_sql
    migration_v1._execute_sql_script(database._conn, future_sql)
    _rewrite_schema_sql(
        database._conn,
        "marker_harmless",
        lambda sql: sql.replace(
            "CHECK(length(value)>0)",
            "CHECK(/* harmless */ length(value)>0)",
            1,
        ),
    )
    try:
        migration_v1._validate_complete_schema(
            database._conn,
            expected_schema,
        )
    finally:
        database.close()


def test_sqlite_stat4_without_stat1_is_not_a_safe_statistics_shape(
    tmp_path: Path,
):
    database = Database(tmp_path / "sqlite-stat4-alone.db")
    database.init_schema()
    schema_version = int(
        database._conn.execute("PRAGMA schema_version").fetchone()[0]
    )
    database._conn.execute("PRAGMA writable_schema=ON")
    try:
        database._conn.execute(
            "INSERT INTO sqlite_master(type, name, tbl_name, rootpage, sql) "
            "VALUES ('table', 'sqlite_stat4', 'sqlite_stat4', 0, "
            "'CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)')"
        )
    finally:
        database._conn.execute("PRAGMA writable_schema=OFF")
    database._conn.execute(f"PRAGMA schema_version={schema_version + 1}")
    database._conn.commit()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="sqlite_stat1"):
            migration_v8.validate(database._conn)
    finally:
        database.close()


def test_default_literal_case_is_not_normalized_away(tmp_path: Path):
    database = Database(tmp_path / "default-case.db")
    database.init_schema()
    _rewrite_schema_sql(
        database._conn,
        "workers",
        lambda sql: sql.replace("DEFAULT 'offline'", "DEFAULT 'OFFLINE'", 1),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="workers.status"):
            migration_v8.validate(database._conn)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("object_name", "future_sql", "original", "tampered"),
    [
        (
            "literal_guard",
            "CREATE TABLE literal_guard(value TEXT CHECK(value='CaseSensitive'));",
            "'CaseSensitive'",
            "'casesensitive'",
        ),
        (
            "literal_index",
            "CREATE INDEX literal_index ON jobs(status) WHERE status='pending';",
            "'pending'",
            "'PENDING'",
        ),
        (
            "literal_view",
            "CREATE VIEW literal_view AS SELECT id FROM jobs "
            "WHERE status='pending';",
            "'pending'",
            "'PENDING'",
        ),
        (
            "literal_trigger",
            "CREATE TRIGGER literal_trigger BEFORE UPDATE ON jobs "
            "WHEN NEW.status='pending' BEGIN SELECT 1; END;",
            "'pending'",
            "'PENDING'",
        ),
    ],
    ids=["check", "index", "view", "trigger"],
)
def test_quoted_literal_case_drift_is_rejected_for_every_schema_object(
    tmp_path: Path,
    object_name: str,
    future_sql: str,
    original: str,
    tampered: str,
):
    database = Database(tmp_path / f"literal-{object_name}.db")
    database.init_schema()
    expected_schema = migration_v8.CURRENT_SCHEMA_SQL + "\n" + future_sql
    migration_v1._execute_sql_script(database._conn, future_sql)
    migration_v1._validate_complete_schema(database._conn, expected_schema)
    _rewrite_schema_sql(
        database._conn,
        object_name,
        lambda sql: sql.replace(original, tampered, 1),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            migration_v1._validate_complete_schema(
                database._conn,
                expected_schema,
            )
    finally:
        database.close()


def test_safety_backup_failure_prevents_any_migration(tmp_path: Path, monkeypatch):
    path = _load_fixture(tmp_path / "backup-fail.db", "v0001_pre_ledger.sql")
    database = Database(path)

    def fail_backup(_from_version: int) -> Path:
        raise OSError("快照目标不可写")

    monkeypatch.setattr(database, "_create_migration_backup", fail_backup)
    with pytest.raises(OSError, match="不可写"):
        database.init_schema()
    try:
        assert database.schema_version() == 1
        assert database._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is None
    finally:
        database.close()


def test_tampered_migration_ledger_is_fail_closed(tmp_path: Path):
    path = tmp_path / "tampered.db"
    database = Database(path)
    database.init_schema()
    database._conn.execute(
        "UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,)
    )
    database._conn.commit()
    database.close()

    reopened = Database(path)
    with pytest.raises(MigrationHistoryError, match="不一致"):
        reopened.init_schema()
    try:
        assert reopened.schema_version() == SCHEMA_VERSION
        assert reopened._conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=1"
        ).fetchone()[0] == "0" * 64
    finally:
        reopened.close()


def test_unreleased_v4_without_lifecycle_ledger_is_rejected_by_checksum(tmp_path: Path):
    path = tmp_path / "obsolete-v4.db"
    obsolete_checksum = (
        "ba0481e699176929f379fd2abef54911c90ee3e079ef999b165689de97851860"
    )
    database = Database(path)
    database.init_schema()
    database._conn.execute(
        "UPDATE schema_migrations SET checksum=? WHERE version=4",
        (obsolete_checksum,),
    )
    database._conn.commit()
    database.close()

    reopened = Database(path)
    with pytest.raises(MigrationHistoryError, match="不一致"):
        reopened.init_schema()
    try:
        assert reopened._conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=4"
        ).fetchone()[0] == obsolete_checksum
    finally:
        reopened.close()


def test_two_processes_serialize_backup_and_migration_from_v1(tmp_path: Path):
    path = _load_fixture(tmp_path / "concurrent.db", "v0001_pre_ledger.sql")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_initialize_in_process, args=(str(path), queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert not any(process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted(queue.get(timeout=2) for _ in processes) == [
        ("ok", SCHEMA_VERSION),
        ("ok", SCHEMA_VERSION),
    ]
    database = Database(path)
    try:
        assert database.schema_version() == SCHEMA_VERSION
        assert [row[0] for row in _ledger(database)] == list(
            range(1, SCHEMA_VERSION + 1)
        )
    finally:
        database.close()
    backups = list((tmp_path / "migration-backups").glob("*.db"))
    assert [backup.name for backup in backups] == ["concurrent.pre-v1-to-v8.db"]
    connection = sqlite3.connect(backups[0])
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT id FROM jobs WHERE id='legacy-v1-job'"
        ).fetchone() == ("legacy-v1-job",)
    finally:
        connection.close()
