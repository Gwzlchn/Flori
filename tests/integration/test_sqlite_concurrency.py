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
from shared.study import StudyConflictError


pytestmark = pytest.mark.integration


_REVIEWED_AT = "2026-07-09T00:00:00+00:00"


def _concurrent_study_write(
    db_path: str,
    barrier,
    results,
    action: str,
    request_id: str,
) -> None:
    database: Database | None = None
    try:
        database = Database(db_path)
        database.init_schema()
        barrier.wait(timeout=10)
        if action == "review":
            card = database.record_study_review(
                request_id=request_id,
                card_id="sc_race",
                grade="good",
                expected_revision=1,
                reviewed_at=_REVIEWED_AT,
            )
            results.put(("ok", action, card["revision"], card["status"]))
        else:
            card = database.set_study_card_status(
                "sc_race", "suspended", expected_revision=1
            )
            results.put(("ok", action, card["revision"], card["status"]))
    except StudyConflictError as exc:
        results.put(("conflict", action, exc.code))
    except BaseException as exc:
        results.put(("error", action, type(exc).__name__, str(exc)))
    finally:
        if database is not None:
            database.close()


def _run_spawn_study_race(
    db_path: Path,
    operations: list[tuple[str, str]],
) -> list[tuple]:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(operations))
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_study_write,
            args=(str(db_path), barrier, results, action, request_id),
        )
        for action, request_id in operations
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail(f"SRS spawn 竞态超时: pid={process.pid}")
        assert process.exitcode == 0
    outcomes = []
    for _ in processes:
        try:
            outcomes.append(results.get(timeout=2))
        except queue.Empty:
            pytest.fail("SRS spawn 子进程没有返回结果")
    return sorted(outcomes)


def _job(job_id: str, title: str) -> Job:
    return Job(
        id=job_id,
        content_type="document",
        pipeline="document",
        document_kind="article",
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


def test_four_spawn_processes_cold_start_production_database(tmp_path: Path) -> None:
    db_path = tmp_path / "cold-start.db"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    results = context.Queue()
    processes = [
        context.Process(
            target=_cold_start_database,
            args=(str(db_path), barrier, results, worker),
        )
        for worker in ("alpha", "beta", "gamma", "delta")
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
        ("ok", "delta", SCHEMA_VERSION, "jobs_cold_delta", "冷启动 delta"),
        ("ok", "gamma", SCHEMA_VERSION, "jobs_cold_gamma", "冷启动 gamma"),
    ]

    with Database(db_path) as database:
        database.init_schema()
        assert database.get_job("jobs_cold_alpha").title == "冷启动 alpha"
        assert database.get_job("jobs_cold_beta").title == "冷启动 beta"
        assert database.get_job("jobs_cold_gamma").title == "冷启动 gamma"
        assert database.get_job("jobs_cold_delta").title == "冷启动 delta"
        assert [
            row[0]
            for row in database._conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == list(range(1, SCHEMA_VERSION + 1))


def test_one_production_database_serializes_four_thread_writers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "one-instance.db"
    with Database(db_path) as database:
        database.init_schema()
        barrier = threading.Barrier(4)

        def create(worker: str) -> tuple[str, str]:
            barrier.wait(timeout=10)
            job_id = f"jobs_thread_{worker}"
            database.create_job(_job(job_id, worker))
            return worker, database.get_job(job_id).title

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(create, worker)
                for worker in ("alpha", "beta", "gamma", "delta")
            ]
            outcomes = sorted(future.result(timeout=15) for future in futures)
        assert outcomes == [
            ("alpha", "alpha"),
            ("beta", "beta"),
            ("delta", "delta"),
            ("gamma", "gamma"),
        ]
        assert database.list_jobs(current_only=False, limit=10)[0] == 4


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


def test_two_connections_same_review_request_write_once_and_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "study-two-connections.db"
    first = Database(db_path)
    first.init_schema()
    first.create_study_card(card_id="sc_race", domain="ml", front="Q", back="A")
    second = Database(db_path)
    second.init_schema()
    barrier = threading.Barrier(2)

    def review(database: Database) -> dict:
        barrier.wait(timeout=10)
        return database.record_study_review(
            request_id="same-request",
            card_id="sc_race",
            grade="good",
            expected_revision=1,
            reviewed_at=_REVIEWED_AT,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [
                future.result(timeout=15)
                for future in (
                    executor.submit(review, first),
                    executor.submit(review, second),
                )
            ]
        assert outcomes[0] == outcomes[1]
        assert outcomes[0]["revision"] == 2
        assert first._conn.execute(
            "SELECT COUNT(*) FROM study_review_logs WHERE card_id='sc_race'"
        ).fetchone()[0] == 1
    finally:
        first.close()
        second.close()


def test_spawn_distinct_requests_on_same_revision_have_one_stale_loser(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "study-spawn-stale.db"
    with Database(db_path) as database:
        database.init_schema()
        database.create_study_card(card_id="sc_race", domain="ml", front="Q", back="A")

    outcomes = _run_spawn_study_race(
        db_path,
        [("review", "request-alpha"), ("review", "request-beta")],
    )
    assert [outcome[0] for outcome in outcomes] == ["conflict", "ok"]
    assert outcomes[0][2] == "study_revision_stale"
    with Database(db_path) as verification:
        verification.init_schema()
        assert verification.get_study_card("sc_race")["revision"] == 2
        assert verification._conn.execute(
            "SELECT COUNT(*) FROM study_review_logs WHERE card_id='sc_race'"
        ).fetchone()[0] == 1


def test_spawn_suspend_and_review_race_never_revives_or_double_writes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "study-spawn-suspend.db"
    with Database(db_path) as database:
        database.init_schema()
        database.create_study_card(card_id="sc_race", domain="ml", front="Q", back="A")

    outcomes = _run_spawn_study_race(
        db_path,
        [("review", "request-review"), ("suspend", "unused")],
    )
    assert [outcome[0] for outcome in outcomes] == ["conflict", "ok"]
    with Database(db_path) as verification:
        verification.init_schema()
        card = verification.get_study_card("sc_race")
        logs = verification._conn.execute(
            "SELECT COUNT(*) FROM study_review_logs WHERE card_id='sc_race'"
        ).fetchone()[0]
        assert card["revision"] == 2
        assert (card["status"], logs) in {("active", 1), ("suspended", 0)}
