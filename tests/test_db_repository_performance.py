"""量化 façade 转发相对等价 repository SQL 的延迟和语句数。"""

from __future__ import annotations

import statistics
import time

from shared.db import Database
from shared.models import Job
from shared.repositories.jobs import JobsReadRepository


def _latencies(call, iterations: int) -> list[int]:
    result: list[int] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        call()
        result.append(time.perf_counter_ns() - started)
    return result


def _percentiles(samples: list[int]) -> tuple[float, float]:
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

    facade: list[int] = []
    direct: list[int] = []
    for iteration in range(8):
        if iteration % 2:
            direct.extend(_latencies(direct_call, 250))
            facade.extend(_latencies(facade_call, 250))
        else:
            facade.extend(_latencies(facade_call, 250))
            direct.extend(_latencies(direct_call, 250))

    facade_p50, facade_p95 = _percentiles(facade)
    direct_p50, direct_p95 = _percentiles(direct)
    p50_ratio = facade_p50 / direct_p50
    p95_ratio = facade_p95 / direct_p95
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
