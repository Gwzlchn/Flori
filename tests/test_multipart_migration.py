"""多Part对象与SQLite离线切换测试。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from shared.db import Database
from shared.migrations import migration_steps, run_migrations
from shared.multipart_migration import (
    LocalObjectStore,
    MultipartMigrationError,
    ObjectStat,
    commit,
    is_part_artifact,
    stage,
    verify,
)
from shared.step_scope import stable_part_id


def _v7_video_database(path) -> None:
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
    connection.executemany(
        """INSERT INTO job_steps(job_id,step,status,pool)
           VALUES ('job_video',?,?,?)""",
        [
            ("01_download", "done", "io"),
            ("08_punctuate", "done", "ai"),
            ("09_mechanical", "done", "io"),
        ],
    )
    connection.commit()
    connection.close()


def _legacy_objects(jobs_dir) -> None:
    root = jobs_dir / "job_video"
    files = {
        "job.json": {
            "id": "job_video",
            "url": "https://example.test/p1",
            "source": "http",
            "content_type": "video",
            "domain": "finance",
            "style_tags": [],
            "flags": {"smart_note": True},
        },
        "input/source.mp4": b"video",
        "assets/frame-0001.jpg": b"frame",
        "intermediate/ocr.json": b"{}",
        "output/transcript.md": b"transcript",
        "output/notes_mechanical.md": b"job note",
        ".08_punctuate.done": b"{}",
        "logs/08_punctuate.log": b"ok",
    }
    for rel, value in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            json.dumps(value).encode("utf-8") if isinstance(value, dict) else value
        )


def test_part_artifact_classifier_keeps_job_level_outputs_at_root() -> None:
    assert is_part_artifact("input/source.mp4")
    assert is_part_artifact("assets/frame-0001.jpg")
    assert is_part_artifact(".08_punctuate.done")
    assert is_part_artifact("logs/.08_punctuate.usage.json")
    assert is_part_artifact("output/ai_logs/08_punctuate.jsonl")
    assert not is_part_artifact("job.json")
    assert not is_part_artifact("output/notes_mechanical.md")
    assert not is_part_artifact("output/review.json")
    assert not is_part_artifact(".09_mechanical.done")


@pytest.mark.asyncio
async def test_stage_commit_and_verify_preserve_legacy_objects(tmp_path) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)
    store = LocalObjectStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"

    staged = stage(db_path, store, journal_path)

    part_id = stable_part_id("job_video", 1)
    assert staged["state"] == "staged"
    assert (jobs_dir / "job_video/input/source.mp4").read_bytes() == b"video"
    assert (jobs_dir / f"job_video/parts/{part_id}/input/source.mp4").read_bytes() == b"video"
    assert not (jobs_dir / f"job_video/parts/{part_id}/output/notes_mechanical.md").exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7

    await commit(db_path, store, journal_path, redis_url=None)
    result = verify(db_path, store, journal_path)

    assert result["schema_version"] == 8
    assert result["video_jobs"] == result["parts"] == 1
    root_doc = json.loads((jobs_dir / "job_video/job.json").read_text())
    assert root_doc["url"] is None
    assert root_doc["parts"][0]["part_id"] == part_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT url FROM jobs WHERE id='job_video'",
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT source_url FROM job_parts WHERE job_id='job_video'",
        ).fetchone()[0] == "https://example.test/p1"
    assert list((db_path.parent / "migration-backups").glob("*.pre-v7-to-v8.db"))
    marker = json.loads((db_path.parent / "multipart-v8.ready.json").read_text())
    assert marker["state"] == "verified"


@pytest.mark.asyncio
async def test_stage_accepts_distinct_server_side_copy_token(tmp_path) -> None:
    """MinIO复制大对象会重算ETag,迁移应分别冻结源和目标身份。"""
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)

    class DistinctCopyTokenStore:
        def __init__(self, root):
            self.local = LocalObjectStore(root)
            self.target_stats = {}

        def list_job(self, job_id):
            return self.local.list_job(job_id)

        def stat(self, job_id, rel_path):
            return self.target_stats.get((job_id, rel_path)) or self.local.stat(
                job_id, rel_path,
            )

        def copy(self, job_id, src_rel, dst_rel):
            source, target = self.local.copy(job_id, src_rel, dst_rel)
            target = ObjectStat(target.size, f"copy:{target.token}")
            self.target_stats[(job_id, dst_rel)] = target
            return source, target

        def read(self, job_id, rel_path):
            return self.local.read(job_id, rel_path)

        def write(self, job_id, rel_path, data):
            self.local.write(job_id, rel_path, data)

    store = DistinctCopyTokenStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"
    staged = stage(db_path, store, journal_path)
    first = staged["jobs"]["job_video"]["objects"][0]
    assert first["source"]["token"] != first["target"]["token"]

    await commit(db_path, store, journal_path, redis_url=None)
    assert verify(db_path, store, journal_path)["verified_objects"] > 0


def test_stage_copy_failure_never_switches_database_or_root_manifest(tmp_path) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)

    class FailingStore(LocalObjectStore):
        calls = 0

        def copy(self, job_id, src_rel, dst_rel):
            self.calls += 1
            if self.calls == 2:
                raise MultipartMigrationError("injected copy failure")
            return super().copy(job_id, src_rel, dst_rel)

    original = (jobs_dir / "job_video" / "job.json").read_bytes()
    with pytest.raises(MultipartMigrationError, match="injected copy failure"):
        stage(
            db_path,
            FailingStore(jobs_dir),
            db_path.parent / "multipart-v8-journal.json",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    assert (jobs_dir / "job_video" / "job.json").read_bytes() == original
    assert not (db_path.parent / "multipart-v8.ready.json").exists()


@pytest.mark.asyncio
async def test_commit_database_failure_restores_root_manifest(
    tmp_path, monkeypatch,
) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)
    store = LocalObjectStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"
    original = (jobs_dir / "job_video" / "job.json").read_bytes()
    stage(db_path, store, journal_path)

    def fail_init(self):
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(Database, "init_schema", fail_init)
    with pytest.raises(RuntimeError, match="injected database failure"):
        await commit(db_path, store, journal_path, redis_url=None)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    assert (jobs_dir / "job_video" / "job.json").read_bytes() == original


@pytest.mark.asyncio
async def test_commit_rejects_database_changes_after_stage(tmp_path) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)
    store = LocalObjectStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"
    stage(db_path, store, journal_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET title='changed after stage' WHERE id='job_video'",
        )
        connection.commit()

    with pytest.raises(MultipartMigrationError, match="database changed after object stage"):
        await commit(db_path, store, journal_path, redis_url=None)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


@pytest.mark.asyncio
async def test_commit_rejects_object_changes_after_stage(tmp_path) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)
    store = LocalObjectStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"
    stage(db_path, store, journal_path)
    (jobs_dir / "job_video/input/source.mp4").write_bytes(b"changed")

    with pytest.raises(MultipartMigrationError, match="staged object changed before commit"):
        await commit(db_path, store, journal_path, redis_url=None)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


@pytest.mark.asyncio
async def test_commit_rejects_tampered_staged_journal(tmp_path) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)
    store = LocalObjectStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"
    stage(db_path, store, journal_path)
    journal = json.loads(journal_path.read_text())
    journal["jobs"]["job_video"]["bytes"] += 1
    journal_path.write_text(json.dumps(journal))

    with pytest.raises(MultipartMigrationError, match="journal checksum mismatch"):
        await commit(db_path, store, journal_path, redis_url=None)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


@pytest.mark.asyncio
async def test_stage_commit_verify_are_restart_safe(tmp_path) -> None:
    db_path = tmp_path / "db" / "analyzer.db"
    db_path.parent.mkdir()
    jobs_dir = tmp_path / "jobs"
    _v7_video_database(db_path)
    _legacy_objects(jobs_dir)
    store = LocalObjectStore(jobs_dir)
    journal_path = db_path.parent / "multipart-v8-journal.json"

    assert stage(db_path, store, journal_path)["state"] == "staged"
    assert stage(db_path, store, journal_path)["state"] == "staged"
    assert (await commit(db_path, store, journal_path, redis_url=None))["state"] == "committed"
    assert (await commit(db_path, store, journal_path, redis_url=None))["state"] == "committed"
    assert verify(db_path, store, journal_path)["video_jobs"] == 1
    assert verify(db_path, store, journal_path)["video_jobs"] == 1


def test_production_gate_rejects_v7_video_database_without_stage_marker(
    tmp_path, monkeypatch,
) -> None:
    db_path = tmp_path / "analyzer.db"
    _v7_video_database(db_path)
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    with pytest.raises(RuntimeError, match="object stage is required"):
        database.init_schema()
    assert database.schema_version() == 7
    database.close()


def test_production_gate_rejects_interrupted_v8_commit(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "analyzer.db"
    _v7_video_database(db_path)
    jobs_dir = tmp_path / "jobs"
    _legacy_objects(jobs_dir)
    journal_path = tmp_path / "multipart-v8-journal.json"
    stage(db_path, LocalObjectStore(jobs_dir), journal_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        run_migrations(connection, migration_steps(), target_version=8)
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    with pytest.raises(RuntimeError, match="database commit is incomplete"):
        database.init_schema()
    database.close()


def test_production_gate_rejects_unknown_v8_marker_state(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "analyzer.db"
    _v7_video_database(db_path)
    jobs_dir = tmp_path / "jobs"
    _legacy_objects(jobs_dir)
    journal_path = tmp_path / "multipart-v8-journal.json"
    stage(db_path, LocalObjectStore(jobs_dir), journal_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        run_migrations(connection, migration_steps(), target_version=8)
    marker_path = tmp_path / "multipart-v8.ready.json"
    marker = json.loads(marker_path.read_text())
    marker["state"] = "unexpected"
    marker_path.write_text(json.dumps(marker))
    monkeypatch.setenv("FLORI_REQUIRE_OFFLINE_MIGRATIONS", "1")

    database = Database(db_path)
    with pytest.raises(RuntimeError, match="database commit is incomplete"):
        database.init_schema()
    database.close()
