"""把视频 Job 原子迁移为显式 Part 与 scoped step 模型。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import (
    v0001_legacy_baseline,
    v0006_concept_definition_history,
    v0007_unified_document,
)
VERSION = 8
NAME = "multipart-video-jobs"

PART_STEPS = {
    "01_download", "02_whisper", "03_scene", "04_frames",
    "05_dedup", "06_ocr", "07_danmaku", "08_punctuate",
}
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _stable_part_id(job_id: str, part_index: int) -> str:
    """迁移必须能被DR作为独立冻结包加载,不能依赖shared父包。"""
    if not _SEGMENT_RE.fullmatch(job_id) or part_index < 1:
        raise ValueError("invalid job part identity")
    digest = hashlib.sha256(f"{job_id}:part:{part_index}".encode()).hexdigest()[:20]
    return f"pt_{digest}"

MULTIPART_SCHEMA_SQL = """
ALTER TABLE job_steps RENAME TO job_steps_v7;

CREATE TABLE job_parts (
    id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL CHECK (part_index >= 1),
    title TEXT,
    source_url TEXT,
    source_ref TEXT,
    source_digest TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, id),
    UNIQUE (job_id, part_index)
);
CREATE INDEX idx_job_parts_job ON job_parts(job_id, part_index);

CREATE TABLE job_steps (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL DEFAULT 'job',
    step TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    pool TEXT NOT NULL DEFAULT '',
    input_hash TEXT,
    worker_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_sec REAL,
    meta TEXT,
    error TEXT,
    retries INTEGER DEFAULT 0,
    PRIMARY KEY (job_id, scope_key, step)
);

CREATE TRIGGER trg_job_steps_part_scope_insert
BEFORE INSERT ON job_steps
WHEN NEW.scope_key != 'job'
BEGIN
    SELECT CASE WHEN
        substr(NEW.scope_key, 1, 5) != 'part:'
        OR NOT EXISTS (
            SELECT 1 FROM job_parts
            WHERE id=substr(NEW.scope_key, 6) AND job_id=NEW.job_id
        )
    THEN RAISE(ABORT, 'job step part scope invariant failed') END;
END;

CREATE TRIGGER trg_job_steps_part_scope_update
BEFORE UPDATE OF job_id, scope_key ON job_steps
WHEN NEW.scope_key != 'job'
BEGIN
    SELECT CASE WHEN
        substr(NEW.scope_key, 1, 5) != 'part:'
        OR NOT EXISTS (
            SELECT 1 FROM job_parts
            WHERE id=substr(NEW.scope_key, 6) AND job_id=NEW.job_id
        )
    THEN RAISE(ABORT, 'job step part scope invariant failed') END;
