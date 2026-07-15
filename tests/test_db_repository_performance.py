"""量化 façade 转发相对等价 repository SQL 的延迟和语句数。"""

from __future__ import annotations

import statistics
import time

from shared.db import Database
from shared.models import Job
from shared.repositories.jobs import JobsReadRepository


def _batch_cpu_latency(call, iterations: int) -> float:
    started = time.thread_time_ns()
    for _ in range(iterations):
        call()
    return (time.thread_time_ns() - started) / iterations


def _percentiles(samples: list[float]) -> tuple[float, float]:
    return statistics.median(samples), statistics.quantiles(
        samples, n=100, method="inclusive"
    )[94]


def test_job_read_facade_p50_and_p95_stay_within_fifteen_percent(
    db: Database,
) -> None:
    job = Job(
        id="jobs_repository_perf",
        content_type="article",
        pipeline="article",
        title="repository performance",
    )
    db.create_job(job)
    facade_call = lambda: db.get_job(job.id)
    direct_call = lambda: JobsReadRepository.get_job(db, job.id)
    for _ in range(100):
        facade_call()
        direct_call()

    # façade 只增加 Python 转发，线程 CPU 时间排除同 runner 进程的调度抢占。
    # 每轮相邻测量后直接取 ratio，避免两个独立 p95 来自不同负载窗口。
    ratios: list[float] = []
    for iteration in range(40):
        if iteration % 2:
            direct = _batch_cpu_latency(direct_call, 200)
            facade = _batch_cpu_latency(facade_call, 200)
        else:
            facade = _batch_cpu_latency(facade_call, 200)
            direct = _batch_cpu_latency(direct_call, 200)
        ratios.append(facade / direct)

    p50_ratio, p95_ratio = _percentiles(ratios)
    print(
        "repository get_job latency: "
        f"p50={p50_ratio:.4f}x, p95={p95_ratio:.4f}x"
    )
    assert p50_ratio <= 1.15
    assert p95_ratio <= 1.15


def test_job_read_facade_preserves_sql_statement_count(db: Database) -> None:
    job = Job(
        id="jobs_repository_trace",
        content_type="article",
        pipeline="article",
        title="repository trace",
    )
    db.create_job(job)

    def trace(call) -> list[str]:
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            call()
        finally:
            db._conn.set_trace_callback(None)
        return statements

    facade = trace(lambda: db.get_job(job.id))
    direct = trace(lambda: JobsReadRepository.get_job(db, job.id))
    assert facade == direct
    assert len(facade) == 1


def test_job_read_uses_primary_key_query_plan(db: Database) -> None:
    plan = db._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE id=?", ("missing",)
    ).fetchall()
    detail = "\n".join(str(row[3]) for row in plan)
    assert "SEARCH jobs USING INDEX sqlite_autoindex_jobs_1 (id=?)" in detail
