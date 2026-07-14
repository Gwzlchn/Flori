"""jobs 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    Step,
    StepStatus,
    _STEP_UPDATABLE,
    _now_iso,
    datetime,
    json,
)

class JobsReadRepository:
    """只执行作业查询；事务和连接生命周期由 Database façade 持有。"""

    @staticmethod
    def get_job(database, job_id: str) -> Job | None:
        with database._lock:
            row = database._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return database._row_to_job(row)

    @staticmethod
    def jobs_brief(database, job_ids: list[str]) -> dict[str, dict]:
        ids = [job_id for job_id in dict.fromkeys(job_ids) if job_id]
        if not ids:
            return {}
        result: dict[str, dict] = {}
        with database._lock:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = database._conn.execute(
                    "SELECT id, title, content_type, domain, status, pipeline "
                    f"FROM jobs WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[row["id"]] = {
                        "title": row["title"],
                        "content_type": row["content_type"],
                        "domain": row["domain"],
                        "status": row["status"],
                        "pipeline": row["pipeline"],
                    }
        return result

    @staticmethod
    def list_jobs(
        database,
        status: str | None = None,
        collection_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        domain: str | None = None,
        source: str | None = None,
        uncategorized: bool = False,
        current_only: bool = True,
    ) -> tuple[int, list[Job]]:
        where_parts: list[str] = []
        params: list = []
        if current_only:
            where_parts.append("is_current=1")
        if status:
            where_parts.append("status=?")
            params.append(status)
        if uncategorized:
            where_parts.append("collection_id IS NULL")
        elif collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if source:
            where_parts.append("source=?")
            params.append(source)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with database._lock:
            total = database._conn.execute(
                f"SELECT COUNT(*) FROM jobs {where}", params
            ).fetchone()[0]
            rows = database._conn.execute(
                f"SELECT * FROM jobs {where} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return total, [database._row_to_job(row) for row in rows]

    @staticmethod
    def lineage_versions(database, job_id: str) -> list[Job]:
        with database._lock:
            row = database._conn.execute(
                "SELECT lineage_key FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return []
            lineage_key = row["lineage_key"]
            if not lineage_key:
                one = database._conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                return [database._row_to_job(one)] if one else []
            rows = database._conn.execute(
                "SELECT * FROM jobs WHERE lineage_key=? ORDER BY created_at DESC",
                (lineage_key,),
            ).fetchall()
        return [database._row_to_job(row) for row in rows]

    @staticmethod
    def lineage_counts(database, lineage_keys: list[str]) -> dict[str, int]:
        keys = [key for key in dict.fromkeys(lineage_keys) if key]
        if not keys:
            return {}
        result: dict[str, int] = {}
        with database._lock:
            for offset in range(0, len(keys), 500):
                chunk = keys[offset : offset + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = database._conn.execute(
                    "SELECT lineage_key, COUNT(*) AS n FROM jobs "
                    f"WHERE lineage_key IN ({placeholders}) GROUP BY lineage_key",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[row["lineage_key"]] = row["n"]
        return result

    @staticmethod
    def count_jobs_by_status(
        database, collection_id: str | None = None
    ) -> dict[str, int]:
        where = "WHERE collection_id=?" if collection_id else ""
        params = (collection_id,) if collection_id else ()
        with database._lock:
            rows = database._conn.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs {where} GROUP BY status",
                params,
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    @staticmethod
    def job_facets(database) -> dict[str, dict]:
        def group(column: str) -> dict:
            with database._lock:
                return {
                    row[0]: row[1]
                    for row in database._conn.execute(
                        f"SELECT {column}, COUNT(*) FROM jobs GROUP BY {column}"
                    ).fetchall()
                    if row[0] is not None
                }

        return {
            "source": group("source"),
            "domain": group("domain"),
            "status": group("status"),
        }

    @staticmethod
    def glossary_for_job(
        database, job_id: str, domain: str | None = None
    ) -> list[dict]:
        sql = (
            "SELECT * FROM glossary "
            "WHERE status != 'rejected' AND occurrences LIKE ?"
        )
        params: list = [f'%"{job_id}"%']
        if domain:
            sql += " AND domain=?"
            params.append(domain)
        result: list[dict] = []
        for row in database._conn.execute(sql, params):
            glossary = database._row_to_glossary(row)
            occurrences = glossary.get("occurrences") or []
            hits = [
                occurrence
                for occurrence in occurrences
                if isinstance(occurrence, dict)
                and occurrence.get("job_id") == job_id
            ]
            if hits:
                glossary["job_occurrences"] = hits
                result.append(glossary)
        return result


class JobsRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def get_steps(self, job_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE job_id=? ORDER BY step", (job_id,)
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def _strip_occurrences_for_jobs_in_tx(self, connection, job_ids: list[str]) -> None:
        """从 glossary.occurrences 摘除指向这些 job 的出现(保留概念与定义)。
        调用方须已持锁且在同一事务内;本方法只 execute,不 commit。"""
        for job_id in job_ids:
            # glossary.occurrences=[{job_id,...}],摘掉指向已删 job 的出现。
            rows = connection.execute(
                "SELECT domain, term, occurrences FROM glossary WHERE occurrences LIKE ?",
                (f'%"{job_id}"%',),
            ).fetchall()
            for r in rows:
                try:
                    occs = json.loads(r["occurrences"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
                kept = [o for o in occs if o.get("job_id") != job_id]
                if len(kept) != len(occs):
                    connection.execute(
                        "UPDATE glossary SET occurrences=? WHERE domain=? AND term=?",
                        (json.dumps(kept, ensure_ascii=False), r["domain"], r["term"]),
                    )

    def _detach_study_sources_locked_in_tx(self, connection, job_ids: list[str]) -> None:
        """删源前保留学习审计事实,调用方负责事务和锁."""
        if not job_ids:
            return
        now = _db._now_iso()
        for job_id in job_ids:
            connection.execute(
                """UPDATE study_suggestion_evidence
                   SET status='unavailable', invalid_reason='job_deleted',
                       validated_at=?
                   WHERE job_id=?""",
                (now, job_id),
            )
            connection.execute(
                "UPDATE study_cards SET job_id=NULL, updated_at=? WHERE job_id=?",
                (now, job_id),
            )

    def upsert_step_in_tx(self, connection, step: Step) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO job_steps
               (job_id, step, status, pool, input_hash, worker_id,
                started_at, finished_at, duration_sec, meta, error, retries)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                step.job_id,
                step.name,
                step.status.value if isinstance(step.status, StepStatus) else step.status,
                step.pool,
                step.input_hash,
                step.worker_id,
                step.started_at.isoformat() if step.started_at else None,
                step.finished_at.isoformat() if step.finished_at else None,
                step.duration_sec,
                json.dumps(step.meta, ensure_ascii=False) if step.meta else None,
                step.error,
                step.retries,
            ),
        )

    def delete_step_in_tx(self, connection, job_id: str, step_name: str) -> None:
        """删单个步骤行(供 resubmit 对齐:删去当前 pipeline 不再有的步,避免 DB 残留旧步)。"""
        connection.execute(
            "DELETE FROM job_steps WHERE job_id=? AND step=?", (job_id, step_name)
        )

    def update_step_in_tx(
        self, connection, job_id: str, step_name: str, *, only_if_active: bool = False, **fields
    ) -> None:
        """更新步骤行。only_if_active=True 时仅在当前状态非终态(done/skipped)才写,
        防成功步被迟到的失败上报覆盖(done→failed 不一致)。"""
        if not fields:
            return
        invalid = set(fields.keys()) - _STEP_UPDATABLE
        if invalid:
            raise ValueError(f"Invalid step columns: {invalid}")
        if "status" in fields and isinstance(fields["status"], StepStatus):
            fields["status"] = fields["status"].value
        if "meta" in fields and isinstance(fields["meta"], dict):
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        if "started_at" in fields and isinstance(fields["started_at"], datetime):
            fields["started_at"] = fields["started_at"].isoformat()
        if "finished_at" in fields and isinstance(fields["finished_at"], datetime):
            fields["finished_at"] = fields["finished_at"].isoformat()

        set_clause = ", ".join(f"{k}=?" for k in fields)
        where = "job_id=? AND step=?"
        values = list(fields.values()) + [job_id, step_name]
        if only_if_active:
            where += " AND status NOT IN ('done','skipped')"
        connection.execute(
            f"UPDATE job_steps SET {set_clause} WHERE {where}",
            values,
        )
