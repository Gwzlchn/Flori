"""验证生产 Database 在独立连接和 spawn 子进程间的事务语义。"""

from __future__ import annotations

import multiprocessing
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from shared.db import Database, SCHEMA_VERSION
from shared.models import Job


pytestmark = pytest.mark.integration


def _job(job_id: str, title: str) -> Job:
    return Job(
        id=job_id,
        content_type="article",
        pipeline="article",
        title=title,
        lineage_key=job_id,
    )


def _cold_start_database(
    db_path: str,
    barrier,
    results,
    worker: str,
) -> None:
    database: Database | None = None
    try:
        barrier.wait(timeout=10)
        database = Database(db_path)
        database.init_schema()
        job_id = f"jobs_cold_{worker}"
        database.create_job(_job(job_id, f"冷启动 {worker}"))
        persisted = database.get_job(job_id)
        results.put(
            (
                "ok",
                worker,
                database.schema_version(),
                persisted.id if persisted else None,
                persisted.title if persisted else None,
            )
        )
    except BaseException as exc:
        results.put(("error", worker, type(exc).__name__, str(exc)))
    finally:
        if database is not None:
            database.close()


def test_two_spawn_processes_cold_start_production_database(tmp_path: Path) -> None:
    db_path = tmp_path / "cold-start.db"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_cold_start_database,
            args=(str(db_path), barrier, results, worker),
        )
        for worker in ("alpha", "beta")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail(f"Database 冷启动子进程超时: pid={process.pid}")
        assert process.exitcode == 0

    outcomes = []
    for _ in processes:
        try:
            outcomes.append(results.get(timeout=2))
        except queue.Empty:
            pytest.fail("Database 冷启动子进程没有返回结果")
    assert sorted(outcomes) == [
        ("ok", "alpha", SCHEMA_VERSION, "jobs_cold_alpha", "冷启动 alpha"),
        ("ok", "beta", SCHEMA_VERSION, "jobs_cold_beta", "冷启动 beta"),
    ]

    with Database(db_path) as database:
        database.init_schema()
        assert database.get_job("jobs_cold_alpha").title == "冷启动 alpha"
        assert database.get_job("jobs_cold_beta").title == "冷启动 beta"
        assert [
            row[0]
            for row in database._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == list(range(1, SCHEMA_VERSION + 1))


def test_two_production_connections_share_commits_and_enforce_unique_job(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "connections.db"
    first = Database(db_path)
    first.init_schema()
    second = Database(db_path)
    second.init_schema()
    try:
        first.create_job(_job("jobs_visible", "跨连接可见"))
        assert second.get_job("jobs_visible").title == "跨连接可见"

        barrier = threading.Barrier(2)

        def create_same_job(database: Database, title: str) -> tuple[str, str]:
            barrier.wait(timeout=10)
            try:
                database.create_job(_job("jobs_unique", title))
            except sqlite3.IntegrityError:
                return "duplicate", title
            return "created", title

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(create_same_job, first, "first"),
                executor.submit(create_same_job, second, "second"),
            ]
            outcomes = sorted(future.result(timeout=15) for future in futures)
        assert [status for status, _title in outcomes] == ["created", "duplicate"]
    finally:
        first.close()
        second.close()

    with Database(db_path) as verification:
        verification.init_schema()
        winner = verification.get_job("jobs_unique")
        assert winner is not None
        assert winner.title in {"first", "second"}
        total, jobs = verification.list_jobs(current_only=False, limit=10)
        assert total == 2
        assert {job.id for job in jobs} == {"jobs_visible", "jobs_unique"}
