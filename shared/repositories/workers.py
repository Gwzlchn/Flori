"""workers 领域的显式数据库边界。"""

from __future__ import annotations

from ..db import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    STALE,
    Step,
    StepStatus,
    Worker,
    _parse_dt,
    compute_worker_status,
    datetime,
    json,
    timezone,
)


class WorkersRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def get_worker_desired_config(self, worker_id: str) -> tuple[dict | None, int]:
        """读中心期望配置;(None, 0) = 未配置/worker 不存在(worker 端视为尊重自报)。"""
        row = self._conn.execute(
            "SELECT desired_config, cfg_rev FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if row is None or not row["desired_config"]:
            return None, (row["cfg_rev"] or 0) if row else 0
        try:
            return json.loads(row["desired_config"]), row["cfg_rev"] or 0
        except (ValueError, TypeError):
            return None, row["cfg_rev"] or 0

    def list_running_steps(self) -> list[Step]:
        """所有 status=running 的 step(= 正在执行的 task),按开始时间倒序。
        队列页「运行中」分组的权威来源:step 行自带 pool/worker_id/started_at,无需依赖 worker 心跳派生。"""
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE status=? ORDER BY started_at DESC",
            (StepStatus.RUNNING.value,),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def list_worker_tasks(self, worker_id: str, limit: int = 50) -> list[Step]:
        """该 worker 的 task 执行历史(task = 某作业的某步骤的一次执行,按最近开始时间倒序;每条 = 一个 step 记录)。"""
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE worker_id=? "
            "ORDER BY started_at DESC LIMIT ?",
            (worker_id, limit),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def get_worker_token_by_hash(self, token_hash: str) -> dict | None:
        """按 token hash 查 token 行,未命中返回 None;revoked 折算成 bool。"""
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

    def upsert_worker_in_tx(self, connection, worker: Worker) -> None:
        # ON CONFLICT DO UPDATE 而非 INSERT OR REPLACE:REPLACE 是整行删重建,会把不在
        # 列清单里的中心配置列(desired_config/cfg_rev)清零——worker 每次重注册都会走到
        # 这里,页面下发的配置绝不能被重启冲掉。
        connection.execute(
            """INSERT INTO workers
               (id, type, pools, tags, reject_tags, hostname, gpu_name,
                gpu_memory_mb, concurrency, remote_addr, status, admin_status,
                current_job, current_step,
                tasks_completed, tasks_failed, total_duration_sec,
                first_seen, started_at, last_heartbeat, admin_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 type=excluded.type, pools=excluded.pools, tags=excluded.tags,
                 reject_tags=excluded.reject_tags, hostname=excluded.hostname,
                 gpu_name=excluded.gpu_name, gpu_memory_mb=excluded.gpu_memory_mb,
                 concurrency=excluded.concurrency, remote_addr=excluded.remote_addr,
                 status=excluded.status, admin_status=excluded.admin_status,
                 current_job=excluded.current_job, current_step=excluded.current_step,
                 tasks_completed=excluded.tasks_completed, tasks_failed=excluded.tasks_failed,
                 total_duration_sec=excluded.total_duration_sec,
                 first_seen=excluded.first_seen, started_at=excluded.started_at,
                 last_heartbeat=excluded.last_heartbeat, admin_note=excluded.admin_note""",
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

    def get_worker_in_tx(
        self,
        connection,
        worker_id: str,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> Worker | None:
        row = connection.execute(
            "SELECT * FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if row is None:
            return None
        w = self._row_to_worker(row)
        self._apply_status(w, online_window_sec, stale_window_sec)
        return w

    def list_workers_in_tx(
        self,
        connection,
        online_window_sec: int = DEFAULT_ONLINE_WINDOW_SEC,
        stale_window_sec: int = DEFAULT_STALE_WINDOW_SEC,
    ) -> list[Worker]:
        """列出所有 worker,状态由后端按心跳新鲜度统一算出(online-idle/busy、
        offline、stale,paused 为管理员叠加)。越过 stale 窗口的持久化为信号,
        供 GC 回收僵尸 worker。"""
        rows = connection.execute("SELECT * FROM workers").fetchall()
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
        """把 worker 的存量字段折算成对外公共状态,并对 stale 持久化(不动心跳)。
        管理员叠加位(paused)来自独立的 admin_status 列;运行时 status 列只供 busy/idle + GC。"""
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

    def set_worker_status_in_tx(self, connection, worker_id: str, status: str) -> None:
        """仅更新 worker 状态,不触碰 last_heartbeat(用于标记僵尸为 offline)。"""
        connection.execute(
            "UPDATE workers SET status=? WHERE id=?", (status, worker_id),
        )

    def set_worker_admin_status_in_tx(self, connection, worker_id: str, admin_status: str) -> None:
        """仅更新管理员暂停叠加位("" / "paused"),不触碰运行时 status / 心跳。"""
        connection.execute(
            "UPDATE workers SET admin_status=? WHERE id=?",
            (admin_status, worker_id),
        )

    def increment_worker_stats_in_tx(
        self,
        connection,
        worker_id: str,
        completed: int = 0,
        failed: int = 0,
        duration: float = 0.0,
    ) -> None:
        connection.execute(
            """UPDATE workers SET
               tasks_completed = tasks_completed + ?,
               tasks_failed = tasks_failed + ?,
               total_duration_sec = total_duration_sec + ?
               WHERE id=?""",
            (completed, failed, duration, worker_id),
        )

    def set_worker_desired_config_in_tx(self, connection, worker_id: str, config: dict) -> int:
        """写中心期望配置并 cfg_rev+1(单调);返回新 rev,worker 不存在返回 -1。
        config 只存显式指定的键(pools/concurrency/tags/reject_tags),worker 端按键应用。"""
        cur = connection.execute(
            "SELECT cfg_rev FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        if cur is None:
            return -1
        rev = (cur["cfg_rev"] or 0) + 1
        connection.execute(
            "UPDATE workers SET desired_config=?, cfg_rev=? WHERE id=?",
            (json.dumps(config), rev, worker_id),
        )
        return rev

    def update_worker_heartbeat_in_tx(
        self,
        connection,
        worker_id: str,
        status: str | None = None,
        current_job: str | None = None,
        current_step: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        """刷新 worker 在 DB 中的 last_heartbeat(及可选的 status / 当前任务)。

        心跳与状态变更必须写回 DB,否则 /api/workers 读到的 last_heartbeat
        永远停在注册时刻,前端会在 30s 后把所有 worker 判成 offline。"""
        fields = {"last_heartbeat": datetime.now(timezone.utc).isoformat()}
        if status is not None:
            fields["status"] = status
        if current_job is not None:
            fields["current_job"] = current_job or None
        if current_step is not None:
            fields["current_step"] = current_step or None
        if concurrency is not None:
            fields["concurrency"] = max(1, int(concurrency))
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [worker_id]
        connection.execute(
            f"UPDATE workers SET {set_clause} WHERE id=?",
            values,
        )

    def delete_worker_in_tx(self, connection, worker_id: str) -> None:
        connection.execute("DELETE FROM workers WHERE id=?", (worker_id,))

    def upsert_worker_token_in_tx(
        self,
        connection,
        token_hash: str,
        worker_id: str,
        pools: list[str],
        tags: list[str],
        created_at: datetime,
        revoked: bool = False,
        revoke_existing: bool = False,
    ) -> None:
        """登记一枚 per-worker token(仅存 sha256 hash),pools/tags 限定其授权范围。

        revoke_existing=True 用于首次 bootstrap/recreate,先吊销该 worker 旧 token,保证同一
        worker 同时只有一枚 active token。"""
        if revoke_existing:
            connection.execute(
                "UPDATE worker_tokens SET revoked=1 WHERE worker_id=?",
                (worker_id,),
            )
        connection.execute(
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

    def revoke_worker_token_in_tx(self, connection, worker_id: str) -> None:
        """吊销某 worker 名下全部 token(删 worker 时连带,使其心跳/认领立即 401)。"""
        connection.execute(
            "UPDATE worker_tokens SET revoked=1 WHERE worker_id=?", (worker_id,)
        )
