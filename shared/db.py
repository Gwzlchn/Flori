"""SQLite 数据库层。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import structlog

from .models import AIUsage, Collection, Job, JobStatus, Step, StepStatus, Worker
from .status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    STALE,
    compute_worker_status,
)

# DB schema 版本戳。当前仅做加列(additive)迁移,无需版本驱动;此戳是为将来
# 非加列迁移(改列/删列/数据回填)与备份兼容校验预留的钩子——可经 PRAGMA user_version 查询。
SCHEMA_VERSION = 1


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
    published_at TEXT,
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
    concurrency INTEGER NOT NULL DEFAULT 1,
    remote_addr TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    admin_status TEXT NOT NULL DEFAULT '',
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
    worker_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    num_turns INTEGER DEFAULT 0,
    cached INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_job ON ai_usage(job_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_provider ON ai_usage(provider);

-- 集合：订阅是集合的属性(source_type/source_id 非空=订阅集合)，无独立 subscriptions 表。
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    job_count INTEGER DEFAULT 0,
    source_type TEXT,
    source_id TEXT,
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
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

-- 订阅去重(通用,跨来源):一个集合(订阅)已入库过哪些 item(B站=bvid、youtube=videoId、
-- rss=entry id/link、local=文件名)。source-adapter 模式下各来源统一用 item_id 去重,
-- 不再依赖从 jobs.url 抠 BV 号(旧 ingested_bvids 仅 B站可用)。
CREATE TABLE IF NOT EXISTS ingested_items (
    collection_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, item_id)
);

-- 概念图/知识层：occurrences=[{job_id,content_type,location}] 类型化出现索引(替代旧 sources)；
-- is_topic=粗粒度浏览主题；definition_locked=钉住后不被自动综合覆盖。
CREATE TABLE IF NOT EXISTS glossary (
    domain TEXT NOT NULL,
    term TEXT NOT NULL,
    definition TEXT DEFAULT '',
    occurrences TEXT DEFAULT '[]',
    related TEXT DEFAULT '[]',
    status TEXT DEFAULT 'accepted',
    is_topic INTEGER DEFAULT 0,
    definition_locked INTEGER DEFAULT 0,
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
    "published_at",
}
_STEP_UPDATABLE = {
    "status", "input_hash", "worker_id", "started_at", "finished_at",
    "duration_sec", "meta", "error", "retries",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_log = structlog.get_logger(component="db")


@lru_cache(maxsize=1)
def _fernet():
    """凭证 at-rest 加密的 Fernet 实例（按 FLORI_SECRET_KEY 缓存）。

    key 取自环境变量 FLORI_SECRET_KEY（urlsafe-base64 的 32 字节 Fernet key）。
    未设/为空 → 返回 None（凭证退回明文存储，向后兼容）。cryptography 在此惰性
    导入，缺库或 key 非法时返回 None，使本模块在无该依赖/未配 key 时仍可正常 import
    与运行（其它 DB 用法与测试不受影响）。"""
    key = (os.environ.get("FLORI_SECRET_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet  # 惰性导入：缺库也不影响模块 import
        return Fernet(key.encode())
    except Exception as e:  # 库缺失 / key 非法 → 退回明文(不阻断启动)
        _log.warning("credential_fernet_init_failed", error=str(e)[:200])
        return None


_PLAINTEXT_CRED_WARNED = False


def _warn_plaintext_credentials_once() -> None:
    """无 Fernet key 时存凭证仅警告一次，提示设 FLORI_SECRET_KEY 以加密 at-rest。"""
    global _PLAINTEXT_CRED_WARNED
    if not _PLAINTEXT_CRED_WARNED:
        _PLAINTEXT_CRED_WARNED = True
        _log.warning(
            "credentials_stored_plaintext",
            hint="set FLORI_SECRET_KEY (a Fernet key) to encrypt app_credentials at rest",
        )


def _fts_match_query(q: str) -> str:
    """把用户查询串包成 fts5 安全的双引号短语，防 MATCH 语法注入。
    内部双引号转义为两个双引号；空白折叠；空查询返回空串（调用方按无结果处理）。"""
    # 剔除空字节(null byte):sqlite3 绑定含 \x00 的串会抛 "unterminated string";它也非有效检索词。
    cleaned = " ".join((q or "").replace("\x00", "").split())
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
        # api/scheduler/worker 三进程各开连接写同一文件,撞 SQLITE_BUSY 时等待而非立刻报错。
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        # RLock(可重入):写方法持锁 execute+commit,序列化对【单一共享连接】的写访问。
        # 读方法多数直接走单一共享连接(check_same_thread=False),依赖 C 层(GIL + SQLite
        # 单条语句)的原子性而【不额外持锁】;少数"多条读+组装"的复合读(如 get_job/list_jobs)
        # 持锁,序列化读游标迭代与另一线程 commit,避免见到半提交态。WAL+busy_timeout 负责
        # 跨连接竞争。可重入以便持锁方法内部再调其它持锁读不自死锁。
        self._lock = threading.RLock()

    def init_schema(self) -> None:
        with self._lock:
            # 先补旧表缺列,再跑 schema：否则 schema 里 ON jobs(collection_id) 的
            # CREATE INDEX 会在缺该列的老库上先行报错。新库此步无表可补、直接跳过。
            self._ensure_columns()
            self._conn.executescript(_SCHEMA_SQL)
            # 版本戳钩子:user_version=0 表示全新库或尚未打戳的旧库,统一标记为 1。
            # 当前只做加列迁移、无需版本驱动;此处仅建立版本号以便将来非加列迁移
            # (改列/删列/回填)按 user_version 分支处理 + 做备份兼容校验。不在此放迁移逻辑。
            if self._conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        """当前库的 schema 版本(PRAGMA user_version)。供备份兼容/未来迁移判断。"""
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # 各表的期望列(列名 -> 建列 SQL 片段)。旧库缺列时按需 ALTER ADD,
    # 避免"代码加了新列、旧库没有 → 查询崩"。新增列只在此登记即可平滑升级。
    _EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
        "jobs": {
            "collection_id": "collection_id TEXT",
            "source": "source TEXT",
            "published_at": "published_at TEXT",
        },
        "job_steps": {"retries": "retries INTEGER DEFAULT 0"},
        "ai_usage": {
            "worker_id": "worker_id TEXT",
            "cache_creation_input_tokens": "cache_creation_input_tokens INTEGER DEFAULT 0",
            "cache_read_input_tokens": "cache_read_input_tokens INTEGER DEFAULT 0",
            "num_turns": "num_turns INTEGER DEFAULT 0",
        },
        "workers": {
            "reject_tags": "reject_tags TEXT NOT NULL DEFAULT '[]'",
            "admin_note": "admin_note TEXT",
            "admin_status": "admin_status TEXT NOT NULL DEFAULT ''",
            "concurrency": "concurrency INTEGER NOT NULL DEFAULT 1",
            "remote_addr": "remote_addr TEXT",
        },
        "collections": {
            "source_type": "source_type TEXT",
            "source_id": "source_id TEXT",
            "sync_enabled": "sync_enabled INTEGER NOT NULL DEFAULT 1",
            "last_synced_at": "last_synced_at TEXT",
        },
        "glossary": {
            "occurrences": "occurrences TEXT DEFAULT '[]'",
            "is_topic": "is_topic INTEGER DEFAULT 0",
            "definition_locked": "definition_locked INTEGER DEFAULT 0",
        },
    }

    def _ensure_columns(self) -> None:
        """幂等的列迁移:对已存在的表补齐期望列(SQLite 不支持 IF NOT EXISTS 加列)。"""
        for table, cols in self._EXPECTED_COLUMNS.items():
            existing = {
                r["name"]
                for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:
                continue  # 表不存在(schema 会建),跳过
            for col, ddl in cols.items():
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

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
                    published_at, created_at, updated_at, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    job.published_at.isoformat() if job.published_at else None,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.error,
                ),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
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
        domain: str | None = None,
        source: str | None = None,
        uncategorized: bool = False,
    ) -> tuple[int, list[Job]]:
        where_parts: list[str] = []
        params: list = []
        if status:
            where_parts.append("status=?")
            params.append(status)
        if uncategorized:           # 未归类:无所属集合(侧栏「未归类」分组)
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

        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM jobs {where}", params
            ).fetchone()[0]

            rows = self._conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return total, [self._row_to_job(r) for r in rows]

    def count_jobs_by_status(self) -> dict[str, int]:
        """一次 GROUP BY 取各状态计数(替代多次 list_jobs(limit=0) 的 COUNT+空 SELECT)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def job_facets(self) -> dict[str, dict]:
        """全量 jobs 按 source / domain / status 的计数,供前端过滤 chip 显示(后端聚合,非客户端基于已加载)。"""
        def grp(col: str) -> dict:
            with self._lock:
                return {
                    r[0]: r[1]
                    for r in self._conn.execute(
                        f"SELECT {col}, COUNT(*) FROM jobs GROUP BY {col}"
                    ).fetchall()
                    if r[0] is not None
                }
        return {"source": grp("source"), "domain": grp("domain"), "status": grp("status")}

    def glossary_for_job(self, job_id: str, domain: str | None = None) -> list[dict]:
        """反查:occurrences 含该 job_id 的概念(LIKE 粗筛 + 精确过滤防子串误命中),供内容详情·概念 tab。"""
        sql = "SELECT * FROM glossary WHERE occurrences LIKE ?"
        params: list = [f'%"{job_id}"%']
        if domain:
            sql += " AND domain=?"
            params.append(domain)
        out: list[dict] = []
        for r in self._conn.execute(sql, params):
            g = self._row_to_glossary(r)
            occs = g.get("occurrences") or []
            hit = [o for o in occs if isinstance(o, dict) and o.get("job_id") == job_id]
            if hit:
                g["job_occurrences"] = hit       # 该 job 命中的位置(首次出现等)
                out.append(g)
        return out

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
        if "published_at" in fields and isinstance(fields["published_at"], datetime):
            fields["published_at"] = fields["published_at"].isoformat()

        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [job_id]
        # FTS 行冗余存 title/domain/collection_id,这几项变更要同步,否则检索元数据漂移。
        fts_sync = {k: fields[k] for k in ("title", "domain", "collection_id") if k in fields}
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {set_clause} WHERE id=?", values
            )
            if fts_sync:
                fts_clause = ", ".join(f"{k}=?" for k in fts_sync)
                self._conn.execute(
                    f"UPDATE notes_fts5 SET {fts_clause} WHERE job_id=?",
                    [("" if v is None else v) for v in fts_sync.values()] + [job_id],
                )
            self._conn.commit()

    def _strip_occurrences_for_jobs(self, job_ids: list[str]) -> None:
        """从 glossary.occurrences 摘除指向这些 job 的出现(保留概念与定义,§1.10-7)。
        调用方须【已持锁且在同一事务内】;本方法只 execute,不 commit。"""
        for job_id in job_ids:
            # glossary.occurrences=[{job_id,...}]，摘掉指向已删 job 的出现。
            rows = self._conn.execute(
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
                    self._conn.execute(
                        "UPDATE glossary SET occurrences=? WHERE domain=? AND term=?",
                        (json.dumps(kept, ensure_ascii=False), r["domain"], r["term"]),
                    )

    def delete_job_cascade(self, job_id: str, collection_id: str | None = None) -> None:
        """原子删 job：jobs 行 + FTS 索引 + 集合计数 -1 + 摘除 glossary.occurrences 里的 job_id。
        全部在单事务内,避免两次 commit 之间崩溃留孤儿 FTS 行 / 计数错位。
        job_steps 经 FK ON DELETE CASCADE 连带删除。"""
        with self._lock:
            self._conn.execute("DELETE FROM notes_fts5 WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            if collection_id:
                self._conn.execute(
                    "UPDATE collections SET job_count = MAX(0, job_count - 1) WHERE id=?",
                    (collection_id,),
                )
            self._strip_occurrences_for_jobs([job_id])
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

    def delete_step(self, job_id: str, step_name: str) -> None:
        """删单个步骤行(供 resubmit 对齐:删去当前 pipeline 不再有的步,避免 DB 残留旧步)。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM job_steps WHERE job_id=? AND step=?", (job_id, step_name)
            )
            self._conn.commit()

    def update_step(
        self, job_id: str, step_name: str, *, only_if_active: bool = False, **fields
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
        with self._lock:
            self._conn.execute(
                f"UPDATE job_steps SET {set_clause} WHERE {where}",
                values,
            )
            self._conn.commit()

    # ── Worker ──

    def upsert_worker(self, worker: Worker) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO workers
                   (id, type, pools, tags, reject_tags, hostname, gpu_name,
                    gpu_memory_mb, concurrency, remote_addr, status, admin_status,
                    current_job, current_step,
                    tasks_completed, tasks_failed, total_duration_sec,
                    first_seen, started_at, last_heartbeat, admin_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    worker.id,
                    worker.type,
                    json.dumps(worker.pools),
                    json.dumps(sorted(worker.tags)),
                    json.dumps(sorted(worker.reject_tags)),
                    worker.hostname,
                    worker.gpu_name,
                    worker.gpu_memory_mb,
                    worker.concurrency,
                    worker.remote_addr,
                    worker.status,
                    worker.admin_status,
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
        offline、stale，paused 为管理员叠加）。越过 stale 窗口的持久化为信号，
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
        管理员叠加位(paused)来自独立的 admin_status 列；运行时 status 列只供 busy/idle + GC。"""
        public = compute_worker_status(
            last_heartbeat=w.last_heartbeat,
            current_job=w.current_job,
            admin_status=w.admin_status,
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

    def set_worker_admin_status(self, worker_id: str, admin_status: str) -> None:
        """仅更新管理员暂停叠加位("" / "paused")，不触碰运行时 status / 心跳。"""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET admin_status=? WHERE id=?",
                (admin_status, worker_id),
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
        """存/覆盖一条应用级凭证（如 B站 cookie JSON），按 key 幂等 upsert。

        设了 FLORI_SECRET_KEY 时以 Fernet token 加密落库；未设则存明文(向后兼容)
        并一次性告警(建议设 key 以 at-rest 加密)。"""
        f = _fernet()
        if f is not None:
            stored = f.encrypt(value.encode()).decode()
        else:
            _warn_plaintext_credentials_once()
            stored = value
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO app_credentials (key, value, updated_at)
                   VALUES (?,?,?)""",
                (key, stored, _now_iso()),
            )
            self._conn.commit()

    def get_credential(self, key: str) -> str | None:
        """读一条凭证值，未命中返回 None。

        有 Fernet key 时尝试解密；遇 InvalidToken(历史明文行，或换了 key 的旧 token)
        透传原始串(legacy passthrough)。无 key 则直接返回原始串。任何情况都不因坏值崩。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_credentials WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        raw = row["value"]
        if raw is None:
            return None
        f = _fernet()
        if f is None:
            return raw
        try:
            from cryptography.fernet import InvalidToken
            return f.decrypt(raw.encode()).decode()
        except InvalidToken:
            return raw  # 明文遗留行 / 异 key 的 token：原样透传
        except Exception:
            return raw  # 任何意外都不让读凭证崩

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
                       (exec_id, job_id, step, worker_id, provider, model,
                        input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        cost_usd, duration_sec, num_turns, cached, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        usage.exec_id,
                        usage.job_id,
                        usage.step,
                        usage.worker_id,
                        usage.provider,
                        usage.model,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_creation_input_tokens,
                        usage.cache_read_input_tokens,
                        usage.cost_usd,
                        usage.duration_sec,
                        usage.num_turns,
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

    def get_usage_aggregate(self) -> dict:
        """全量 AI 用量聚合(供 /api/usage + 系统状态展示):累计 token/缓存/成本 + 平均缓存命中率
        + 按 model 分。命中率 = cache_read /(input + cache_read + cache_creation)。"""
        with self._lock:
            total = self._conn.execute(
                """SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(input_tokens),0) AS in_tok,
                    COALESCE(SUM(output_tokens),0) AS out_tok,
                    COALESCE(SUM(cache_creation_input_tokens),0) AS cc_tok,
                    COALESCE(SUM(cache_read_input_tokens),0) AS cr_tok,
                    COALESCE(SUM(cost_usd),0) AS cost,
                    COALESCE(SUM(num_turns),0) AS turns,
                    COALESCE(SUM(duration_sec),0) AS dur
                FROM ai_usage""",
            ).fetchone()
            rows = self._conn.execute(
                """SELECT provider, model,
                    COUNT(*) AS calls,
                    COALESCE(SUM(input_tokens),0) AS in_tok,
                    COALESCE(SUM(output_tokens),0) AS out_tok,
                    COALESCE(SUM(cache_creation_input_tokens),0) AS cc_tok,
                    COALESCE(SUM(cache_read_input_tokens),0) AS cr_tok,
                    COALESCE(SUM(cost_usd),0) AS cost
                FROM ai_usage GROUP BY provider, model ORDER BY cost DESC""",
            ).fetchall()

        def _hit_rate(in_tok: int, cc: int, cr: int) -> float:
            denom = in_tok + cc + cr
            return round(cr / denom * 100, 1) if denom else 0.0

        return {
            "calls": total["calls"],
            "total_input_tokens": total["in_tok"],
            "total_output_tokens": total["out_tok"],
            "total_cache_creation_tokens": total["cc_tok"],
            "total_cache_read_tokens": total["cr_tok"],
            "total_cost_usd": round(total["cost"], 6),
            "total_num_turns": total["turns"],
            "total_duration_sec": round(total["dur"], 1),
            "cache_hit_rate_pct": _hit_rate(total["in_tok"], total["cc_tok"], total["cr_tok"]),
            "by_model": [
                {
                    "provider": r["provider"], "model": r["model"], "calls": r["calls"],
                    "input_tokens": r["in_tok"], "output_tokens": r["out_tok"],
                    "cache_creation_tokens": r["cc_tok"], "cache_read_tokens": r["cr_tok"],
                    "cost_usd": round(r["cost"], 6),
                    "cache_hit_rate_pct": _hit_rate(r["in_tok"], r["cc_tok"], r["cr_tok"]),
                }
                for r in rows
            ],
        }

    def list_usage_by_job(self, job_id: str) -> list[dict]:
        """该 job 的逐次 AI 调用明细(供 job 详情按步展示:in/out/cache/命中率/cost/耗时/轮数/worker)。
        命中率 = cache_read /(input + cache_read + cache_creation)。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT step, worker_id, provider, model,
                    input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    cost_usd, duration_sec, num_turns, created_at
                FROM ai_usage WHERE job_id=? ORDER BY created_at""",
                (job_id,),
            ).fetchall()
        out = []
        for r in rows:
            denom = r["input_tokens"] + r["cache_creation_input_tokens"] + r["cache_read_input_tokens"]
            hit = round(r["cache_read_input_tokens"] / denom * 100, 1) if denom else 0.0
            out.append({
                "step": r["step"], "worker_id": r["worker_id"],
                "provider": r["provider"], "model": r["model"],
                "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                "cache_creation_tokens": r["cache_creation_input_tokens"],
                "cache_read_tokens": r["cache_read_input_tokens"],
                "cost_usd": round(r["cost_usd"], 6), "duration_sec": r["duration_sec"],
                "num_turns": r["num_turns"], "cache_hit_rate_pct": hit,
            })
        return out

    def throughput_since(self, since_iso: str) -> dict:
        """近窗口吞吐:since_iso 之后进入终态的 job 计数(done/failed)。用 updated_at 近似终态时刻
        (rerun 改 updated_at 致重复计入罕见,设计 §7.3 已注;利用 idx_jobs_status)。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, COUNT(*) AS n FROM jobs
                   WHERE status IN ('done','failed') AND updated_at >= ?
                   GROUP BY status""",
                (since_iso,),
            ).fetchall()
        by = {r["status"]: r["n"] for r in rows}
        return {"done": by.get("done", 0), "failed": by.get("failed", 0)}

    # ── Collection ──

    def _row_to_collection(self, r: sqlite3.Row) -> Collection:
        return Collection(
            id=r["id"],
            name=r["name"],
            domain=r["domain"],
            description=r["description"],
            tags=json.loads(r["tags"]),
            job_count=r["job_count"],
            source_type=r["source_type"],
            source_id=r["source_id"],
            sync_enabled=bool(r["sync_enabled"]),
            last_synced_at=_parse_dt(r["last_synced_at"]),
            created_at=_parse_dt(r["created_at"]),
            updated_at=_parse_dt(r["updated_at"]),
        )

    def create_collection(self, collection: Collection) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO collections
                   (id, name, domain, description, tags, job_count,
                    source_type, source_id, sync_enabled, last_synced_at,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    collection.id,
                    collection.name,
                    collection.domain,
                    collection.description,
                    json.dumps(collection.tags, ensure_ascii=False),
                    collection.job_count,
                    collection.source_type,
                    collection.source_id,
                    1 if collection.sync_enabled else 0,
                    collection.last_synced_at.isoformat() if collection.last_synced_at else None,
                    collection.created_at.isoformat(),
                    collection.updated_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_collection(self, collection_id: str) -> Collection | None:
        row = self._conn.execute(
            "SELECT * FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        return self._row_to_collection(row) if row else None

    def list_collections(self, domain: str | None = None) -> list[Collection]:
        if domain:
            rows = self._conn.execute(
                "SELECT * FROM collections WHERE domain=?", (domain,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM collections").fetchall()
        return [self._row_to_collection(r) for r in rows]

    def find_collection_by_source(self, source_type: str, source_id: str) -> Collection | None:
        """按来源找订阅集合(建订阅前去重；一个来源全局唯一对应一个订阅集合)。"""
        row = self._conn.execute(
            "SELECT * FROM collections WHERE source_type=? AND source_id=?",
            (source_type, source_id),
        ).fetchone()
        return self._row_to_collection(row) if row else None

    def list_subscription_collections(self, enabled_only: bool = False) -> list[Collection]:
        """订阅集合(source_type 非空)；enabled_only 时仅自动追更开启的。周期同步用。"""
        q = "SELECT * FROM collections WHERE source_type IS NOT NULL"
        if enabled_only:
            q += " AND sync_enabled=1"
        return [self._row_to_collection(r) for r in self._conn.execute(q).fetchall()]

    def update_collection(
        self,
        collection_id: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        sync_enabled: bool | None = None,
    ) -> None:
        """更新集合可变字段（name/description/tags/订阅自动追更开关），None 表示不动。"""
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if tags is not None:
            fields["tags"] = json.dumps(tags, ensure_ascii=False)
        if sync_enabled is not None:
            fields["sync_enabled"] = 1 if sync_enabled else 0
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

    def delete_collection(self, collection_id: str, purge: bool = False) -> None:
        """删集合两模式。默认解绑:名下 job 的 collection_id 置 NULL(保留 job)。
        purge=True:连名下 job 一起删(jobs 行 + FTS 行 + 摘除各 job 的 glossary.occurrences;
        注:产物/MinIO 清理走既有 job 删除路径)。
        两种都清该集合 ingested_items(便于重订阅重新入库)。FTS 索引行同步处理,避免悬空行。"""
        with self._lock:
            if purge:
                # 先摘除名下 job 的 glossary 出现记录,避免删 job 后留悬空 job_id(与 delete_job_cascade 一致)。
                job_rows = self._conn.execute(
                    "SELECT id FROM jobs WHERE collection_id=?", (collection_id,)
                ).fetchall()
                self._strip_occurrences_for_jobs([r["id"] for r in job_rows])
                self._conn.execute(
                    "DELETE FROM notes_fts5 WHERE collection_id=?", (collection_id,)
                )
                self._conn.execute(
                    "DELETE FROM jobs WHERE collection_id=?", (collection_id,)
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET collection_id=NULL WHERE collection_id=?",
                    (collection_id,),
                )
                self._conn.execute(
                    "UPDATE notes_fts5 SET collection_id='' WHERE collection_id=?",
                    (collection_id,),
                )
            self._conn.execute(
                "DELETE FROM ingested_items WHERE collection_id=?", (collection_id,)
            )
            self._conn.execute(
                "DELETE FROM collections WHERE id=?", (collection_id,)
            )
            self._conn.commit()

    def mark_collection_synced(self, collection_id: str, dt: datetime) -> None:
        """订阅集合同步后记录 last_synced_at。"""
        with self._lock:
            self._conn.execute(
                "UPDATE collections SET last_synced_at=?, updated_at=? WHERE id=?",
                (dt.isoformat(), _now_iso(), collection_id),
            )
            self._conn.commit()

    # ── Domain（领域是派生视图：来自 jobs ∪ collections ∪ glossary 的 distinct domain）──

    def list_domains(self) -> list[dict]:
        """领域总览：每个 domain 的 集合数/内容数/概念数/订阅数/最近活跃(派生,无 domains 表)。"""
        domains: set[str] = set()
        for tbl in ("jobs", "collections", "glossary"):
            for r in self._conn.execute(
                f"SELECT DISTINCT domain FROM {tbl} WHERE domain IS NOT NULL AND domain<>''"
            ):
                domains.add(r[0])

        def grp(sql: str) -> dict:
            return {r[0]: r[1] for r in self._conn.execute(sql)}

        coll_c = grp("SELECT domain, COUNT(*) FROM collections GROUP BY domain")
        job_c = grp("SELECT domain, COUNT(*) FROM jobs GROUP BY domain")
        concept_c = grp("SELECT domain, COUNT(*) FROM glossary GROUP BY domain")
        sub_c = grp("SELECT domain, COUNT(*) FROM collections WHERE source_type IS NOT NULL GROUP BY domain")
        last = grp("SELECT domain, MAX(updated_at) FROM jobs GROUP BY domain")
        return [
            {
                "domain": d,
                "collection_count": coll_c.get(d, 0),
                "job_count": job_c.get(d, 0),
                "concept_count": concept_c.get(d, 0),
                "subscription_count": sub_c.get(d, 0),
                "last_active_at": last.get(d),
            }
            for d in sorted(domains)
        ]

    def domain_top_terms(self, domain: str, limit: int = 30) -> list[dict]:
        """领域工作台语义栏：该 domain 的术语(含候选 suggested，各带 status)，按来源数(佐证强度代理)降序。
        候选数另由 suggested_count 单独提示；前端可按 status 区分展示。"""
        rows = self._conn.execute(
            "SELECT term, definition, occurrences, status, is_topic FROM glossary WHERE domain=?",
            (domain,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            out.append({
                "term": r["term"], "definition": r["definition"],
                "source_count": len(occs) if isinstance(occs, list) else 0,
                "status": r["status"], "is_topic": bool(r["is_topic"]),
            })
        out.sort(key=lambda t: t["source_count"], reverse=True)
        return out[:limit]

    def concept_timeline(self, domain: str, granularity: str = "month") -> dict:
        """概念时间线:把该 domain 各概念的 occurrences 经 job_id→源内容发布时间映射,按粒度分桶计数。
        分桶时间用 COALESCE(published_at, created_at):优先源内容在平台的发布/更新时间(「这个概念
        在世界上何时出现」),无已知发布时间的 job 回退入库时间(created_at),不丢计数。
        granularity: day(YYYY-MM-DD) / week(YYYY-Www) / month(YYYY-MM)。无 glossary/job 时返回空。"""
        from collections import defaultdict
        job_dates = {
            r["id"]: r["bucket_at"]
            for r in self._conn.execute(
                "SELECT id, COALESCE(published_at, created_at) AS bucket_at "
                "FROM jobs WHERE domain=?",
                (domain,),
            )
        }

        def bucket(iso: str | None) -> str | None:
            dt = _parse_dt(iso)
            if dt is None:
                return None
            if granularity == "day":
                return dt.strftime("%Y-%m-%d")
            if granularity == "week":
                y, w, _ = dt.isocalendar()
                return f"{y}-W{w:02d}"
            return dt.strftime("%Y-%m")

        rows = self._conn.execute(
            "SELECT term, occurrences FROM glossary WHERE domain=?", (domain,)
        ).fetchall()
        totals: dict = defaultdict(int)
        concepts: list[dict] = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            buckets: dict = defaultdict(int)
            for o in occs if isinstance(occs, list) else []:
                b = bucket(job_dates.get(o.get("job_id")))
                if b:
                    buckets[b] += 1
                    totals[b] += 1
            if buckets:
                concepts.append({
                    "term": r["term"], "buckets": dict(buckets),
                    "total": sum(buckets.values()),
                })
        concepts.sort(key=lambda c: c["total"], reverse=True)
        return {
            "granularity": granularity,
            "buckets": sorted(totals),
            "totals": dict(totals),
            "concepts": concepts,
        }

    def domain_topics(self, domain: str) -> list[dict]:
        """领域内主题(可浏览标签) = 该 domain 所有 job 的 style_tags distinct + 计数。"""
        from collections import Counter
        c: Counter = Counter()
        for r in self._conn.execute("SELECT style_tags FROM jobs WHERE domain=?", (domain,)):
            try:
                for t in json.loads(r["style_tags"] or "[]"):
                    if t:
                        c[t] += 1
            except (ValueError, TypeError):
                pass
        return [{"topic": t, "count": n} for t, n in c.most_common()]

    def ingested_bvids(self) -> set[str]:
        """已入库的 B站 BV 号集合(从 jobs.url 提取),供订阅同步去重。
        注:source-adapter 模式新增了通用去重表 ingested_items(见 ingested_item_ids/
        mark_ingested),按 (collection_id, item_id) 去重。此方法保留供旧库/旧 bili
        数据的兜底回填——同步首跑时可把它的结果并入某集合的 ingested 集合,
        避免迁移前已入库的 B站视频被重复建 job。"""
        import re
        out: set[str] = set()
        for (u,) in self._conn.execute(
            "SELECT url FROM jobs WHERE url LIKE '%BV%'"
        ).fetchall():
            m = re.search(r"(BV[0-9A-Za-z]{8,12})", u or "")
            if m:
                out.add(m.group(1))
        return out

    def ingested_item_ids(self, collection_id: str) -> set[str]:
        """某集合(订阅)已入库过的 item_id 集合,供 source-adapter 通用去重。
        item_id 含义随来源而定(B站=bvid、youtube=videoId、rss=entry id 等)。"""
        rows = self._conn.execute(
            "SELECT item_id FROM ingested_items WHERE collection_id=?",
            (collection_id,),
        ).fetchall()
        return {r["item_id"] for r in rows}

    def mark_ingested(self, collection_id: str, item_id: str) -> None:
        """登记某集合已入库 item_id(幂等:重复 mark 不报错),同步成功后调。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ingested_items "
                "(collection_id, item_id, ingested_at) VALUES (?,?,?)",
                (collection_id, item_id, _now_iso()),
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
    ) -> None:
        """写入/覆盖一条术语（手动维护入口）：按 (domain, term) 幂等 upsert，
        保留已有 occurrences，覆盖 definition/related/status。"""
        now = _now_iso()
        related_json = json.dumps(related or [], ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO glossary
                       (domain, term, definition, occurrences, related, status,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (domain, term, definition, "[]", related_json, status, now, now),
                )
            else:
                self._conn.execute(
                    """UPDATE glossary SET definition=?, related=?, status=?,
                       updated_at=? WHERE domain=? AND term=?""",
                    (definition, related_json, status, now, domain, term),
                )
            self._conn.commit()

    def add_glossary_suggestion(
        self,
        domain: str,
        term: str,
        job_id: str,
        content_type: str = "",
        location: str | None = None,
        definition: str = "",
    ) -> None:
        """抽取(①「这篇讲清楚了什么」)采集候选概念：不存在则插 status='suggested' 记一条
        occurrence + 候选定义；已存在则把该 job 的 occurrence 并入(按 job_id 去重)，
        绝不降级已 accepted 的条目。候选定义仅在该条尚无定义且未钉住时补写——不覆盖
        已有/已钉住定义(§1.10-11)。occurrence = {job_id, content_type, location}（§1.5）。"""
        now = _now_iso()
        occ = {"job_id": job_id, "content_type": content_type, "location": location}
        with self._lock:
            row = self._conn.execute(
                "SELECT occurrences, definition, definition_locked "
                "FROM glossary WHERE domain=? AND term=?",
                (domain, term),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO glossary
                       (domain, term, definition, occurrences, related, status,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (domain, term, definition, json.dumps([occ], ensure_ascii=False),
                     "[]", "suggested", now, now),
                )
            else:
                occs = json.loads(row["occurrences"] or "[]")
                changed = False
                if not any(o.get("job_id") == job_id for o in occs):
                    occs.append(occ)
                    changed = True
                new_def = row["definition"]
                # 候选定义补空:仅当本条还没定义且未钉住时填(不覆盖已有/已钉住，§1.10-11)。
                if definition and not (row["definition"] or "").strip() \
                        and not row["definition_locked"]:
                    new_def = definition
                    changed = True
                if changed:
                    self._conn.execute(
                        "UPDATE glossary SET occurrences=?, definition=?, updated_at=? "
                        "WHERE domain=? AND term=?",
                        (json.dumps(occs, ensure_ascii=False), new_def, now, domain, term),
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

    def set_glossary_topic(self, domain: str, term: str, is_topic: bool) -> bool:
        """置该词 is_topic（主题概念标记）。命中返回 True，无该行返回 False（供路由判 404）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE glossary SET is_topic=?, updated_at=? WHERE domain=? AND term=?",
                (1 if is_topic else 0, _now_iso(), domain, term),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_topic_concepts(self, domain: str) -> list[dict]:
        """该 domain 中标为主题概念(is_topic=1)的列表，按出现数(occurrence)降序；
        每项含 term/definition/occurrence_count/related/is_topic。空则 []。"""
        rows = self._conn.execute(
            "SELECT term, definition, occurrences, related, is_topic "
            "FROM glossary WHERE domain=? AND is_topic=1",
            (domain,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                occs = json.loads(r["occurrences"] or "[]")
            except (ValueError, TypeError):
                occs = []
            try:
                related = json.loads(r["related"] or "[]")
            except (ValueError, TypeError):
                related = []
            out.append({
                "term": r["term"],
                "definition": r["definition"] or "",
                "occurrence_count": len(occs) if isinstance(occs, list) else 0,
                "related": related if isinstance(related, list) else [],
                "is_topic": True,
            })
        out.sort(key=lambda t: t["occurrence_count"], reverse=True)
        return out

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
            "occurrences": json.loads(row["occurrences"] or "[]"),
            "related": json.loads(row["related"] or "[]"),
            "status": row["status"],
            "is_topic": bool(row["is_topic"]),
            "definition_locked": bool(row["definition_locked"]),
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
            published_at=_parse_dt(row["published_at"]),
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
            concurrency=row["concurrency"] if "concurrency" in row.keys() else 1,
            remote_addr=row["remote_addr"] if "remote_addr" in row.keys() else None,
            status=row["status"],
            admin_status=row["admin_status"] if "admin_status" in row.keys() else "",
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
