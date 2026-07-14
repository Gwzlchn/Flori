"""SQLite 行到领域模型的显式映射。"""

from __future__ import annotations

import json
import sqlite3

from ..models import Job, JobStatus


class DatabaseRowMappers:
    @staticmethod
    def job(row: sqlite3.Row, *, parse_datetime) -> Job:
        return Job(
            id=row["id"],
            content_type=row["content_type"],
            pipeline=row["pipeline"],
            collection_id=row["collection_id"],
            url=row["url"],
            title=row["title"],
            domain=row["domain"],
            source=row["source"],
            style_tags=json.loads(row["style_tags"]),
            status=JobStatus(row["status"]),
            progress_pct=row["progress_pct"],
            meta=json.loads(row["meta"]),
            published_at=parse_datetime(row["published_at"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
            error=row["error"],
            lineage_key=row["lineage_key"],
            is_current=bool(row["is_current"]),
            source_digest=row["source_digest"],
            pipeline_digest=row["pipeline_digest"],
            parent_job_id=row["parent_job_id"],
        )
