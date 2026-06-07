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

CREATE TABLE IF NOT EXISTS worker_tokens (
    token_hash TEXT PRIMARY KEY,
    worker_id  TEXT NOT NULL,
    pools      TEXT NOT NULL DEFAULT '[]',
    tags       TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    last_used  TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_worker_tokens_worker ON worker_tokens(worker_id);

CREATE TABLE IF NOT EXISTS app_credentials (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS glossary (
    domain TEXT NOT NULL,
    term TEXT NOT NULL,
    definition TEXT DEFAULT '',
    sources TEXT DEFAULT '[]',
    related TEXT DEFAULT '[]',
    status TEXT DEFAULT 'accepted',
    source_type TEXT DEFAULT 'manual',
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (domain, term)
);

CREATE INDEX IF NOT EXISTS idx_glossary_domain_status ON glossary(domain, status);

-- trigram tokenizer：对中文做子串匹配，零外部依赖。
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts5 USING fts5(
    job_id UNINDEXED,
    content_type UNINDEXED,
    note_type UNINDEXED,
    collection_id UNINDEXED,
    domain UNINDEXED,
    title,
    body,
    tokenize='trigram'
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


def _fts_match_query(q: str) -> str:
    """把用户查询串包成 fts5 安全的双引号短语，防 MATCH 语法注入。
    内部双引号转义为两个双引号；空白折叠；空查询返回空串（调用方按无结果处理）。"""
    cleaned = " ".join((q or "").split())
    if not cleaned:
        return ""
    escaped = cleaned.replace('"', '""')
    return f'"{escaped}"'


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

    # ── Worker Token ──

    def upsert_worker_token(
        self,
        token_hash: str,
        worker_id: str,
        pools: list[str],
        tags: list[str],
        created_at: datetime,
        revoked: bool = False,
    ) -> None:
        """登记一枚 per-worker token（仅存 sha256 hash），pools/tags 限定其授权范围。"""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO worker_tokens
                   (token_hash, worker_id, pools, tags, created_at, revoked)
                   VALUES (?,?,?,?,?,?)""",
                (
                    token_hash,
                    worker_id,
                    json.dumps(list(pools)),
                    json.dumps(list(tags)),
                    created_at.isoformat(),
                    1 if revoked else 0,
                ),
            )
            self._conn.commit()

    def get_worker_token_by_hash(self, token_hash: str) -> dict | None:
        """按 token hash 查 token 行，未命中返回 None；revoked 折算成 bool。"""
        row = self._conn.execute(
            "SELECT * FROM worker_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        return {
            "token_hash": row["token_hash"],
            "worker_id": row["worker_id"],
            "pools": json.loads(row["pools"]),
            "tags": json.loads(row["tags"]),
            "created_at": _parse_dt(row["created_at"]),
            "last_used": _parse_dt(row["last_used"]),
            "revoked": bool(row["revoked"]),
        }

    def revoke_worker_token(self, worker_id: str) -> None:
        """吊销某 worker 名下全部 token（删 worker 时连带，使其心跳/认领立即 401）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE worker_tokens SET revoked=1 WHERE worker_id=?", (worker_id,)
            )
            self._conn.commit()

    def list_worker_tokens(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM worker_tokens ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "token_hash": r["token_hash"],
                "worker_id": r["worker_id"],
                "pools": json.loads(r["pools"]),
                "tags": json.loads(r["tags"]),
                "created_at": _parse_dt(r["created_at"]),
                "last_used": _parse_dt(r["last_used"]),
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ]

    # ── App Credentials ──

    def set_credential(self, key: str, value: str) -> None:
        """存/覆盖一条应用级凭证（如 B站 cookie JSON），按 key 幂等 upsert。"""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO app_credentials (key, value, updated_at)
                   VALUES (?,?,?)""",
                (key, value, _now_iso()),
            )
            self._conn.commit()

    def get_credential(self, key: str) -> str | None:
        """读一条凭证值，未命中返回 None。"""
        row = self._conn.execute(
            "SELECT value FROM app_credentials WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    def delete_credential(self, key: str) -> None:
        """删一条凭证（如登出清除 B站 cookie）。"""
        with self._lock:
            self._conn.execute("DELETE FROM app_credentials WHERE key=?", (key,))
            self._conn.commit()

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

    def list_collections(self, domain: str | None = None) -> list[Collection]:
        if domain:
            rows = self._conn.execute(
                "SELECT * FROM collections WHERE domain=?", (domain,)
            ).fetchall()
        else:
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

    def update_collection(
        self,
        collection_id: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """更新集合的可变字段（name/description/tags），None 表示不动。"""
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if tags is not None:
            fields["tags"] = json.dumps(tags, ensure_ascii=False)
        if not fields:
            return
        fields["updated_at"] = _now_iso()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [collection_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE collections SET {set_clause} WHERE id=?", values
            )
            self._conn.commit()

    def delete_collection(self, collection_id: str) -> None:
        """删集合=解绑：把名下 job 的 collection_id 置 NULL（保留 job），再删集合行。"""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET collection_id=NULL WHERE collection_id=?",
                (collection_id,),
            )
            self._conn.execute(
                "DELETE FROM collections WHERE id=?", (collection_id,)
            )
            self._conn.commit()

    def increment_collection_count(self, collection_id: str, delta: int) -> None:
        """维护集合的 job_count：建/删 job 时增减；负值不下穿 0。"""
        if not collection_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE collections SET job_count = MAX(0, job_count + ?) WHERE id=?",
                (delta, collection_id),
            )
            self._conn.commit()

    # ── Glossary ──

    def upsert_glossary_term(
        self,
        domain: str,
        term: str,
        definition: str = "",
        related: list[str] | None = None,
        status: str = "accepted",
        source_type: str = "manual",
    ) -> None:
        """写入/覆盖一条术语（手动维护入口）：按 (domain, term) 幂等 upsert，
        保留已有 sources，覆盖 definition/related/status/source_type。"""
        now = _now_iso()
        related_json = json.dumps(related or [], ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT sources, created_at FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO glossary
                       (domain, term, definition, sources, related, status,
                        source_type, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (domain, term, definition, "[]", related_json,
                     status, source_type, now, now),
                )
            else:
                self._conn.execute(
                    """UPDATE glossary SET definition=?, related=?, status=?,
                       source_type=?, updated_at=? WHERE domain=? AND term=?""",
                    (definition, related_json, status, source_type, now,
                     domain, term),
                )
            self._conn.commit()

    def add_glossary_suggestion(
        self,
        domain: str,
        term: str,
        source_job: str,
        source_type: str = "review",
    ) -> None:
        """评审采集到的候选术语：不存在则插 status='suggested' 并记 source_job；
        已存在则仅把 source_job 并入 sources，绝不降级已 accepted 的条目。"""
        now = _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT sources FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO glossary
                       (domain, term, definition, sources, related, status,
                        source_type, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (domain, term, "", json.dumps([source_job]), "[]",
                     "suggested", source_type, now, now),
                )
            else:
                sources = json.loads(row["sources"])
                if source_job not in sources:
                    sources.append(source_job)
                    self._conn.execute(
                        "UPDATE glossary SET sources=?, updated_at=? "
                        "WHERE domain=? AND term=?",
                        (json.dumps(sources), now, domain, term),
                    )
            self._conn.commit()

    def get_glossary_term(self, domain: str, term: str) -> dict | None:
        """读单条术语，未命中返回 None。"""
        row = self._conn.execute(
            "SELECT * FROM glossary WHERE domain=? AND term=?", (domain, term)
        ).fetchone()
        return self._row_to_glossary(row) if row is not None else None

    def list_glossary(
        self, domain: str | None = None, status: str | None = None
    ) -> list[dict]:
        """列术语，可按 domain / status 过滤，按 term 升序。"""
        where_parts: list[str] = []
        params: list = []
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if status:
            where_parts.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        rows = self._conn.execute(
            f"SELECT * FROM glossary {where} ORDER BY term", params
        ).fetchall()
        return [self._row_to_glossary(r) for r in rows]

    def accept_glossary_term(self, domain: str, term: str) -> None:
        """采纳候选术语：status -> 'accepted'。"""
        with self._lock:
            self._conn.execute(
                "UPDATE glossary SET status='accepted', updated_at=? "
                "WHERE domain=? AND term=?",
                (_now_iso(), domain, term),
            )
            self._conn.commit()

    def delete_glossary_term(self, domain: str, term: str) -> None:
        """删一条术语。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM glossary WHERE domain=? AND term=?", (domain, term)
            )
            self._conn.commit()

    # ── Notes 全文索引 (FTS5) ──

    def index_job_notes(
        self,
        job_id: str,
        note_type: str,
        title: str,
        body: str,
        content_type: str = "",
        domain: str = "",
        collection_id: str = "",
    ) -> None:
        """把某 job 某类笔记写入 FTS 索引：先删该 (job_id, note_type) 行再插，幂等。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM notes_fts5 WHERE job_id=? AND note_type=?",
                (job_id, note_type),
            )
            self._conn.execute(
                """INSERT INTO notes_fts5
                   (job_id, content_type, note_type, collection_id, domain,
                    title, body)
                   VALUES (?,?,?,?,?,?,?)""",
                (job_id, content_type, note_type, collection_id or "",
                 domain or "", title or "", body or ""),
            )
            self._conn.commit()

    def delete_job_index(self, job_id: str) -> None:
        """删某 job 的全部笔记索引行（删 job 时连带）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM notes_fts5 WHERE job_id=?", (job_id,)
            )
            self._conn.commit()

    def search_notes(
        self,
        q: str,
        collection_id: str | None = None,
        domain: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """全文检索笔记。q 走 fts5 MATCH（trigram，中文子串友好），做基本转义防注入；
        可按 collection_id / domain / content_type 收窄。返回 (total, items)，
        items 含 job_id/note_type/title/snippet/content_type/domain/collection_id。
        注意：trigram 至少需 3 个字符才能命中，更短的查询会无结果。"""
        match = _fts_match_query(q)
        if not match:
            return 0, []

        where_parts = ["notes_fts5 MATCH ?"]
        params: list = [match]
        if collection_id:
            where_parts.append("collection_id=?")
            params.append(collection_id)
        if domain:
            where_parts.append("domain=?")
            params.append(domain)
        if content_type:
            where_parts.append("content_type=?")
            params.append(content_type)
        where = " AND ".join(where_parts)

        total = self._conn.execute(
            f"SELECT COUNT(*) FROM notes_fts5 WHERE {where}", params
        ).fetchone()[0]

        # snippet(表, 列号 6=body, 高亮包裹, 省略号, 单片最多 12 token)。
        rows = self._conn.execute(
            f"""SELECT job_id, note_type, title, content_type, domain,
                   collection_id,
                   snippet(notes_fts5, 6, '<mark>', '</mark>', '…', 12) AS snippet
                FROM notes_fts5 WHERE {where}
                ORDER BY rank LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        items = [
            {
                "job_id": r["job_id"],
                "note_type": r["note_type"],
                "title": r["title"],
                "snippet": r["snippet"],
                "content_type": r["content_type"],
                "domain": r["domain"],
                "collection_id": r["collection_id"] or None,
            }
            for r in rows
        ]
        return total, items

    # ── Private ──

    def _row_to_glossary(self, row: sqlite3.Row) -> dict:
        return {
            "domain": row["domain"],
            "term": row["term"],
            "definition": row["definition"],
            "sources": json.loads(row["sources"]),
            "related": json.loads(row["related"]),
            "status": row["status"],
            "source_type": row["source_type"],
            "created_at": _parse_dt(row["created_at"]),
            "updated_at": _parse_dt(row["updated_at"]),
        }

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
