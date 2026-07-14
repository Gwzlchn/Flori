"""telemetry 领域的显式数据库边界。"""

from __future__ import annotations

from ..db import (
    AIUsage,
    json,
    sqlite3,
)


class TelemetryRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def get_ai_task_logs(self, task_id: str) -> list[dict]:
        """读某 AI task 的白盒审计(供查看端点);最近在前。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ai_task_logs WHERE task_id=? ORDER BY id DESC", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_ai_task_log(self, task_id: str) -> dict | None:
        """返回独立 AI task 最近一条持久审计,并解析 record 供 TTL 丢失恢复."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ai_task_logs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            record = json.loads(str(value["record_json"]))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(record, dict) or record.get("task_id") != task_id:
            return None
        value["record"] = record
        return value

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
        """近窗口吞吐:since_iso 之后进入终态的 job 计数(done/failed)。用 updated_at 近似终态时刻,
        rerun 改 updated_at 会重复计入但属罕见;利用 idx_jobs_status。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT status, COUNT(*) AS n FROM jobs
                   WHERE status IN ('done','failed') AND updated_at >= ?
                   GROUP BY status""",
                (since_iso,),
            ).fetchall()
        by = {r["status"]: r["n"] for r in rows}
        return {"done": by.get("done", 0), "failed": by.get("failed", 0)}

    def record_ai_usage_in_tx(self, connection, usage: AIUsage) -> bool:
        try:
            connection.execute(
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
            return True
        except sqlite3.IntegrityError:
            return False

    def record_ai_task_log_in_tx(self, connection, log: dict) -> bool:
        """落一条独立 AI task 的白盒审计(对应 DAG 的 output/ai_logs/{step}.jsonl;AI task 无 job_dir 故入库)。
        log = 索引列(task_id/exec_id/step_name/domain/provider/model/ok/error/各 token/cost/duration/num_turns)
        + record(全量审计 dict,存进 record_json)+ created_at。best-effort,不让审计失败影响主流程。"""
        try:
            connection.execute(
                """INSERT INTO ai_task_logs
                   (task_id, exec_id, step_name, domain, provider, model, ok, error,
                    input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                    cost_usd, duration_sec, num_turns, record_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    log.get("task_id"), log.get("exec_id"), log.get("step_name"),
                    log.get("domain"), log.get("provider"), log.get("model"),
                    1 if log.get("ok", True) else 0, log.get("error"),
                    log.get("input_tokens", 0), log.get("output_tokens", 0),
                    log.get("cache_creation_input_tokens", 0), log.get("cache_read_input_tokens", 0),
                    log.get("cost_usd", 0.0), log.get("duration_sec", 0.0), log.get("num_turns", 0),
                    json.dumps(log.get("record", {}), ensure_ascii=False, default=str),
                    log.get("created_at"),
                ),
            )
            return True
        except Exception:
            return False
