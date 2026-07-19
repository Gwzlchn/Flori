"""v7 Video 库的离线迁移启动门。

多 Part 一次性迁移工具已退役,但启动门仍是生产代码:它拒绝让停在 v7 且仍有 Video
的旧库只迁数据库就起服务。marker 在这里直接构造,不依赖已删的工具。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from shared.db import Database
from shared.migrations import migration_steps, run_migrations


MARKER_NAME = "multipart-v8.ready.json"


def _video_database(path, *, target_version: int) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    run_migrations(connection, migration_steps(), target_version=7)
    connection.execute(
        """INSERT INTO jobs
           (id,content_type,pipeline,document_kind,url,title,source,domain,status,
            style_tags,meta,created_at,updated_at,source_digest)
           VALUES ('job_video','video','video','','https://example.test/p1',
                   'legacy','http','finance','failed','[]','{}',
                   '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',
                   'sha256:legacy')""",
    )
    connection.commit()
    if target_version > 7:
        run_migrations(connection, migration_steps(), target_version=target_version)
    connection.close()


def _write_marker(tmp_path, state: str) -> None:
    (tmp_path / MARKER_NAME).write_text(
        json.dumps({"state": state}), encoding="utf-8"
    )


def test_gate_rejects_v7_video_database_without_stage_marker(
    tmp_path, monkeypatch,
) -> None:
    db_path = tmp_path / "analyzer.db"
    _video_database(db_path, target_version=7)
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    with pytest.raises(RuntimeError, match="object stage is required"):
        database.init_schema()
    assert database.schema_version() == 7
    database.close()


@pytest.mark.parametrize("state", ["staged", "unexpected"])
def test_gate_rejects_v8_database_whose_marker_never_reached_commit(
    tmp_path, monkeypatch, state: str,
) -> None:
    # 数据库已切到 v8 但 marker 停在非终态 = 迁移中断,带旧 Redis 状态接单会错乱.
    db_path = tmp_path / "analyzer.db"
    _video_database(db_path, target_version=8)
    _write_marker(tmp_path, state)
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    with pytest.raises(RuntimeError, match="database commit is incomplete"):
        database.init_schema()
    database.close()


def test_gate_rejects_unreadable_marker(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "analyzer.db"
    _video_database(db_path, target_version=8)
    (tmp_path / MARKER_NAME).write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    with pytest.raises(RuntimeError, match="marker is unreadable"):
        database.init_schema()
    database.close()


@pytest.mark.parametrize("state", ["committed", "verified"])
def test_gate_lets_completed_migration_continue_upgrading(
    tmp_path, monkeypatch, state: str,
) -> None:
    db_path = tmp_path / "analyzer.db"
    _video_database(db_path, target_version=8)
    _write_marker(tmp_path, state)
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    database.init_schema()
    assert database.schema_version() >= 8
    database.close()


def test_gate_is_off_without_the_production_flag(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "analyzer.db"
    _video_database(db_path, target_version=7)
    monkeypatch.delenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", raising=False)

    database = Database(db_path)
    database.init_schema()
    assert database.schema_version() >= 8
    database.close()