END;
""".strip()

DROP_OLD_STEPS_SQL = "DROP TABLE job_steps_v7;"

CURRENT_SCHEMA_SQL = (
    v0007_unified_document.CURRENT_SCHEMA_SQL
    + "\n\n"
    + MULTIPART_SCHEMA_SQL
    + "\n\n"
    + DROP_OLD_STEPS_SQL
)


def source_payload() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def apply(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._execute_sql_script(connection, MULTIPART_SCHEMA_SQL)
    video_rows = connection.execute(
        """SELECT id, url, source_digest, created_at, updated_at
           FROM jobs WHERE content_type='video' ORDER BY id"""
    ).fetchall()
    part_by_job: dict[str, str] = {}
    for row in video_rows:
        part_id = _stable_part_id(str(row["id"]), 1)
        part_by_job[str(row["id"])] = part_id
        connection.execute(
            """INSERT INTO job_parts
               (id, job_id, part_index, title, source_url, source_digest,
                meta, created_at, updated_at)
               VALUES (?,?,1,NULL,?,?,'{}',?,?)""",
            (
                part_id, row["id"], row["url"], row["source_digest"],
                row["created_at"], row["updated_at"],
            ),
        )
        manifest = json.dumps(
            [{"part_index": 1, "url": row["url"], "title": None}],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        connection.execute(
            "UPDATE jobs SET url=NULL, source_digest=? WHERE id=?",
            (f"sha256:{hashlib.sha256(manifest).hexdigest()}", row["id"]),
        )
    rows = connection.execute("SELECT * FROM job_steps_v7 ORDER BY job_id, step").fetchall()
    for row in rows:
        part_id = part_by_job.get(str(row["job_id"]))
        scope_key = (
            f"part:{part_id}" if part_id is not None and row["step"] in PART_STEPS
            else "job"
        )
        connection.execute(
            """INSERT INTO job_steps
               (job_id, scope_key, step, status, pool, input_hash, worker_id,
                started_at, finished_at, duration_sec, meta, error, retries)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["job_id"], scope_key, row["step"], row["status"],
                row["pool"], row["input_hash"], row["worker_id"],
                row["started_at"], row["finished_at"], row["duration_sec"],
                row["meta"], row["error"], row["retries"],
            ),
        )
    for job_id, part_id in part_by_job.items():
        connection.execute(
            """UPDATE ai_usage
               SET step='part:' || ? || '::' || step
               WHERE job_id=? AND step IN (
                   '01_download','02_whisper','03_scene','04_frames',
                   '05_dedup','06_ocr','07_danmaku','08_punctuate'
               )""",
            (part_id, job_id),
        )
    for job_id in part_by_job:
        downstream = connection.execute(
            """SELECT status, started_at, finished_at, duration_sec
               FROM job_steps WHERE job_id=? AND scope_key='job'
                 AND step='09_mechanical'""",
            (job_id,),
        ).fetchone()
        merge_status = (
            downstream["status"]
            if downstream is not None and downstream["status"] in {"done", "skipped"}
            else "waiting"
        )
        connection.execute(
            """INSERT INTO job_steps
               (job_id, scope_key, step, status, pool, started_at, finished_at,
                duration_sec, retries)
               VALUES (?, 'job', '09_merge_parts', ?, 'io', ?, ?, ?, 0)""",
            (
                job_id,
                merge_status,
                downstream["started_at"] if merge_status != "waiting" else None,
                downstream["finished_at"] if merge_status != "waiting" else None,
                downstream["duration_sec"] if merge_status != "waiting" else None,
            ),
        )
    connection.execute(DROP_OLD_STEPS_SQL)


def validate(connection: sqlite3.Connection) -> None:
    v0001_legacy_baseline._validate_complete_schema(connection, CURRENT_SCHEMA_SQL)
    v0006_concept_definition_history._replay_frozen_validator(
        connection, v0007_unified_document.validate,
    )
    missing = connection.execute(
        """SELECT id FROM jobs
           WHERE content_type='video'
             AND NOT EXISTS (SELECT 1 FROM job_parts WHERE job_id=jobs.id)
           LIMIT 1"""
    ).fetchone()
    if missing is not None:
        raise sqlite3.DatabaseError(f"video job missing part: {missing[0]}")
    stray = connection.execute(
        """SELECT job_id FROM job_parts
           WHERE job_id IN (SELECT id FROM jobs WHERE content_type!='video')
           LIMIT 1"""
    ).fetchone()
    if stray is not None:
        raise sqlite3.DatabaseError(f"non-video job has part: {stray[0]}")
    discontinuous = connection.execute(
        """SELECT job_id FROM job_parts GROUP BY job_id
           HAVING MIN(part_index) != 1 OR MAX(part_index) != COUNT(*)
           LIMIT 1"""
    ).fetchone()
    if discontinuous is not None:
        raise sqlite3.DatabaseError(
            f"job parts are not contiguous: {discontinuous[0]}"
        )
    invalid = connection.execute(
        """SELECT job_id, scope_key FROM job_steps
           WHERE scope_key != 'job'
             AND NOT EXISTS (
                 SELECT 1 FROM job_parts
                 WHERE id=substr(job_steps.scope_key, 6)
                   AND job_id=job_steps.job_id
             )
           LIMIT 1"""
    ).fetchone()
    if invalid is not None:
        raise sqlite3.DatabaseError(
            f"job step has invalid part scope: {invalid[0]}/{invalid[1]}"
        )
