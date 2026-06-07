"""SQLite 数据库层。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import AIUsage, Collection, Job, JobStatus, Step, StepStatus, Worker
from .status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    STALE,
    compute_worker_status,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    content_type TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    collection_id TEXT,
    url TEXT,
    title TEXT,
    domain TEXT NOT NULL DEFAULT 'general',
    source TEXT,
    style_tags TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    progress_pct INTEGER DEFAULT 0,
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_collection ON jobs(collection_id);

CREATE TABLE IF NOT EXISTS job_steps (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
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
    PRIMARY KEY (job_id, step)
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    pools TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    reject_tags TEXT NOT NULL DEFAULT '[]',
    hostname TEXT,
    gpu_name TEXT,
    gpu_memory_mb INTEGER,
    status TEXT NOT NULL DEFAULT 'offline',
    current_job TEXT,
    current_step TEXT,
    tasks_completed INTEGER DEFAULT 0,
    tasks_failed INTEGER DEFAULT 0,
    total_duration_sec REAL DEFAULT 0,
    first_seen TEXT NOT NULL,
    started_at TEXT,
    last_heartbeat TEXT,
    admin_note TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id TEXT NOT NULL UNIQUE,
    job_id TEXT,
    step TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    cached INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_job ON ai_usage(job_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_provider ON ai_usage(provider);

CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    job_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


_JOB_UPDATABLE = {
    "status", "title", "progress_pct", "error", "updated_at",
    "meta", "style_tags", "domain", "source", "collection_id",
}
_STEP_UPDATABLE = {
    "status", "input_hash", "worker_id", "started_at", "finished_at",
    "duration_sec", "meta", "error", "retries",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    """解析 ISO 时间串为 aware-UTC。旧库里存的 naive 串补上 UTC tzinfo，
    避免与 aware 的 now() 相减时崩 'can't subtract offset-naive and offset-aware'。"""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        # 持锁关闭，确保没有线程正在使用连接。
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── Job ──

    def create_job(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs
                   (id, content_type, pipeline, collection_id, url, title,
                    domain, source, style_tags, status, progress_pct, meta,
                    created_at, updated_at, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.id,
                    job.content_type,
                    job.pipeline,
                    job.collection_id,
                    job.url,
                    job.title,
                    job.domain,
                    job.source,
                    json.dumps(job.style_tags, ensure_ascii=False),
                    job.status.value if isinstance(job.status, JobStatus) else job.status,
                    job.progress_pct,
                    json.dumps(job.meta, ensure_ascii=False),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.error,
                ),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(
        self,
        status: str | None = None,
        collection_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[Job]]:
        where_parts: list[str] = []
        params: list = []
        if status:
            where_parts.append("status=?")
            params.append(status)
        if collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM jobs {where}", params
        ).fetchone()[0]

        rows = self._conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        return total, [self._row_to_job(r) for r in rows]

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        invalid = set(fields.keys()) - _JOB_UPDATABLE
        if invalid:
            raise ValueError(f"Invalid job columns: {invalid}")
        fields["updated_at"] = _now_iso()
        if "style_tags" in fields:
            fields["style_tags"] = json.dumps(fields["style_tags"], ensure_ascii=False)
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        if "status" in fields and isinstance(fields["status"], JobStatus):
            fields["status"] = fields["status"].value

        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [job_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {set_clause} WHERE id=?", values
            )
            self._conn.commit()

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()

    # ── Step ──

    def upsert_step(self, step: Step) -> None:
        with self._lock:
            self._conn.execute(
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
            self._conn.commit()

    def get_steps(self, job_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE job_id=? ORDER BY step", (job_id,)
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def update_step(self, job_id: str, step_name: str, **fields) -> None:
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
        values = list(fields.values()) + [job_id, step_name]
        with self._lock:
            self._conn.execute(
                f"UPDATE job_steps SET {set_clause} WHERE job_id=? AND step=?",
                values,
            )
            self._conn.commit()

    # ── Worker ──

    def upsert_worker(self, worker: Worker) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO workers
                   (id, type, pools, tags, reject_tags, hostname, gpu_name,
                    gpu_memory_mb, status, current_job, current_step,
                    tasks_completed, tasks_failed, total_duration_sec,
                    first_seen, started_at, last_heartbeat, admin_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    worker.id,
                    worker.type,
                    json.dumps(worker.pools),
                    json.dumps(sorted(worker.tags)),
                    json.dumps(sorted(worker.reject_tags)),
                    worker.hostname,
                    worker.gpu_name,
                    worker.gpu_memory_mb,
                    worker.status,
                    worker.current_job,
                    worker.current_step,
                    worker.tasks_completed,
                    worker.tasks_failed,
                    worker.total_duration_sec,
                    worker.first_seen.isoformat(),
                    worker.started_at.isoformat() if worker.started_at else None,
                    worker.last_heartbeat.isoformat() if worker.last_heartbeat else None,
                    worker.admin_note,
                ),
            )
            self._conn.commit()

    def get_worker(
        self,
        worker_id: str,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if row is None:
            return None
        w = self._row_to_worker(row)
        self._apply_status(w, online_window_sec, stale_window_sec)
        return w

    def list_workers(
        self,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> list[Worker]:
        """列出所有 worker，状态由后端按心跳新鲜度统一算出（online-idle/busy、
        offline、stale，draining 为管理员叠加）。越过 stale 窗口的持久化为信号，
        供 GC 回收僵尸 worker。"""
        rows = self._conn.execute("SELECT * FROM workers").fetchall()
        workers = [self._row_to_worker(r) for r in rows]
        now = datetime.now(timezone.utc)
        for w in workers:
            self._apply_status(w, online_window_sec, stale_window_sec, now=now)
        return workers

    def _apply_status(
        self,
        w: Worker,
        online_window_sec: int,
        stale_window_sec: int,
        now: datetime | None = None,
    ) -> None:
        """把 worker 的存量字段折算成对外公共状态，并对 stale 持久化（不动心跳）。
        存量 status 列只作管理员叠加位(draining)的来源。"""
        public = compute_worker_status(
            last_heartbeat=w.last_heartbeat,
            current_job=w.current_job,
            admin_status=w.status,
            now=now,
            online_window_sec=online_window_sec,
            stale_window_sec=stale_window_sec,
        )
        if public == STALE and w.status != STALE:
            self.set_worker_status(w.id, STALE)
        w.status = public

    def set_worker_status(self, worker_id: str, status: str) -> None:
        """仅更新 worker 状态，不触碰 last_heartbeat（用于标记僵尸为 offline）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET status=? WHERE id=?", (status, worker_id),
            )
            self._conn.commit()

    def increment_worker_stats(
        self,
        worker_id: str,
        completed: int = 0,
        failed: int = 0,
        duration: float = 0.0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE workers SET
                   tasks_completed = tasks_completed + ?,
                   tasks_failed = tasks_failed + ?,
                   total_duration_sec = total_duration_sec + ?
                   WHERE id=?""",
                (completed, failed, duration, worker_id),
            )
            self._conn.commit()

    def update_worker_heartbeat(
        self,
        worker_id: str,
        status: str | None = None,
        current_job: str | None = None,
        current_step: str | None = None,
    ) -> None:
        """刷新 worker 在 DB 中的 last_heartbeat（及可选的 status / 当前任务）。

        心跳与状态变更必须写回 DB，否则 /api/workers 读到的 last_heartbeat
        永远停在注册时刻，前端会在 30s 后把所有 worker 判成 offline。"""
        fields = {"last_heartbeat": datetime.now(timezone.utc).isoformat()}
        if status is not None:
            fields["status"] = status
        if current_job is not None:
            fields["current_job"] = current_job or None
        if current_step is not None:
            fields["current_step"] = current_step or None
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [worker_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE workers SET {set_clause} WHERE id=?",
                values,
            )
            self._conn.commit()

    def delete_worker(self, worker_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM workers WHERE id=?", (worker_id,))
            self._conn.commit()

    def list_worker_jobs(self, worker_id: str, limit: int = 50) -> list[Step]:
        """该 worker 跑过的步骤历史（按最近开始时间倒序），对应 runner 的 job 历史。"""
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE worker_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (worker_id, limit),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    # ── AI Usage ──

    def record_ai_usage(self, usage: AIUsage) -> bool:
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO ai_usage
                       (exec_id, job_id, step, provider, model,
                        input_tokens, output_tokens, cost_usd,
                        duration_sec, cached, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        usage.exec_id,
                        usage.job_id,
                        usage.step,
                        usage.provider,
                        usage.model,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cost_usd,
                        usage.duration_sec,
                        1 if usage.cached else 0,
                        usage.created_at.isoformat(),
                    ),
                )
                self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_usage_summary(
        self, job_id: str | None = None, since: str | None = None
    ) -> dict:
        where_parts: list[str] = []
        params: list = []
        if job_id:
            where_parts.append("job_id=?")
            params.append(job_id)
        if since:
            where_parts.append("created_at>=?")
            params.append(since)

        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        row = self._conn.execute(
            f"""SELECT
                COUNT(*) as calls,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(cost_usd), 0) as total_cost
            FROM ai_usage {where}""",
            params,
        ).fetchone()

        return {
            "calls": row["calls"],
            "total_input_tokens": row["total_input"],
            "total_output_tokens": row["total_output"],
            "total_cost_usd": row["total_cost"],
        }

    # ── Collection ──

    def create_collection(self, collection: Collection) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO collections
                   (id, name, domain, description, tags, job_count,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    collection.id,
                    collection.name,
                    collection.domain,
                    collection.description,
                    json.dumps(collection.tags, ensure_ascii=False),
                    collection.job_count,
                    collection.created_at.isoformat(),
                    collection.updated_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_collection(self, collection_id: str) -> Collection | None:
        row = self._conn.execute(
            "SELECT * FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        if row is None:
            return None
        return Collection(
            id=row["id"],
            name=row["name"],
            domain=row["domain"],
            description=row["description"],
            tags=json.loads(row["tags"]),
            job_count=row["job_count"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def list_collections(self) -> list[Collection]:
        rows = self._conn.execute("SELECT * FROM collections").fetchall()
        return [
            Collection(
                id=r["id"],
                name=r["name"],
                domain=r["domain"],
                description=r["description"],
                tags=json.loads(r["tags"]),
                job_count=r["job_count"],
                created_at=_parse_dt(r["created_at"]),
                updated_at=_parse_dt(r["updated_at"]),
            )
            for r in rows
        ]

    # ── Private ──

    def _row_to_job(self, row: sqlite3.Row) -> Job:
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
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            error=row["error"],
        )

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        return Step(
            job_id=row["job_id"],
            name=row["step"],
            status=StepStatus(row["status"]),
            pool=row["pool"],
            input_hash=row["input_hash"],
            worker_id=row["worker_id"],
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            duration_sec=row["duration_sec"],
            meta=json.loads(row["meta"]) if row["meta"] else {},
            error=row["error"],
            retries=row["retries"],
        )

    def _row_to_worker(self, row: sqlite3.Row) -> Worker:
        return Worker(
            id=row["id"],
            type=row["type"],
            pools=json.loads(row["pools"]),
            tags=set(json.loads(row["tags"])),
            reject_tags=set(json.loads(row["reject_tags"])),
            hostname=row["hostname"],
            gpu_name=row["gpu_name"],
            gpu_memory_mb=row["gpu_memory_mb"],
            status=row["status"],
            current_job=row["current_job"],
            current_step=row["current_step"],
            tasks_completed=row["tasks_completed"],
            tasks_failed=row["tasks_failed"],
            total_duration_sec=row["total_duration_sec"],
            first_seen=_parse_dt(row["first_seen"]),
            started_at=_parse_dt(row["started_at"]),
            last_heartbeat=_parse_dt(row["last_heartbeat"]),
            admin_note=row["admin_note"],
        )
