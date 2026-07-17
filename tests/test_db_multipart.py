from datetime import datetime, timezone

import pytest

from shared.db import Database
from shared.models import Job, JobPart, Step
from shared.step_scope import part_scope, stable_part_id


def test_v7_video_job_migrates_to_stable_part_and_scoped_steps(tmp_path) -> None:
    database = Database(tmp_path / "legacy-video.db")
    database._conn.execute("PRAGMA foreign_keys=ON")
    from shared.migrations import run_migrations

    run_migrations(database._conn, database._migration_steps(), target_version=7)
    database._conn.execute(
        """INSERT INTO jobs
           (id,content_type,pipeline,document_kind,url,title,domain,status,
            created_at,updated_at)
           VALUES ('job_video_1','video','video','','https://example.test/p1',
                   'legacy','finance','failed','2026-01-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00')"""
    )
    database._conn.executemany(
        """INSERT INTO job_steps(job_id,step,status,pool)
           VALUES ('job_video_1',?,?,?)""",
        [
            ("01_download", "done", "io"),
            ("08_punctuate", "done", "ai"),
            ("09_mechanical", "done", "io"),
        ],
    )
    database._conn.execute(
        """INSERT INTO ai_usage
           (exec_id,job_id,step,provider,model,created_at)
           VALUES ('exec_part','job_video_1','08_punctuate','test','test',
                   '2026-01-01T00:00:00+00:00')""",
    )
    database._conn.commit()

    database.init_schema()
    parts = database.get_parts("job_video_1")
    assert [part.id for part in parts] == [stable_part_id("job_video_1", 1)]
    steps = {(step.scope_key, step.name): step.status.value for step in database.get_steps("job_video_1")}
    scope = part_scope(parts[0].id)
    assert steps[(scope, "01_download")] == "done"
    assert steps[(scope, "08_punctuate")] == "done"
    assert steps[("job", "09_merge_parts")] == "done"
    assert steps[("job", "09_mechanical")] == "done"
    assert database._conn.execute(
        "SELECT step FROM ai_usage WHERE exec_id='exec_part'",
    ).fetchone()[0] == f"part:{parts[0].id}::08_punctuate"
    database.close()


def test_job_parts_and_same_named_steps_are_atomic_and_isolated(tmp_path) -> None:
    database = Database(tmp_path / "multipart.db")
    database.init_schema()
    now = datetime.now(timezone.utc)
    job = Job(id="job_video_2", content_type="video", pipeline="video")
    parts = [
        JobPart("pt_a", job.id, 1, source_url="https://example.test/a", created_at=now, updated_at=now),
        JobPart("pt_b", job.id, 2, source_url="https://example.test/b", created_at=now, updated_at=now),
    ]
    database.create_job(job, parts)
    database.upsert_step(Step(job.id, "02_whisper", scope_key=part_scope("pt_a")))
    database.upsert_step(Step(job.id, "02_whisper", scope_key=part_scope("pt_b")))

    assert [part.id for part in database.get_parts(job.id)] == ["pt_a", "pt_b"]
    assert {(step.scope_key, step.name) for step in database.get_steps(job.id)} == {
        ("part:pt_a", "02_whisper"),
        ("part:pt_b", "02_whisper"),
    }
    database.close()


def test_job_creation_rejects_empty_or_non_video_parts(tmp_path) -> None:
    database = Database(tmp_path / "parts-invariant.db")
    database.init_schema()
    video = Job(id="job_video_empty", content_type="video", pipeline="video")
    with pytest.raises(ValueError, match="at least one part"):
        database.create_job(video, [])
    too_many = Job(id="job_too_many_parts", content_type="video", pipeline="video")
    with pytest.raises(ValueError, match="between 1 and 128"):
        database.create_job(too_many, [
            JobPart(f"pt_{index}", too_many.id, index)
            for index in range(1, 130)
        ])
    document = Job(id="job_document", content_type="document", pipeline="document")
    part = JobPart("pt_a", document.id, 1, source_url="https://example.test/a")
    with pytest.raises(ValueError, match="only video"):
        database.create_job(document, [part])
    invalid = Job(id="job_invalid_part", content_type="video", pipeline="video")
    with pytest.raises(ValueError, match="invalid part_id"):
        database.create_job(invalid, [
            JobPart("../escape", invalid.id, 1, source_url="https://example.test/a"),
        ])
    database.close()
