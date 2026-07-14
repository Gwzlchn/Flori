"""证据型学习建议的快照,幂等,事务和 API 边界."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import shared.db as db_module
from shared.db import Database
from shared.migrations import v0006_concept_definition_history as migration_current
from shared.models import Collection, Job, JobStatus
from shared.study_suggestions import (
    StudySuggestionConflictError,
    canonical_json,
    content_fingerprint,
    knowledge_fingerprint,
    payload_fingerprint,
    parse_ai_suggestions,
    validate_operation_items,
)


UTC = timezone.utc


def _seed(
    db,
    *,
    job_id: str = "jobs_study_1",
    domain: str = "ml",
    body: str = "## 反向传播\n\n反向传播通过链式法则高效计算梯度。",
    concept: str = "反向传播",
    lineage_key: str | None = None,
    collection_id: str | None = None,
) -> None:
    db.create_job(
        Job(
            id=job_id,
            content_type="article",
            pipeline="article",
            status=JobStatus.DONE,
            title=f"title-{job_id}",
            domain=domain,
            lineage_key=lineage_key,
            collection_id=collection_id,
        )
    )
    db.index_job_notes(
        job_id,
        "smart",
        f"title-{job_id}",
        body,
        content_type="article",
        domain=domain,
    )
    db.upsert_glossary_term(domain, concept, "用链式法则求梯度", status="accepted")


def _queued(
    db,
    *,
    request_id: str = "batch-create-1",
    job_id: str = "jobs_study_1",
    domain: str = "ml",
    concept: str | None = "反向传播",
    knowledge_key: str = "backprop-gradient",
    front: str = "反向传播解决什么问题?",
) -> tuple[dict, dict]:
    batch = db.create_study_suggestion_batch(
        request_id=request_id,
        domain=domain,
        job_ids=[job_id],
        concept_terms=[concept] if concept else [],
        max_cards=5,
    )
    queued = db.mark_study_suggestion_batch_queued(
        batch["batch_id"],
        task_id=batch["task_id"],
        expected_revision=batch["revision"],
    )
    evidence = batch["llm_request"]["evidence"][0]
    quote = next(
        line.strip() for line in reversed(evidence["untrusted_body"].splitlines())
        if line.strip()
    )
    concept_input = batch["llm_request"]["concepts"]
    result = {
        "schema_version": 1,
        "suggestions": [
            {
                "knowledge_key": knowledge_key,
                "concept_input_id": concept_input[0]["input_id"] if concept_input else None,
                "card_type": "basic",
                "front": front,
                "back": "它从输出误差向前高效计算各层梯度。",
                "explanation": "核心是链式法则。",
                "evidence": [
                    {
                        "evidence_id": evidence["evidence_id"],
                        "quote": quote,
                    }
                ],
            }
        ],
    }
    assert queued["status"] == "queued"
    return batch, result


def _ready(
    db,
    *,
    request_id: str = "batch-create-1",
    job_id: str = "jobs_study_1",
    domain: str = "ml",
    concept: str | None = "反向传播",
    knowledge_key: str = "backprop-gradient",
    front: str = "反向传播解决什么问题?",
) -> tuple[dict, dict]:
    batch, result = _queued(
        db,
        request_id=request_id,
        job_id=job_id,
        domain=domain,
        concept=concept,
        knowledge_key=knowledge_key,
        front=front,
    )
    suggestions = db.materialize_study_suggestions(
        batch["batch_id"], task_id=batch["task_id"], result=result
    )
    return db.get_study_suggestion_batch(batch["batch_id"]), suggestions[0]


def _accept(db, batch: dict, suggestion: dict, request_id: str = "accept-1") -> dict:
    return db.apply_study_suggestion_operations(
        request_id=request_id,
        batch_id=batch["batch_id"],
        items=[
            {
                "suggestion_id": suggestion["suggestion_id"],
                "expected_revision": suggestion["revision"],
                "action": "accept",
                "patch": {},
            }
        ],
    )


def _guarded_update(db, trigger_name: str, sql: str, params: tuple = ()) -> None:
    """模拟磁盘/离线篡改后恢复原 trigger,让 validator 只审计数据语义."""
    trigger_sql = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()[0]
    db._conn.execute(f"DROP TRIGGER {trigger_name}")
    db._conn.execute(sql, params)
    db._conn.execute(trigger_sql)
    db._conn.commit()


def _guarded_delete(db, sql: str, params: tuple = ()) -> None:
    """模拟离线删除 operation 后恢复 no-delete trigger."""
    trigger_sql = db._conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type='trigger' AND name='study_suggestion_operation_no_delete'"""
    ).fetchone()[0]
    db._conn.execute("DROP TRIGGER study_suggestion_operation_no_delete")
    db._conn.execute(sql, params)
    db._conn.execute(trigger_sql)
    db._conn.commit()


def _lifecycle_request_id(request: dict) -> str:
    identity = {
        key: request[key]
        for key in (
            "operation_kind",
            "batch_id",
            "task_id",
            "attempt",
            "expected_revision",
        )
    }
    return f"study-lifecycle:{request['operation_kind']}:{payload_fingerprint(identity)}"


class TestStudySuggestionDb:
    def test_batch_snapshot_create_is_idempotent_and_request_key_is_global(self, db):
        _seed(db)
        first = db.create_study_suggestion_batch(
            request_id="create-one", domain="ml", job_ids=["jobs_study_1"]
        )
        replay = db.create_study_suggestion_batch(
            request_id="create-one", domain="ml", job_ids=["jobs_study_1"]
        )
        same_input = db.create_study_suggestion_batch(
            request_id="create-two", domain="ml", job_ids=["jobs_study_1"]
        )

        assert replay == first
        assert same_input["batch_id"] == first["batch_id"]
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_batches"
        ).fetchone()[0] == 1
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0] == 2
        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.create_study_suggestion_batch(
                request_id="create-one",
                domain="ml",
                job_ids=["jobs_study_1"],
                max_cards=11,
            )
        assert conflict.value.code == "study_suggestion_request_id_conflict"

    def test_batch_create_request_fingerprint_includes_deadline(self, db):
        _seed(db)
        db.create_study_suggestion_batch(
            request_id="deadline-key",
            domain="ml",
            job_ids=["jobs_study_1"],
            deadline_seconds=600,
        )

        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.create_study_suggestion_batch(
                request_id="deadline-key",
                domain="ml",
                job_ids=["jobs_study_1"],
                deadline_seconds=601,
            )
        assert conflict.value.code == "study_suggestion_request_id_conflict"

    def test_batch_rejects_cross_domain_note_chunk_even_when_job_matches(self, db):
        _seed(db)
        db._conn.execute(
            "UPDATE note_chunks SET domain='other' WHERE job_id='jobs_study_1'"
        )
        db._conn.commit()

        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.create_study_suggestion_batch(
                request_id="cross-domain-chunk",
                domain="ml",
                job_ids=["jobs_study_1"],
            )

        assert conflict.value.code == "study_suggestion_chunk_domain_mismatch"
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_batches"
        ).fetchone()[0] == 0

    def test_batch_failure_requires_nonblank_error_and_retry_gets_new_task(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="create-failure", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        with pytest.raises(ValueError, match="error_message"):
            db.fail_study_suggestion_batch(
                batch["batch_id"],
                task_id=batch["task_id"],
                expected_revision=queued["revision"],
                error_code="timeout",
                error_message="   ",
            )
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        retried = db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="retry-failure",
            expected_revision=failed["revision"],
        )

        assert failed["status"] == "failed"
        assert retried["status"] == "pending_enqueue"
        assert retried["attempt"] == 2
        assert retried["task_id"] != batch["task_id"]
        assert retried["error_code"] is None

    def test_failed_batch_replay_requires_exact_failure_payload(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="create-failure-replay", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        assert db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        ) == failed

        for changed in (
            {"error_message": "different"},
            {"error_code": "provider_error"},
            {"expected_revision": failed["revision"]},
            {"task_id": "different-task"},
        ):
            payload = {
                "task_id": batch["task_id"],
                "expected_revision": queued["revision"],
                "error_code": "timeout",
                "error_message": "worker timed out",
                **changed,
            }
            with pytest.raises(StudySuggestionConflictError) as conflict:
                db.fail_study_suggestion_batch(batch["batch_id"], **payload)
            assert conflict.value.code == "study_suggestion_failure_conflict"

    def test_batch_lifecycle_operations_are_canonical_and_idempotent(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="lifecycle-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        assert db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        ) == queued
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        assert db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        ) == failed

        rows = db._conn.execute(
            """SELECT request_id, request_fingerprint, operation_kind,
                      request_json, outcome_json, created_at
               FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind IN ('batch_queued','batch_failed')
               ORDER BY ledger_seq""",
            (batch["batch_id"],),
        ).fetchall()
        assert [row["operation_kind"] for row in rows] == [
            "batch_queued",
            "batch_failed",
        ]
        for row, expected in zip(rows, (queued, failed)):
            request = json.loads(row["request_json"])
            assert row["request_json"] == canonical_json(request)
            assert row["request_fingerprint"] == payload_fingerprint(request)
            assert request["request_id"] == row["request_id"]
            assert request["batch_id"] == batch["batch_id"]
            assert request["task_id"] == batch["task_id"]
            assert request["attempt"] == 1
            assert json.loads(row["outcome_json"]) == expected
            assert expected["updated_at"] == row["created_at"]
        for changed in (
            {"task_id": "study-suggestions:different-task", "expected_revision": 1},
            {"task_id": batch["task_id"], "expected_revision": 2},
        ):
            with pytest.raises(StudySuggestionConflictError):
                db.mark_study_suggestion_batch_queued(batch["batch_id"], **changed)

    def test_ready_lifecycle_binds_result_materialization_and_replay_conflict(self, db):
        _seed(db)
        batch, result = _queued(db, request_id="ready-lifecycle-create")
        first = db.materialize_study_suggestions(
            batch["batch_id"], task_id=batch["task_id"], result=result
        )
        replay = db.materialize_study_suggestions(
            batch["batch_id"], task_id=batch["task_id"], result=result
        )
        assert replay == first
        row = db._conn.execute(
            """SELECT request_id, request_fingerprint, request_json,
                      outcome_json, created_at
               FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='batch_ready'""",
            (batch["batch_id"],),
        ).fetchone()
        request = json.loads(row["request_json"])
        outcome = json.loads(row["outcome_json"])
        assert request["request_id"] == row["request_id"] == _lifecycle_request_id(request)
        assert request["result_sha256"] == payload_fingerprint(result)
        assert row["request_fingerprint"] == payload_fingerprint(request)
        assert outcome["result"] == result
        assert outcome["updated_at"] == row["created_at"]
        assert db._conn.execute(
            """SELECT COUNT(*) FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='batch_ready'""",
            (batch["batch_id"],),
        ).fetchone()[0] == 1

        changed = json.loads(canonical_json(result))
        changed["suggestions"][0]["front"] = "different question"
        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.materialize_study_suggestions(
                batch["batch_id"], task_id=batch["task_id"], result=changed
            )
        assert conflict.value.code == "study_suggestion_result_conflict"

    @pytest.mark.parametrize("status", ["queued", "failed", "ready"])
    def test_lifecycle_replay_remains_idempotent_after_domain_rename(self, db, status):
        _seed(db)
        batch, result = _queued(db, request_id=f"identity-replay-{status}")
        if status == "failed":
            db.fail_study_suggestion_batch(
                batch["batch_id"],
                task_id=batch["task_id"],
                expected_revision=2,
                error_code="timeout",
                error_message="worker timed out",
            )
        elif status == "ready":
            db.materialize_study_suggestions(
                batch["batch_id"], task_id=batch["task_id"], result=result
            )
        db.rename_domain("ml", "machine-learning")

        if status == "queued":
            replay = db.mark_study_suggestion_batch_queued(
                batch["batch_id"], task_id=batch["task_id"], expected_revision=1
            )
            assert replay["domain"] == "machine-learning"
        elif status == "failed":
            replay = db.fail_study_suggestion_batch(
                batch["batch_id"],
                task_id=batch["task_id"],
                expected_revision=2,
                error_code="timeout",
                error_message="worker timed out",
            )
            assert replay["domain"] == "machine-learning"
        else:
            replay = db.materialize_study_suggestions(
                batch["batch_id"], task_id=batch["task_id"], result=result
            )
            assert replay[0]["domain"] == "machine-learning"
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("transition", ["domain", "concept"])
    def test_current_validator_replays_identity_transition_before_ready(self, db, transition):
        _seed(db, concept="concept-a")
        if transition == "concept":
            db.upsert_glossary_term("ml", "concept-b", "merged", status="accepted")
        batch, result = _queued(
            db,
            request_id=f"identity-before-ready-{transition}",
            concept="concept-a",
        )
        if transition == "domain":
            db.rename_domain("ml", "machine-learning")
        else:
            db.merge_glossary_terms("ml", "concept-a", "concept-b")
        db.materialize_study_suggestions(
            batch["batch_id"], task_id=batch["task_id"], result=result
        )

        migration_current.validate(db._conn)

    def test_lifecycle_replay_rejects_unledgered_identity_drift(self, db):
        _seed(db)
        batch, _result = _queued(db, request_id="unledgered-identity")
        db._conn.execute(
            """UPDATE study_suggestion_batches
               SET domain='forged-domain', updated_at='2100-01-01T00:00:00+00:00'
               WHERE batch_id=?""",
            (batch["batch_id"],),
        )
        db._conn.commit()

        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.mark_study_suggestion_batch_queued(
                batch["batch_id"], task_id=batch["task_id"], expected_revision=1
            )
        assert conflict.value.code == "study_suggestion_lifecycle_conflict"

    def test_lifecycle_timestamp_is_taken_after_lock_and_previous_transition(
        self, db, monkeypatch
    ):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="lifecycle-clock-create", domain="ml", job_ids=["jobs_study_1"]
        )
        base = datetime.fromisoformat(batch["updated_at"])
        timestamps = iter(
            [
                (base + timedelta(microseconds=1)).isoformat(),
                (base + timedelta(microseconds=2)).isoformat(),
            ]
        )
        clock_calls: list[str] = []

        def ordered_now() -> str:
            value = next(timestamps)
            clock_calls.append(value)
            return value

        monkeypatch.setattr(db_module, "_now_iso", ordered_now)
        underlying = threading.RLock()
        worker_attempted = threading.Event()
        main_thread = threading.get_ident()

        class BarrierLock:
            def __enter__(self):
                if threading.get_ident() != main_thread:
                    worker_attempted.set()
                underlying.acquire()
                return self

            def __exit__(self, exc_type, exc, traceback):
                underlying.release()

        db._lock = BarrierLock()
        failures: list[BaseException] = []

        def fail_after_queue() -> None:
            try:
                db.fail_study_suggestion_batch(
                    batch["batch_id"],
                    task_id=batch["task_id"],
                    expected_revision=2,
                    error_code="timeout",
                    error_message="worker timed out",
                )
            except BaseException as exc:
                failures.append(exc)

        with db._lock:
            thread = threading.Thread(target=fail_after_queue)
            thread.start()
            assert worker_attempted.wait(timeout=2)
            assert clock_calls == []
            db.mark_study_suggestion_batch_queued(
                batch["batch_id"], task_id=batch["task_id"], expected_revision=1
            )
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert failures == []
        assert clock_calls == sorted(clock_calls) and len(clock_calls) == 2
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("transition", ["queued", "failed", "ready", "retry"])
    def test_lifecycle_clock_is_not_read_before_transaction_lock(
        self, db, monkeypatch, transition
    ):
        _seed(db)
        if transition == "queued":
            batch = db.create_study_suggestion_batch(
                request_id="clock-lock-queued",
                domain="ml",
                job_ids=["jobs_study_1"],
            )

            def action():
                return db.mark_study_suggestion_batch_queued(
                    batch["batch_id"], task_id=batch["task_id"], expected_revision=1
                )

        else:
            batch, result = _queued(db, request_id=f"clock-lock-{transition}")
            if transition == "failed":

                def action():
                    return db.fail_study_suggestion_batch(
                        batch["batch_id"],
                        task_id=batch["task_id"],
                        expected_revision=2,
                        error_code="timeout",
                        error_message="worker timed out",
                    )

            elif transition == "ready":

                def action():
                    return db.materialize_study_suggestions(
                        batch["batch_id"], task_id=batch["task_id"], result=result
                    )

            else:
                failed = db.fail_study_suggestion_batch(
                    batch["batch_id"],
                    task_id=batch["task_id"],
                    expected_revision=2,
                    error_code="timeout",
                    error_message="worker timed out",
                )

                def action():
                    return db.retry_study_suggestion_batch(
                        batch["batch_id"],
                        request_id="clock-lock-retry-operation",
                        expected_revision=failed["revision"],
                    )

        current = db.get_study_suggestion_batch(batch["batch_id"])
        next_time = datetime.fromisoformat(current["updated_at"]) + timedelta(
            microseconds=1
        )
        clock_called = threading.Event()

        def iso_clock() -> str:
            clock_called.set()
            return next_time.isoformat()

        def datetime_clock() -> datetime:
            clock_called.set()
            return next_time

        monkeypatch.setattr(
            db_module,
            "utc_now" if transition == "retry" else "_now_iso",
            datetime_clock if transition == "retry" else iso_clock,
        )
        underlying = threading.RLock()
        worker_attempted = threading.Event()
        main_thread = threading.get_ident()

        class BarrierLock:
            def __enter__(self):
                if threading.get_ident() != main_thread:
                    worker_attempted.set()
                underlying.acquire()
                return self

            def __exit__(self, exc_type, exc, traceback):
                underlying.release()

        db._lock = BarrierLock()
        failures: list[BaseException] = []

        def run_transition() -> None:
            try:
                action()
            except BaseException as exc:
                failures.append(exc)

        with db._lock:
            thread = threading.Thread(target=run_transition)
            thread.start()
            assert worker_attempted.wait(timeout=2)
            assert not clock_called.is_set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert failures == []
        assert clock_called.is_set()
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("operation_kind", ["batch_queued", "batch_failed", "batch_ready"])
    def test_lifecycle_operation_write_failure_rolls_back_whole_transition(
        self, db, operation_kind
    ):
        _seed(db)
        if operation_kind == "batch_queued":
            batch = db.create_study_suggestion_batch(
                request_id=f"rollback-{operation_kind}",
                domain="ml",
                job_ids=["jobs_study_1"],
            )
            result = None
        else:
            batch, result = _queued(db, request_id=f"rollback-{operation_kind}")
        db._conn.execute(
            f"""CREATE TRIGGER fail_{operation_kind}_operation
                BEFORE INSERT ON study_suggestion_operations
                WHEN NEW.operation_kind='{operation_kind}'
                BEGIN SELECT RAISE(ABORT, 'lifecycle operation fault'); END"""
        )
        db._conn.commit()

        with pytest.raises((sqlite3.IntegrityError, StudySuggestionConflictError)):
            if operation_kind == "batch_queued":
                db.mark_study_suggestion_batch_queued(
                    batch["batch_id"], task_id=batch["task_id"], expected_revision=1
                )
            elif operation_kind == "batch_failed":
                db.fail_study_suggestion_batch(
                    batch["batch_id"],
                    task_id=batch["task_id"],
                    expected_revision=2,
                    error_code="timeout",
                    error_message="worker timed out",
                )
            else:
                db.materialize_study_suggestions(
                    batch["batch_id"], task_id=batch["task_id"], result=result
                )

        current = db.get_study_suggestion_batch(batch["batch_id"])
        expected_status = "pending_enqueue" if operation_kind == "batch_queued" else "queued"
        expected_revision = 1 if operation_kind == "batch_queued" else 2
        assert (current["status"], current["revision"]) == (
            expected_status,
            expected_revision,
        )
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations WHERE operation_kind=?",
            (operation_kind,),
        ).fetchone()[0] == 0
        if operation_kind == "batch_ready":
            assert db._conn.execute(
                "SELECT COUNT(*) FROM study_suggestions WHERE batch_id=?",
                (batch["batch_id"],),
            ).fetchone()[0] == 0
            assert db._conn.execute(
                "SELECT COUNT(*) FROM study_suggestion_evidence_links WHERE batch_id=?",
                (batch["batch_id"],),
            ).fetchone()[0] == 0

    def test_materialize_accept_due_review_mastery_and_replay(self, db):
        _seed(db)
        batch, suggestion = _ready(db)
        assert batch["status"] == "ready"
        assert suggestion["status"] == "suggested"
        accepted = _accept(db, batch, suggestion)
        replay = _accept(db, batch, suggestion)

        assert replay == accepted
        card = accepted["cards"][0]
        assert card["status"] == "active"
        assert card["review"] is not None
        assert db.list_due_study_cards(domain="ml")[0] == 1
        reviewed = db.record_study_review(
            request_id="review-auto-card",
            card_id=card["card_id"],
            grade="good",
            expected_revision=1,
            reviewed_at=datetime(2026, 7, 14, tzinfo=UTC),
        )
        assert reviewed["revision"] == 2
        assert db.get_study_mastery(domain="ml") == {
            "total": 1,
            "items": [
                {
                    "domain": "ml",
                    "concept_term": "反向传播",
                    "score": 80,
                    "level": "learning",
                    "reviewed_cards": 1,
                    "reviews_total": 1,
                    "last_reviewed_at": "2026-07-14T00:00:00+00:00",
                }
            ],
        }

    @pytest.mark.parametrize(
        "fault_prefix",
        ["after_card", "after_due", "after_suggestion", "after_operation", "before_commit"],
    )
    def test_accept_fault_injection_rolls_back_every_write(self, db, fault_prefix):
        _seed(db)
        batch, suggestion = _ready(db)

        def fail(stage: str) -> None:
            if stage.startswith(fault_prefix):
                raise RuntimeError(f"fault:{stage}")

        with pytest.raises(RuntimeError, match="fault"):
            db.apply_study_suggestion_operations(
                request_id=f"fault-{fault_prefix}",
                batch_id=batch["batch_id"],
                items=[
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "expected_revision": 1,
                        "action": "accept",
                        "patch": {},
                    }
                ],
                fault_injector=fail,
            )
        assert db.get_study_suggestion(suggestion["suggestion_id"])["status"] == "suggested"
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_cards WHERE source LIKE 'suggestion:%'"
        ).fetchone()[0] == 0
        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations WHERE operation_kind='suggestion_review'"
        ).fetchone()[0] == 0
        assert not db._conn.in_transaction

    def test_batch_operation_is_all_or_nothing_on_stale_second_item(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="batch-two", domain="ml", job_ids=["jobs_study_1"], max_cards=5
        )
        db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        ev = batch["llm_request"]["evidence"][0]
        result = {
            "schema_version": 1,
            "suggestions": [
                {
                    "knowledge_key": key,
                    "concept_input_id": None,
                    "card_type": "basic",
                    "front": front,
                    "back": "A",
                    "explanation": "",
                    "evidence": [{"evidence_id": ev["evidence_id"], "quote": "链式法则"}],
                }
                for key, front in (("key-a", "Q1"), ("key-b", "Q2"))
            ],
        }
        suggestions = db.materialize_study_suggestions(
            batch["batch_id"], task_id=batch["task_id"], result=result
        )
        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.apply_study_suggestion_operations(
                request_id="batch-atomic",
                batch_id=batch["batch_id"],
                items=[
                    {
                        "suggestion_id": suggestions[0]["suggestion_id"],
                        "expected_revision": 1,
                        "action": "reject",
                        "patch": {},
                        "reason": "not useful",
                    },
                    {
                        "suggestion_id": suggestions[1]["suggestion_id"],
                        "expected_revision": 2,
                        "action": "accept",
                        "patch": {},
                    },
                ],
            )
        assert conflict.value.code == "study_suggestion_revision_stale"
        assert all(
            db.get_study_suggestion(item["suggestion_id"])["status"] == "suggested"
            for item in suggestions
        )

    def test_chunk_change_blocks_accept_and_job_delete_preserves_audit(self, db):
        _seed(db)
        batch, suggestion = _ready(db)
        db.index_job_notes(
            "jobs_study_1",
            "smart",
            "changed",
            "## 新版\n\n这段正文已经改变。",
            content_type="article",
            domain="ml",
        )
        current = db.get_study_suggestion(suggestion["suggestion_id"])
        assert current["evidence"][0]["status"] == "stale"
        with pytest.raises(StudySuggestionConflictError) as stale:
            _accept(db, batch, current)
        assert stale.value.code == "study_suggestion_evidence_unavailable"

        db.delete_job_cascade("jobs_study_1")
        after_delete = db.get_study_suggestion(suggestion["suggestion_id"])
        assert after_delete["evidence"][0]["status"] == "unavailable"
        assert after_delete["evidence"][0]["invalid_reason"] == "job_deleted"

    def test_collection_purge_preserves_suggestion_audit_and_detaches_cards(self, db):
        db.create_collection(Collection(id="study-purge", name="study", domain="ml"))
        _seed(db, collection_id="study-purge")
        batch, suggestion = _ready(db)
        accepted = _accept(db, batch, suggestion)["cards"][0]
        manual = db.create_study_card(
            card_id="manual-source-card",
            domain="ml",
            job_id="jobs_study_1",
            front="manual question",
            back="manual answer",
        )

        db.delete_collection("study-purge", purge=True)

        evidence = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert (evidence["status"], evidence["invalid_reason"]) == (
            "unavailable",
            "job_deleted",
        )
        assert db.get_study_card(accepted["card_id"])["job_id"] is None
        assert db.get_study_card(manual["card_id"])["job_id"] is None
        assert db.get_job("jobs_study_1") is None
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("delete_mode", ["job", "collection"])
    def test_source_delete_fault_rolls_back_evidence_cards_and_job(
        self, db, delete_mode
    ):
        collection_id = "study-fault" if delete_mode == "collection" else None
        if collection_id:
            db.create_collection(Collection(id=collection_id, name="study", domain="ml"))
        _seed(db, collection_id=collection_id)
        batch, suggestion = _ready(db)
        card = _accept(db, batch, suggestion)["cards"][0]
        db._conn.execute(
            """CREATE TRIGGER fail_study_source_delete BEFORE DELETE ON jobs
               WHEN OLD.id='jobs_study_1'
               BEGIN SELECT RAISE(ABORT, 'fault after study detach'); END"""
        )
        db._conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="fault after study detach"):
            if delete_mode == "job":
                db.delete_job_cascade("jobs_study_1")
            else:
                db.delete_collection("study-fault", purge=True)

        assert not db._conn.in_transaction
        assert db.get_job("jobs_study_1") is not None
        evidence = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert (evidence["status"], evidence["invalid_reason"]) == ("valid", None)
        assert db.get_study_card(card["card_id"])["job_id"] == "jobs_study_1"
        if collection_id:
            assert db.get_collection(collection_id) is not None

    def test_superseded_lineage_blocks_materialize_even_with_unchanged_chunk(self, db):
        _seed(db, job_id="source-v1", lineage_key="same-source")
        batch, result = _queued(db, job_id="source-v1")

        db.create_job(Job(
            id="source-v2",
            content_type="article",
            pipeline="article",
            status=JobStatus.DONE,
            title="source-v2",
            domain="ml",
            lineage_key="same-source",
            is_current=True,
        ))

        evidence = db._conn.execute(
            "SELECT status, invalid_reason FROM study_suggestion_evidence WHERE batch_id=?",
            (batch["batch_id"],),
        ).fetchone()
        assert tuple(evidence) == ("stale", "job_superseded")
        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.materialize_study_suggestions(
                batch["batch_id"], task_id=batch["task_id"], result=result
            )
        assert conflict.value.code == "study_suggestion_evidence_unavailable"
        assert db.get_study_suggestion_batch(batch["batch_id"])["status"] == "queued"
        assert db.list_study_suggestions(batch_id=batch["batch_id"])[0] == 0

    def test_superseded_lineage_blocks_accept_and_promotion_revalidates(self, db):
        _seed(db, job_id="source-v1", lineage_key="same-source")
        batch, suggestion = _ready(db, job_id="source-v1")
        db.create_job(Job(
            id="source-v2",
            content_type="article",
            pipeline="article",
            status=JobStatus.DONE,
            title="source-v2",
            domain="ml",
            lineage_key="same-source",
            is_current=True,
        ))

        with pytest.raises(StudySuggestionConflictError) as stale:
            _accept(db, batch, suggestion)
        assert stale.value.code == "study_suggestion_evidence_unavailable"

        db.delete_job_cascade("source-v2")
        db.promote_lineage_current("same-source")
        restored = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert restored["status"] == "valid"
        assert restored["invalid_reason"] is None
        assert _accept(db, batch, suggestion)["items"][0]["status"] == "accepted"

    def test_job_status_and_update_current_refresh_evidence_atomically(self, db):
        _seed(db, job_id="source-v1", lineage_key="same-source")
        batch, suggestion = _ready(db, job_id="source-v1")
        db.update_job("source-v1", status=JobStatus.PROCESSING)
        changed = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert (changed["status"], changed["invalid_reason"]) == (
            "stale", "job_not_done",
        )

        db.update_job("source-v1", status=JobStatus.DONE)
        assert db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0][
            "status"
        ] == "valid"
        db.create_job(Job(
            id="source-v2",
            content_type="article",
            pipeline="article",
            status=JobStatus.DONE,
            title="source-v2",
            domain="ml",
            lineage_key="same-source",
            is_current=False,
        ))
        db.update_job("source-v2", is_current=1)
        assert db.get_job("source-v2").is_current is True
        assert db.get_job("source-v1").is_current is False
        stale = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert (stale["status"], stale["invalid_reason"]) == (
            "stale", "job_superseded",
        )

        before = {
            job_id: db.get_job(job_id).is_current
            for job_id in ("source-v1", "source-v2")
        }
        for invalid in ("0", 2, -1, None):
            with pytest.raises(ValueError, match="is_current"):
                db.update_job("source-v1", is_current=invalid)
            assert {
                job_id: db.get_job(job_id).is_current
                for job_id in ("source-v1", "source-v2")
            } == before
        with pytest.raises(ValueError, match="lineage_key"):
            db.update_job("source-v2", lineage_key="different-lineage")
        assert db.get_job("source-v2").lineage_key == "same-source"

    def test_lineage_supersede_failure_rolls_back_job_and_evidence(self, db, monkeypatch):
        _seed(db, job_id="source-v1", lineage_key="same-source")
        _batch, suggestion = _ready(db, job_id="source-v1")

        def fail_revalidation(*, job_id: str, note_type: str | None = None) -> None:
            raise RuntimeError(f"fault:{job_id}:{note_type}")

        monkeypatch.setattr(
            db,
            "_revalidate_study_suggestion_evidence_locked",
            fail_revalidation,
        )
        with pytest.raises(RuntimeError, match="fault:source-v1"):
            db.create_job(Job(
                id="source-v2",
                content_type="article",
                pipeline="article",
                status=JobStatus.DONE,
                title="source-v2",
                domain="ml",
                lineage_key="same-source",
                is_current=True,
            ))

        assert db.get_job("source-v2") is None
        assert db.get_job("source-v1").is_current is True
        assert db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0][
            "status"
        ] == "valid"
        assert not db._conn.in_transaction

    def test_two_connections_serialize_supersede_before_accept(self, db, monkeypatch):
        _seed(db, job_id="source-v1", lineage_key="same-source")
        batch, suggestion = _ready(db, job_id="source-v1")
        second = Database(db._path)
        second.init_schema()
        entered = threading.Event()
        release = threading.Event()
        accept_started = threading.Event()
        original = second._revalidate_study_suggestion_evidence_locked

        def pause_revalidation(*, job_id: str, note_type: str | None = None) -> None:
            entered.set()
            assert release.wait(5)
            original(job_id=job_id, note_type=note_type)

        monkeypatch.setattr(
            second,
            "_revalidate_study_suggestion_evidence_locked",
            pause_revalidation,
        )

        def supersede() -> None:
            second.create_job(Job(
                id="source-v2",
                content_type="article",
                pipeline="article",
                status=JobStatus.DONE,
                title="source-v2",
                domain="ml",
                lineage_key="same-source",
                is_current=True,
            ))

        def accept() -> dict:
            accept_started.set()
            return _accept(db, batch, suggestion)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                supersede_future = pool.submit(supersede)
                assert entered.wait(5)
                accept_future = pool.submit(accept)
                assert accept_started.wait(5)
                release.set()
                supersede_future.result(timeout=5)
                with pytest.raises(StudySuggestionConflictError) as conflict:
                    accept_future.result(timeout=5)
                assert conflict.value.code == "study_suggestion_evidence_unavailable"
        finally:
            release.set()
            second.close()

    def test_job_domain_move_marks_evidence_stale_and_move_back_revalidates(self, db):
        _seed(db)
        _batch, suggestion = _ready(db)

        db.update_job("jobs_study_1", domain="other")
        moved = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert moved["status"] == "stale"
        assert moved["current_domain"] == "other"

        db.update_job("jobs_study_1", domain="ml")
        restored = db.get_study_suggestion(suggestion["suggestion_id"])["evidence"][0]
        assert restored["status"] == "valid"
        assert restored["invalid_reason"] is None

    def test_accepted_card_survives_job_delete_and_fk_blocks_orphan(self, db):
        _seed(db)
        batch, suggestion = _ready(db)
        card = _accept(db, batch, suggestion)["cards"][0]
        db.delete_job_cascade("jobs_study_1")
        assert db.get_study_card(card["card_id"])["job_id"] is None
        with pytest.raises(sqlite3.IntegrityError):
            db.delete_study_card(card["card_id"])
        assert not db._conn.in_transaction
        assert db.get_study_card(card["card_id"]) is not None
        assert db.create_study_card(
            card_id="after-restrict",
            domain="ml",
            front="connection usable?",
            back="yes",
        )["card_id"] == "after-restrict"

    @pytest.mark.parametrize("concept_change", ["delete", "reject"])
    def test_materialize_rejects_nonaccepted_concept_atomically(
        self, db, concept_change
    ):
        _seed(db)
        batch, result = _queued(db)
        if concept_change == "delete":
            db.delete_glossary_term("ml", "反向传播")
        else:
            assert db.reject_glossary_term("ml", "反向传播")

        with pytest.raises(StudySuggestionConflictError) as conflict:
            db.materialize_study_suggestions(
                batch["batch_id"], task_id=batch["task_id"], result=result
            )

        assert conflict.value.code == "study_suggestion_concept_unavailable"
        assert db.get_study_suggestion_batch(batch["batch_id"])["status"] == "queued"
        assert db.list_study_suggestions(batch_id=batch["batch_id"])[0] == 0
        assert not db._conn.in_transaction

    @pytest.mark.parametrize("concept_change", ["delete", "reject"])
    def test_reject_suggestion_does_not_require_live_concept(self, db, concept_change):
        _seed(db)
        batch, suggestion = _ready(db)
        if concept_change == "delete":
            db.delete_glossary_term("ml", "反向传播")
        else:
            assert db.reject_glossary_term("ml", "反向传播")

        outcome = db.apply_study_suggestion_operations(
            request_id=f"reject-after-{concept_change}",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion["suggestion_id"],
                    "expected_revision": suggestion["revision"],
                    "action": "reject",
                    "patch": {},
                    "reason": "no longer useful",
                }
            ],
        )

        assert outcome["items"][0]["status"] == "rejected"
        migration_current.validate(db._conn)

    def test_concept_merge_moves_current_pointers_and_terminal_card(self, db):
        _seed(db, concept="old")
        db.upsert_glossary_term("ml", "new", "new definition", status="accepted")
        batch, suggestion = _ready(db, concept="old")
        card = _accept(db, batch, suggestion)["cards"][0]

        db.merge_glossary_terms("ml", "old", "new")

        assert db.get_study_suggestion(suggestion["suggestion_id"])["concept_term"] == "new"
        assert db.get_study_card(card["card_id"])["concept_term"] == "new"
        assert db._conn.execute(
            """SELECT current_concept_term FROM study_suggestion_inputs
               WHERE batch_id=? AND kind='concept'""",
            (batch["batch_id"],),
        ).fetchone()[0] == "new"

    def test_domain_rename_recomputes_fingerprints_and_collision_rolls_back(self, db):
        _seed(db)
        batch, suggestion = _ready(db)
        card = _accept(db, batch, suggestion)["cards"][0]
        db.rename_domain("ml", "machine-learning")
        renamed = db.get_study_suggestion(suggestion["suggestion_id"])

        assert renamed["domain"] == "machine-learning"
        assert renamed["knowledge_fingerprint"] == knowledge_fingerprint(
            "machine-learning", renamed["knowledge_key"]
        )
        assert renamed["content_fingerprint"] == content_fingerprint(
            domain="machine-learning",
            card_type=renamed["card_type"],
            front=renamed["front"],
            back=renamed["back"],
            explanation=renamed["explanation"],
        )
        assert db.get_study_card(card["card_id"])["domain"] == "machine-learning"
        assert renamed["evidence"][0]["source_domain"] == "ml"
        assert renamed["evidence"][0]["current_domain"] == "machine-learning"

        db.create_study_card(
            card_id="target-card", domain="occupied", front="Q", back="A"
        )
        with pytest.raises(ValueError, match="已存在"):
            db.rename_domain("machine-learning", "occupied")
        assert db.get_study_suggestion(suggestion["suggestion_id"])["domain"] == "machine-learning"

    def test_schema_guards_reject_direct_tampering_and_cross_batch_link(self, db):
        _seed(db)
        batch_one, suggestion_one = _ready(db)
        _seed(
            db,
            job_id="jobs_study_2",
            body="## 优化\n\n梯度下降根据梯度更新参数。",
            concept="梯度下降",
        )
        batch_two, suggestion_two = _ready(
            db,
            request_id="batch-create-2",
            job_id="jobs_study_2",
            concept="梯度下降",
            knowledge_key="gradient-descent",
            front="梯度下降如何更新参数?",
        )
        evidence_two = suggestion_two["evidence"][0]

        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                """INSERT INTO study_suggestion_evidence_links
                   (batch_id, suggestion_id, evidence_id, ordinal, quote_snapshot,
                    quote_sha256, created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    batch_one["batch_id"], suggestion_one["suggestion_id"],
                    evidence_two["evidence_id"], 9, evidence_two["quote"],
                    evidence_two["quote_sha256"], "2026-07-14T00:00:00+00:00",
                ),
            )
        db._conn.rollback()
        input_id = db._conn.execute(
            "SELECT input_id FROM study_suggestion_inputs WHERE batch_id=? LIMIT 1",
            (batch_one["batch_id"],),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db._conn.execute(
                "UPDATE study_suggestion_inputs SET input_fingerprint=? WHERE input_id=?",
                ("0" * 64, input_id),
            )
        db._conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="fingerprint"):
            db._conn.execute(
                "UPDATE study_suggestions SET knowledge_fingerprint=? WHERE suggestion_id=?",
                ("0" * 64, suggestion_one["suggestion_id"]),
            )
        db._conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                """UPDATE study_suggestion_evidence
                   SET status='unavailable', invalid_reason=NULL
                   WHERE evidence_id=?""",
                (suggestion_one["evidence"][0]["evidence_id"],),
            )
        db._conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                """UPDATE study_suggestion_batches
                   SET status='queued' WHERE batch_id=?""",
                (batch_two["batch_id"],),
            )
        db._conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="ready.*immutable"):
            db._conn.execute(
                "UPDATE study_suggestion_batches SET task_id='tampered' WHERE batch_id=?",
                (batch_one["batch_id"],),
            )
        db._conn.rollback()

    def test_current_validator_accepts_complete_and_renamed_audit_chain(self, db):
        _seed(db)
        batch, suggestion = _ready(db)
        _accept(db, batch, suggestion)
        migration_current.validate(db._conn)

        db.rename_domain("ml", "machine-learning")
        migration_current.validate(db._conn)
        db.delete_job_cascade("jobs_study_1")
        migration_current.validate(db._conn)

    def test_current_validator_replays_multiple_domain_renames_and_concept_merges(self, db):
        _seed(db, concept="concept-a")
        db.upsert_glossary_term("ml", "concept-b", "middle", status="accepted")
        db.upsert_glossary_term("ml", "concept-c", "final", status="accepted")
        batch, suggestion = _ready(db, concept="concept-a")

        db.rename_domain("ml", "ml-middle")
        db.merge_glossary_terms("ml-middle", "concept-a", "concept-b")
        edited = db.apply_study_suggestion_operations(
            request_id="review-at-middle-identity",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion["suggestion_id"],
                    "expected_revision": suggestion["revision"],
                    "action": "edit",
                    "patch": {"front": "question at middle identity"},
                }
            ],
        )["items"][0]
        db.rename_domain("ml-middle", "ml-final")
        db.merge_glossary_terms("ml-final", "concept-b", "concept-c")
        db.apply_study_suggestion_operations(
            request_id="accept-at-final-identity",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion["suggestion_id"],
                    "expected_revision": edited["revision"],
                    "action": "accept",
                    "patch": {},
                }
            ],
        )

        migration_current.validate(db._conn)

    @pytest.mark.parametrize("transition_kind", ["domain_rename", "concept_merge"])
    def test_current_validator_rejects_identity_transition_tamper(
        self, db, transition_kind
    ):
        _seed(db, concept="concept-a")
        _ready(db, concept="concept-a")
        if transition_kind == "domain_rename":
            db.rename_domain("ml", "ml-renamed")
            tampered_field = "target_domain"
        else:
            db.upsert_glossary_term("ml", "concept-b", "new", status="accepted")
            db.merge_glossary_terms("ml", "concept-a", "concept-b")
            tampered_field = "target_concept"
        operation = db._conn.execute(
            """SELECT request_id, request_json
               FROM study_suggestion_operations
               WHERE operation_kind='identity_transition'
                 AND json_extract(request_json, '$.transition_kind')=?""",
            (transition_kind,),
        ).fetchone()
        request = json.loads(operation["request_json"])
        request[tampered_field] = "forged-identity"
        request_json = canonical_json(request)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            """UPDATE study_suggestion_operations
               SET request_json=?, request_fingerprint=? WHERE request_id=?""",
            (
                request_json,
                payload_fingerprint(request),
                operation["request_id"],
            ),
        )

        with pytest.raises(sqlite3.DatabaseError, match="operation|identity"):
            migration_current.validate(db._conn)

    def test_current_validator_accepts_multiple_retry_transition_chain(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="retry-twice-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="first timeout",
        )
        first_retry = db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="retry-twice-first",
            expected_revision=failed["revision"],
        )
        queued_again = db.mark_study_suggestion_batch_queued(
            batch["batch_id"],
            task_id=first_retry["task_id"],
            expected_revision=first_retry["revision"],
        )
        failed_again = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=first_retry["task_id"],
            expected_revision=queued_again["revision"],
            error_code="timeout",
            error_message="second timeout",
        )
        second_retry = db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="retry-twice-second",
            expected_revision=failed_again["revision"],
        )

        assert second_retry["attempt"] == 3
        migration_current.validate(db._conn)

    def test_current_validator_rejects_failed_state_rollback_without_retry_ledger(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="rollback-ledger-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        db._conn.execute(
            """UPDATE study_suggestion_batches
               SET status='pending_enqueue', revision=1, result_json=NULL,
                   error_code=NULL, error_message=NULL, updated_at=created_at
               WHERE batch_id=?""",
            (batch["batch_id"],),
        )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="重放|当前状态"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("terminal_kind", ["batch_ready", "batch_failed"])
    def test_current_validator_rejects_missing_lifecycle_operation(self, db, terminal_kind):
        _seed(db)
        if terminal_kind == "batch_ready":
            batch, _suggestion = _ready(db, request_id="missing-ready-operation")
        else:
            batch = db.create_study_suggestion_batch(
                request_id="missing-failed-operation",
                domain="ml",
                job_ids=["jobs_study_1"],
            )
            queued = db.mark_study_suggestion_batch_queued(
                batch["batch_id"], task_id=batch["task_id"], expected_revision=1
            )
            db.fail_study_suggestion_batch(
                batch["batch_id"],
                task_id=batch["task_id"],
                expected_revision=queued["revision"],
                error_code="timeout",
                error_message="worker timed out",
            )
        _guarded_delete(
            db,
            "DELETE FROM study_suggestion_operations "
            "WHERE batch_id=? AND operation_kind=?",
            (batch["batch_id"], terminal_kind),
        )

        with pytest.raises(
            sqlite3.DatabaseError, match="重放|lifecycle|当前状态|ledger|账本"
        ):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("error_code", "provider_error"),
            ("error_message", "forged failure"),
            ("updated_at", "2100-01-01T00:00:00+00:00"),
            ("revision", 4),
            ("attempt", 2),
            ("task_id", "study-suggestions:forged-task"),
            ("domain", "forged-domain"),
            ("deadline_at", "2100-01-01T00:00:00+00:00"),
        ],
    )
    def test_current_validator_rejects_failed_current_field_tamper(self, db, field, value):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="failed-field-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        if field == "deadline_at":
            deadline = datetime.fromisoformat(value)
            db._conn.execute(
                """UPDATE study_suggestion_batches
                   SET deadline_at=?, deadline_at_epoch_us=? WHERE batch_id=?""",
                (value, int(deadline.timestamp() * 1_000_000), batch["batch_id"]),
            )
        else:
            db._conn.execute(
                f"UPDATE study_suggestion_batches SET {field}=? WHERE batch_id=?",
                (value, batch["batch_id"]),
            )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="重放|当前状态"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("history", ["ready", "reviewed", "domain", "concept"])
    def test_current_validator_rejects_suggestion_updated_at_tamper(self, db, history):
        _seed(db, concept="concept-a")
        if history == "concept":
            db.upsert_glossary_term("ml", "concept-b", "merged", status="accepted")
        batch, suggestion = _ready(db, concept="concept-a")
        if history == "reviewed":
            db.apply_study_suggestion_operations(
                request_id="updated-at-review",
                batch_id=batch["batch_id"],
                items=[
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "expected_revision": 1,
                        "action": "edit",
                        "patch": {"front": "reviewed question"},
                    }
                ],
            )
        elif history == "domain":
            db.rename_domain("ml", "machine-learning")
        elif history == "concept":
            db.merge_glossary_terms("ml", "concept-a", "concept-b")
        db._conn.execute(
            "UPDATE study_suggestions SET updated_at=? WHERE suggestion_id=?",
            ("2100-01-01T00:00:00+00:00", suggestion["suggestion_id"]),
        )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="候选当前状态"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize(
        "target",
        ["request", "outcome", "fingerprint", "noncanonical", "coherent-future"],
    )
    def test_current_validator_rejects_lifecycle_operation_tamper(self, db, target):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="operation-tamper-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        row = db._conn.execute(
            """SELECT request_id, request_json, outcome_json
               FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='batch_failed'""",
            (batch["batch_id"],),
        ).fetchone()
        request = json.loads(row["request_json"])
        outcome = json.loads(row["outcome_json"])
        if target == "request":
            request["error_message"] = "forged request"
            request_json = canonical_json(request)
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                """UPDATE study_suggestion_operations
                   SET request_json=?, request_fingerprint=? WHERE request_id=?""",
                (request_json, payload_fingerprint(request), row["request_id"]),
            )
        elif target == "outcome":
            outcome["error_message"] = "forged outcome"
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET outcome_json=? WHERE request_id=?",
                (canonical_json(outcome), row["request_id"]),
            )
        elif target == "fingerprint":
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                """UPDATE study_suggestion_operations
                   SET request_fingerprint=? WHERE request_id=?""",
                ("0" * 64, row["request_id"]),
            )
        elif target == "noncanonical":
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET request_json=? WHERE request_id=?",
                (json.dumps(request, ensure_ascii=False, sort_keys=True), row["request_id"]),
            )
        else:
            future = "2100-01-01T00:00:00+00:00"
            outcome["updated_at"] = future
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                """UPDATE study_suggestion_operations
                   SET outcome_json=?, created_at=? WHERE request_id=?""",
                (canonical_json(outcome), future, row["request_id"]),
            )
            db._conn.execute(
                "UPDATE study_suggestion_batches SET updated_at=? WHERE batch_id=?",
                (future, batch["batch_id"]),
            )
            db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="建议操作|batch_failed|未来"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("target", ["task_id", "attempt", "expected_revision"])
    def test_current_validator_rejects_lifecycle_precondition_tamper(self, db, target):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="precondition-create", domain="ml", job_ids=["jobs_study_1"]
        )
        db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        row = db._conn.execute(
            """SELECT request_id, request_json
               FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='batch_queued'""",
            (batch["batch_id"],),
        ).fetchone()
        request = json.loads(row["request_json"])
        if target == "task_id":
            request[target] = "study-suggestions:wrong-task"
        else:
            request[target] += 1
        old_request_id = row["request_id"]
        request["request_id"] = _lifecycle_request_id(request)
        request_json = canonical_json(request)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            """UPDATE study_suggestion_operations
               SET request_id=?, request_json=?, request_fingerprint=?
               WHERE request_id=?""",
            (
                request["request_id"],
                request_json,
                payload_fingerprint(request),
                old_request_id,
            ),
        )

        with pytest.raises(sqlite3.DatabaseError, match="batch_queued"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("target", ["missing-queued", "duplicate", "out-of-order"])
    def test_current_validator_rejects_missing_duplicate_or_disordered_lifecycle(
        self, db, target
    ):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="lifecycle-order-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        queued_operation = db._conn.execute(
            """SELECT * FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='batch_queued'""",
            (batch["batch_id"],),
        ).fetchone()
        if target == "missing-queued":
            _guarded_delete(
                db,
                "DELETE FROM study_suggestion_operations WHERE request_id=?",
                (queued_operation["request_id"],),
            )
        elif target == "duplicate":
            request = json.loads(queued_operation["request_json"])
            request["request_id"] = "duplicate-lifecycle-operation"
            request_json = canonical_json(request)
            db._insert_study_suggestion_operation_locked(
                request_id=request["request_id"],
                request_fingerprint=payload_fingerprint(request),
                operation_kind="batch_queued",
                batch_id=batch["batch_id"],
                request_json=request_json,
                outcome=json.loads(queued_operation["outcome_json"]),
                created_at=queued_operation["created_at"],
            )
            db._conn.commit()
        else:
            failed_time = db._conn.execute(
                """SELECT created_at FROM study_suggestion_operations
                   WHERE batch_id=? AND operation_kind='batch_failed'""",
                (batch["batch_id"],),
            ).fetchone()[0]
            moved = (datetime.fromisoformat(failed_time) + timedelta(microseconds=1)).isoformat()
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET created_at=? WHERE request_id=?",
                (moved, queued_operation["request_id"]),
            )

        with pytest.raises(
            sqlite3.DatabaseError, match="batch_|lifecycle|当前状态|ledger|账本"
        ):
            migration_current.validate(db._conn)

    def test_current_validator_rejects_retry_without_failed_lifecycle(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="retry-without-failed-create",
            domain="ml",
            job_ids=["jobs_study_1"],
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="retry-without-failed-operation",
            expected_revision=failed["revision"],
        )
        _guarded_delete(
            db,
            """DELETE FROM study_suggestion_operations
               WHERE batch_id=? AND operation_kind='batch_failed'""",
            (batch["batch_id"],),
        )

        with pytest.raises(
            sqlite3.DatabaseError, match="batch_retry|failed lifecycle|ledger|账本"
        ):
            migration_current.validate(db._conn)

    def test_current_validator_rejects_pending_retry_revision_drift(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="pending-drift-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        retried = db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="pending-drift-retry",
            expected_revision=failed["revision"],
        )
        assert retried["revision"] == 4
        db._conn.execute(
            "UPDATE study_suggestion_batches SET revision=5 WHERE batch_id=?",
            (batch["batch_id"],),
        )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="当前状态"):
            migration_current.validate(db._conn)

    def test_current_validator_rejects_queued_revision_without_increment(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="queued-drift-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        assert queued["revision"] == 2
        db._conn.execute(
            "UPDATE study_suggestion_batches SET revision=1 WHERE batch_id=?",
            (batch["batch_id"],),
        )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="当前状态"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("terminal_status", ["ready", "failed"])
    def test_current_validator_rejects_terminal_updated_at_before_task_operation(
        self, db, terminal_status
    ):
        _seed(db)
        if terminal_status == "ready":
            batch, _suggestion = _ready(db)
        else:
            batch = db.create_study_suggestion_batch(
                request_id="terminal-time-create",
                domain="ml",
                job_ids=["jobs_study_1"],
            )
            queued = db.mark_study_suggestion_batch_queued(
                batch["batch_id"], task_id=batch["task_id"], expected_revision=1
            )
            db.fail_study_suggestion_batch(
                batch["batch_id"],
                task_id=batch["task_id"],
                expected_revision=queued["revision"],
                error_code="timeout",
                error_message="worker timed out",
            )
        db._conn.execute(
            "UPDATE study_suggestion_batches SET updated_at=? WHERE batch_id=?",
            ("2000-01-01T00:00:00+00:00", batch["batch_id"]),
        )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="当前状态"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("target", ["task_id", "attempt", "revision", "deadline"])
    def test_current_validator_binds_retry_outcome_to_transition_chain(self, db, target):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="retry-chain-create", domain="ml", job_ids=["jobs_study_1"]
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="worker timed out",
        )
        retried = db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="retry-chain-operation",
            expected_revision=failed["revision"],
            deadline_seconds=900,
        )
        row = db._conn.execute(
            "SELECT outcome_json FROM study_suggestion_operations "
            "WHERE request_id='retry-chain-operation'"
        ).fetchone()
        outcome = json.loads(row["outcome_json"])
        batch_updates: dict[str, object] = {}
        if target == "task_id":
            outcome["task_id"] = batch["task_id"]
            batch_updates["task_id"] = batch["task_id"]
        elif target == "attempt":
            outcome["attempt"] += 1
            batch_updates["attempt"] = outcome["attempt"]
        elif target == "revision":
            outcome["revision"] += 1
            batch_updates["revision"] = outcome["revision"]
        else:
            deadline = datetime.fromisoformat(outcome["deadline_at"])
            changed = deadline + timedelta(microseconds=1)
            outcome["deadline_at"] = changed.isoformat()
            batch_updates["deadline_at"] = outcome["deadline_at"]
            batch_updates["deadline_at_epoch_us"] = int(changed.timestamp() * 1_000_000)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            "UPDATE study_suggestion_operations SET outcome_json=? "
            "WHERE request_id='retry-chain-operation'",
            (canonical_json(outcome),),
        )
        set_clause = ", ".join(f"{field}=?" for field in batch_updates)
        db._conn.execute(
            f"UPDATE study_suggestion_batches SET {set_clause} WHERE batch_id=?",
            (*batch_updates.values(), retried["batch_id"]),
        )
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="batch_retry"):
            migration_current.validate(db._conn)

    def test_current_validator_replays_edit_then_accept_exactly(self, db):
        _seed(db)
        batch, suggestion = _ready(db)
        edited = db.apply_study_suggestion_operations(
            request_id="edit-before-accept",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {
                        "card_type": "qa",
                        "front": "  编辑后的问题?  ",
                        "back": "  编辑后的答案。  ",
                        "explanation": "  编辑后的解释。  ",
                    },
                }
            ],
        )["items"][0]
        db.apply_study_suggestion_operations(
            request_id="accept-after-edit",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion["suggestion_id"],
                    "expected_revision": edited["revision"],
                    "action": "accept",
                    "patch": {},
                }
            ],
        )

        migration_current.validate(db._conn)

    def test_current_validator_uses_frozen_helpers(self, db, monkeypatch):
        _seed(db)
        batch, suggestion = _ready(db)
        _accept(db, batch, suggestion)
        import shared.study as runtime_study
        import shared.study_suggestions as runtime_suggestions

        def mutable_runtime_helper(*_args, **_kwargs):
            raise AssertionError("v4 validator must not call mutable runtime helpers")

        for name in (
            "canonical_json",
            "payload_fingerprint",
            "knowledge_fingerprint",
            "content_fingerprint",
            "parse_ai_suggestions",
            "validate_operation_items",
        ):
            monkeypatch.setattr(runtime_suggestions, name, mutable_runtime_helper)
        monkeypatch.setattr(runtime_study, "datetime_to_epoch_us", mutable_runtime_helper)

        migration_current.validate(db._conn)

    @pytest.mark.parametrize(
        "field",
        [
            "knowledge_key",
            "concept_input_id",
            "card_type",
            "front",
            "back",
            "explanation",
            "evidence",
        ],
    )
    def test_current_validator_reconciles_every_result_field_by_ordinal(self, db, field):
        _seed(db)
        batch, _suggestion = _ready(db)
        result = batch["result"]
        item = result["suggestions"][0]
        replacements = {
            "knowledge_key": "tampered-key",
            "concept_input_id": None,
            "card_type": "qa",
            "front": "tampered front",
            "back": "tampered back",
            "explanation": "tampered explanation",
            "evidence": [
                {
                    "evidence_id": item["evidence"][0]["evidence_id"],
                    "quote": "链式法则",
                }
            ],
        }
        item[field] = replacements[field]
        _guarded_update(
            db,
            "study_suggestion_batch_ready_immutable",
            "UPDATE study_suggestion_batches SET result_json=? WHERE batch_id=?",
            (canonical_json(result), batch["batch_id"]),
        )

        with pytest.raises(sqlite3.DatabaseError):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize(
        "target",
        [
            "item-revision",
            "item-content",
            "item-evidence",
            "card-review",
            "card-source",
            "card-count",
        ],
    )
    def test_current_validator_replays_exact_review_outcome(self, db, target):
        _seed(db)
        batch, suggestion = _ready(db)
        _accept(db, batch, suggestion)
        row = db._conn.execute(
            "SELECT outcome_json FROM study_suggestion_operations WHERE request_id='accept-1'"
        ).fetchone()
        outcome = json.loads(row["outcome_json"])
        item = outcome["items"][0]
        if target == "item-revision":
            item["revision"] += 1
        elif target == "item-content":
            item["front"] = "tampered front"
            item["content_fingerprint"] = content_fingerprint(
                domain=item["domain"],
                card_type=item["card_type"],
                front=item["front"],
                back=item["back"],
                explanation=item["explanation"],
            )
        elif target == "item-evidence":
            item["evidence"][0]["quote"] = "链式法则"
        elif target == "card-review":
            outcome["cards"][0]["review"]["due_at"] = "2026-07-14T00:00:00+00:00"
        elif target == "card-source":
            outcome["cards"][0]["source"] = "suggestion:other"
        else:
            outcome["cards"] = []
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            "UPDATE study_suggestion_operations SET outcome_json=? WHERE request_id='accept-1'",
            (canonical_json(outcome),),
        )

        with pytest.raises(sqlite3.DatabaseError, match="outcome"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("target", ["job_ids", "concept_terms"])
    def test_current_validator_reconciles_batch_create_request_inputs(self, db, target):
        _seed(db)
        _ready(db)
        row = db._conn.execute(
            "SELECT request_json FROM study_suggestion_operations "
            "WHERE request_id='batch-create-1'"
        ).fetchone()
        request = json.loads(row["request_json"])
        request[target] = (
            ["jobs_study_1", "jobs_study_1"]
            if target == "job_ids"
            else ["不存在的概念"]
        )
        request_json = canonical_json(request)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            "UPDATE study_suggestion_operations "
            "SET request_json=?, request_fingerprint=? WHERE request_id='batch-create-1'",
            (request_json, payload_fingerprint(request)),
        )

        with pytest.raises(sqlite3.DatabaseError, match="batch_create"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("locator-shape", "locator_json JSON 类型"),
            ("operation-request-shape", "request_json JSON 类型"),
            ("operation-outcome-shape", "outcome_json JSON 类型"),
            ("accepted-card-evidence", "证据快照不匹配"),
            ("deadline-epoch", "deadline text/epoch"),
            ("input-fingerprint", "输入 fingerprint"),
            ("body-hash", "正文 hash"),
            ("quote-hash", "证据引用"),
            ("result-shape", "result_json JSON 类型"),
        ],
    )
    def test_current_validator_rejects_semantic_tamper(self, db, target, expected):
        _seed(db)
        batch, suggestion = _ready(db)
        accepted = _accept(db, batch, suggestion)
        batch_id = batch["batch_id"]
        evidence_id = suggestion["evidence"][0]["evidence_id"]
        suggestion_id = suggestion["suggestion_id"]

        if target == "locator-shape":
            _guarded_update(
                db,
                "study_suggestion_evidence_snapshot_immutable",
                "UPDATE study_suggestion_evidence SET locator_json='[]' WHERE evidence_id=?",
                (evidence_id,),
            )
        elif target == "operation-request-shape":
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET request_json='[]' "
                "WHERE operation_kind='batch_create' AND batch_id=?",
                (batch_id,),
            )
        elif target == "operation-outcome-shape":
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET outcome_json='42' "
                "WHERE operation_kind='batch_create' AND batch_id=?",
                (batch_id,),
            )
        elif target == "accepted-card-evidence":
            db._conn.execute(
                "UPDATE study_cards SET evidence_json='[]' WHERE card_id=?",
                (accepted["cards"][0]["card_id"],),
            )
            db._conn.commit()
        elif target == "deadline-epoch":
            _guarded_update(
                db,
                "study_suggestion_batch_ready_immutable",
                "UPDATE study_suggestion_batches SET deadline_at_epoch_us="
                "deadline_at_epoch_us+1 WHERE batch_id=?",
                (batch_id,),
            )
        elif target == "input-fingerprint":
            _guarded_update(
                db,
                "study_suggestion_input_snapshot_immutable",
                "UPDATE study_suggestion_inputs SET input_fingerprint=? "
                "WHERE batch_id=? AND kind='evidence'",
                ("0" * 64, batch_id),
            )
        elif target == "body-hash":
            _guarded_update(
                db,
                "study_suggestion_evidence_snapshot_immutable",
                "UPDATE study_suggestion_evidence SET body_sha256=? WHERE evidence_id=?",
                ("0" * 64, evidence_id),
            )
        elif target == "quote-hash":
            _guarded_update(
                db,
                "study_suggestion_link_immutable",
                "UPDATE study_suggestion_evidence_links SET quote_sha256=? "
                "WHERE suggestion_id=?",
                ("0" * 64, suggestion_id),
            )
        else:
            _guarded_update(
                db,
                "study_suggestion_batch_ready_immutable",
                "UPDATE study_suggestion_batches SET result_json='[]' WHERE batch_id=?",
                (batch_id,),
            )

        with pytest.raises(sqlite3.DatabaseError, match=expected):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize("target", ["cross-batch", "fingerprint"])
    def test_current_validator_rejects_cross_batch_operation_and_fingerprint(self, db, target):
        _seed(db)
        first, _suggestion = _ready(db)
        _seed(
            db,
            job_id="jobs_study_2",
            body="## 优化\n\n梯度下降根据梯度更新参数。",
            concept="梯度下降",
        )
        second, _ = _ready(
            db,
            request_id="second-batch-create",
            job_id="jobs_study_2",
            concept="梯度下降",
            knowledge_key="gradient-descent",
            front="梯度下降如何更新参数?",
        )
        if target == "cross-batch":
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET batch_id=? "
                "WHERE operation_kind='batch_create' AND batch_id=?",
                (second["batch_id"], first["batch_id"]),
            )
            expected = "outcome batch"
        else:
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET request_fingerprint=? "
                "WHERE operation_kind='batch_create' AND batch_id=?",
                ("0" * 64, second["batch_id"]),
            )
            expected = "request fingerprint"
        with pytest.raises(sqlite3.DatabaseError, match=expected):
            migration_current.validate(db._conn)


def _valid_ai_output() -> dict:
    return {
        "schema_version": 1,
        "suggestions": [
            {
                "knowledge_key": "key",
                "concept_input_id": None,
                "card_type": "basic",
                "front": "Q",
                "back": "A",
                "explanation": "",
                "evidence": [{"evidence_id": "ev", "quote": "quoted fact"}],
            }
        ],
    }


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", None])
def test_parser_rejects_non_plain_integer_schema_version(schema_version):
    payload = _valid_ai_output()
    payload["schema_version"] = schema_version
    with pytest.raises(ValueError):
        parse_ai_suggestions(
            payload, max_cards=5, evidence_ids={"ev"}, concept_input_ids=set()
        )


@pytest.mark.parametrize("card_type", [True, [], {}, 1])
def test_parser_rejects_non_string_card_type_without_type_error(card_type):
    payload = _valid_ai_output()
    payload["suggestions"][0]["card_type"] = card_type
    with pytest.raises(ValueError):
        parse_ai_suggestions(
            payload, max_cards=5, evidence_ids={"ev"}, concept_input_ids=set()
        )


@pytest.mark.parametrize("quote", ["", "   ", "\n\t"])
def test_parser_rejects_blank_quote(quote):
    payload = _valid_ai_output()
    payload["suggestions"][0]["evidence"][0]["quote"] = quote
    with pytest.raises(ValueError):
        parse_ai_suggestions(
            payload, max_cards=5, evidence_ids={"ev"}, concept_input_ids=set()
        )


@pytest.mark.parametrize(
    "items",
    [
        [
            {
                "suggestion_id": "s",
                "expected_revision": 1,
                "action": [],
                "patch": {},
            }
        ],
        [
            {
                "suggestion_id": "s",
                "expected_revision": 1 << 63,
                "action": "accept",
                "patch": {},
            }
        ],
        [
            {
                "suggestion_id": "s",
                "expected_revision": 1,
                "action": "reject",
                "patch": {},
                "reason": "   ",
            }
        ],
        [
            {
                "suggestion_id": "s",
                "expected_revision": 1,
                "action": "accept",
                "patch": {},
                "reason": "unused",
            }
        ],
        [{1: "bad-key"}],
        [
            {
                "suggestion_id": "s",
                "expected_revision": 1,
                "action": "edit",
                "patch": {1: "bad-key"},
            }
        ],
    ],
)
def test_operation_validation_rejects_malicious_shapes_without_type_error(items):
    with pytest.raises(ValueError):
        validate_operation_items(items)


def test_operation_validation_rejects_101_items():
    with pytest.raises(ValueError):
        validate_operation_items(
            [
                {
                    "suggestion_id": f"s-{index}",
                    "expected_revision": 1,
                    "action": "accept",
                    "patch": {},
                }
                for index in range(101)
            ]
        )


class TestStudySuggestionReleaseBlockers:
    @pytest.mark.parametrize(
        "reserved_request_id",
        [
            "study-lifecycle:batch_queued:external",
            "identity-transition:external",
        ],
    )
    def test_external_create_rejects_internal_request_namespace_without_writes(
        self, db, reserved_request_id
    ):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="namespace-owner",
            domain="ml",
            job_ids=["jobs_study_1"],
        )
        before = db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0]

        with pytest.raises(ValueError, match="request_id"):
            db.create_study_suggestion_batch(
                request_id=reserved_request_id,
                domain="ml",
                job_ids=["jobs_study_1"],
            )

        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0] == before
        assert db.get_study_suggestion_batch(batch["batch_id"])["status"] == "pending_enqueue"

    def test_input_dedupe_cannot_occupy_canonical_lifecycle_request_id(self, db):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="namespace-canonical-owner",
            domain="ml",
            job_ids=["jobs_study_1"],
        )
        lifecycle_request = {
            "operation_kind": "batch_queued",
            "batch_id": batch["batch_id"],
            "task_id": batch["task_id"],
            "attempt": batch["attempt"],
            "expected_revision": batch["revision"],
        }
        reserved_request_id = _lifecycle_request_id(lifecycle_request)
        before = db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0]

        with pytest.raises(ValueError, match="request_id"):
            db.create_study_suggestion_batch(
                request_id=reserved_request_id,
                domain="ml",
                job_ids=["jobs_study_1"],
            )

        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0] == before
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=batch["revision"],
        )
        assert queued["status"] == "queued"

    @pytest.mark.parametrize(
        "reserved_request_id",
        [
            "study-lifecycle:batch_ready:external",
            "identity-transition:external-review",
        ],
    )
    def test_external_review_rejects_internal_request_namespace_without_writes(
        self, db, reserved_request_id
    ):
        _seed(db)
        batch, suggestion = _ready(db, request_id="namespace-review-owner")
        before_operations = db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0]

        with pytest.raises(ValueError, match="request_id"):
            db.apply_study_suggestion_operations(
                request_id=reserved_request_id,
                batch_id=batch["batch_id"],
                items=[
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "expected_revision": suggestion["revision"],
                        "action": "reject",
                        "reason": "duplicate",
                        "patch": {},
                    }
                ],
            )

        assert db._conn.execute(
            "SELECT COUNT(*) FROM study_suggestion_operations"
        ).fetchone()[0] == before_operations
        assert db.get_study_suggestion(suggestion["suggestion_id"])["status"] == "suggested"

    @pytest.mark.parametrize("terminal", ["failed", "ready"])
    def test_wall_clock_rollback_is_clamped_for_terminal_lifecycle(
        self, db, monkeypatch, terminal
    ):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id=f"clock-clamp-{terminal}",
            domain="ml",
            job_ids=["jobs_study_1"],
            concept_terms=["反向传播"],
        )
        base = datetime.fromisoformat(batch["updated_at"])
        future = base + timedelta(minutes=2)
        monkeypatch.setattr(db_module, "_now_iso", lambda: future.isoformat())
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        monkeypatch.setattr(
            db_module,
            "_now_iso",
            lambda: (base + timedelta(minutes=1)).isoformat(),
        )

        if terminal == "failed":
            current = db.fail_study_suggestion_batch(
                batch["batch_id"],
                task_id=batch["task_id"],
                expected_revision=queued["revision"],
                error_code="timeout",
                error_message="worker timed out",
            )
        else:
            evidence = batch["llm_request"]["evidence"][0]
            concept = batch["llm_request"]["concepts"][0]
            db.materialize_study_suggestions(
                batch["batch_id"],
                task_id=batch["task_id"],
                result={
                    "schema_version": 1,
                    "suggestions": [
                        {
                            "knowledge_key": "clock-clamp-key",
                            "concept_input_id": concept["input_id"],
                            "card_type": "basic",
                            "front": "clock clamp?",
                            "back": "yes",
                            "explanation": "monotonic",
                            "evidence": [
                                {
                                    "evidence_id": evidence["evidence_id"],
                                    "quote": "链式法则",
                                }
                            ],
                        }
                    ],
                },
            )
            current = db.get_study_suggestion_batch(batch["batch_id"])

        assert current["updated_at"] == queued["updated_at"]
        migration_current.validate(db._conn)

    def test_wall_clock_rollback_is_clamped_for_queue_and_input_dedupe(
        self, db, monkeypatch
    ):
        _seed(db)
        batch = db.create_study_suggestion_batch(
            request_id="clock-clamp-queue-owner",
            domain="ml",
            job_ids=["jobs_study_1"],
        )
        base = datetime.fromisoformat(batch["updated_at"])
        monkeypatch.setattr(
            db_module, "_now_iso", lambda: (base - timedelta(minutes=1)).isoformat()
        )
        queued = db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        assert queued["updated_at"] == batch["updated_at"]
        monkeypatch.setattr(
            db_module, "utc_now", lambda: base - timedelta(minutes=2)
        )

        replay = db.create_study_suggestion_batch(
            request_id="clock-clamp-input-dedupe",
            domain="ml",
            job_ids=["jobs_study_1"],
        )

        assert replay == queued
        operation_time = db._conn.execute(
            "SELECT created_at FROM study_suggestion_operations "
            "WHERE request_id='clock-clamp-input-dedupe'"
        ).fetchone()[0]
        assert operation_time == queued["updated_at"]
        migration_current.validate(db._conn)

    def test_wall_clock_rollback_is_clamped_for_retry_and_review(self, db, monkeypatch):
        _seed(db)
        batch, result = _queued(db, request_id="clock-clamp-retry")
        base = datetime.fromisoformat(
            db.get_study_suggestion_batch(batch["batch_id"])["updated_at"]
        )
        future = base + timedelta(minutes=2)
        monkeypatch.setattr(db_module, "_now_iso", lambda: future.isoformat())
        failed = db.fail_study_suggestion_batch(
            batch["batch_id"],
            task_id=batch["task_id"],
            expected_revision=2,
            error_code="timeout",
            error_message="worker timed out",
        )
        monkeypatch.setattr(db_module, "utc_now", lambda: base + timedelta(minutes=1))
        retried = db.retry_study_suggestion_batch(
            batch["batch_id"],
            request_id="clock-clamp-retry-operation",
            expected_revision=failed["revision"],
        )
        assert retried["updated_at"] == failed["updated_at"]

        monkeypatch.setattr(db_module, "_now_iso", lambda: future.isoformat())
        db.mark_study_suggestion_batch_queued(
            batch["batch_id"],
            task_id=retried["task_id"],
            expected_revision=retried["revision"],
        )
        suggestions = db.materialize_study_suggestions(
            batch["batch_id"], task_id=retried["task_id"], result=result
        )
        before_review = db.get_study_suggestion_batch(batch["batch_id"])["updated_at"]
        monkeypatch.setattr(db_module, "utc_now", lambda: base + timedelta(minutes=1))
        reviewed = db.apply_study_suggestion_operations(
            request_id="clock-clamp-review",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestions[0]["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {"front": "monotonic review"},
                }
            ],
        )
        assert reviewed["items"][0]["updated_at"] == before_review
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("transition", ["domain_rename", "concept_merge"])
    @pytest.mark.parametrize("phase", ["pending", "queued", "ready"])
    def test_identity_outcome_binds_exact_runtime_impact_sets(
        self, db, transition, phase
    ):
        _seed(db, concept="concept-a")
        db.upsert_glossary_term("ml", "concept-b", "target", status="accepted")
        batch = db.create_study_suggestion_batch(
            request_id=f"identity-impact-{transition}-{phase}",
            domain="ml",
            job_ids=["jobs_study_1"],
            concept_terms=["concept-a"],
        )
        suggestion_ids: list[str] = []
        if phase in {"queued", "ready"}:
            db.mark_study_suggestion_batch_queued(
                batch["batch_id"], task_id=batch["task_id"], expected_revision=1
            )
        if phase == "ready":
            evidence = batch["llm_request"]["evidence"][0]
            concept = batch["llm_request"]["concepts"][0]
            suggestion_ids = [
                db.materialize_study_suggestions(
                    batch["batch_id"],
                    task_id=batch["task_id"],
                    result={
                        "schema_version": 1,
                        "suggestions": [
                            {
                                "knowledge_key": "identity-impact-key",
                                "concept_input_id": concept["input_id"],
                                "card_type": "basic",
                                "front": "identity impact?",
                                "back": "bound",
                                "explanation": "exact",
                                "evidence": [
                                    {
                                        "evidence_id": evidence["evidence_id"],
                                        "quote": "链式法则",
                                    }
                                ],
                            }
                        ],
                    },
                )[0]["suggestion_id"]
            ]

        if transition == "domain_rename":
            db.rename_domain("ml", "machine-learning")
            expected_input_ids: list[str] = []
        else:
            db.merge_glossary_terms("ml", "concept-a", "concept-b")
            expected_input_ids = [batch["llm_request"]["concepts"][0]["input_id"]]
        row = db._conn.execute(
            "SELECT outcome_json FROM study_suggestion_operations "
            "WHERE operation_kind='identity_transition' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert json.loads(row["outcome_json"]) == {
            "batch_id": batch["batch_id"],
            "input_ids": expected_input_ids,
            "suggestion_ids": suggestion_ids,
        }
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("transition", ["domain_rename", "concept_merge"])
    @pytest.mark.parametrize("target", ["input_ids", "suggestion_ids", "created_at"])
    def test_current_validator_rejects_identity_impact_or_order_tamper(
        self, db, transition, target
    ):
        _seed(db, concept="concept-a")
        db.upsert_glossary_term("ml", "concept-b", "target", status="accepted")
        batch, _suggestion = _ready(
            db,
            request_id=f"identity-impact-tamper-{transition}-{target}",
            concept="concept-a",
        )
        if transition == "domain_rename":
            db.rename_domain("ml", "machine-learning")
        else:
            db.merge_glossary_terms("ml", "concept-a", "concept-b")
        operation = db._conn.execute(
            "SELECT request_id, outcome_json, created_at FROM study_suggestion_operations "
            "WHERE operation_kind='identity_transition' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if target == "created_at":
            queued_time = db._conn.execute(
                "SELECT created_at FROM study_suggestion_operations "
                "WHERE batch_id=? AND operation_kind='batch_queued'",
                (batch["batch_id"],),
            ).fetchone()[0]
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET created_at=? WHERE request_id=?",
                (queued_time, operation["request_id"]),
            )
        else:
            outcome = json.loads(operation["outcome_json"])
            outcome[target] = [*outcome.get(target, []), "forged-id"]
            _guarded_update(
                db,
                "study_suggestion_operation_immutable",
                "UPDATE study_suggestion_operations SET outcome_json=? WHERE request_id=?",
                (canonical_json(outcome), operation["request_id"]),
            )

        with pytest.raises(sqlite3.DatabaseError, match="identity|时间线|影响"):
            migration_current.validate(db._conn)

    def test_same_timestamp_identity_chain_uses_ledger_sequence(self, db, monkeypatch):
        _seed(db, concept="concept-a")
        db.upsert_glossary_term("ml", "concept-b", "target", status="accepted")
        batch, _suggestion = _ready(
            db, request_id="same-time-identity", concept="concept-a"
        )
        fixed = db.get_study_suggestion_batch(batch["batch_id"])["updated_at"]
        monkeypatch.setattr(db_module, "_now_iso", lambda: fixed)

        db.rename_domain("ml", "machine-learning")
        db.merge_glossary_terms("machine-learning", "concept-a", "concept-b")

        rows = db._conn.execute(
            "SELECT ledger_seq, created_at FROM study_suggestion_operations "
            "WHERE operation_kind='identity_transition' ORDER BY ledger_seq"
        ).fetchall()
        assert [row["created_at"] for row in rows] == [fixed, fixed]
        assert [row["ledger_seq"] for row in rows] == sorted(
            row["ledger_seq"] for row in rows
        )
        migration_current.validate(db._conn)

    @pytest.mark.parametrize("operation_kind", ["batch_create", "suggestion_review"])
    @pytest.mark.parametrize(
        "reserved_request_id",
        ["study-lifecycle:external", "identity-transition:external"],
    )
    def test_current_validator_rejects_external_operation_in_internal_namespace(
        self, db, operation_kind, reserved_request_id
    ):
        _seed(db)
        batch, suggestion = _ready(
            db,
            request_id=f"validator-namespace-{operation_kind}-{reserved_request_id[0]}",
        )
        if operation_kind == "suggestion_review":
            db.apply_study_suggestion_operations(
                request_id="validator-external-review",
                batch_id=batch["batch_id"],
                items=[
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "expected_revision": suggestion["revision"],
                        "action": "reject",
                        "reason": "duplicate",
                        "patch": {},
                    }
                ],
            )
        operation = db._conn.execute(
            "SELECT request_id, request_json FROM study_suggestion_operations "
            "WHERE operation_kind=? ORDER BY rowid DESC LIMIT 1",
            (operation_kind,),
        ).fetchone()
        request = json.loads(operation["request_json"])
        request["request_id"] = reserved_request_id
        request_json = canonical_json(request)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            "UPDATE study_suggestion_operations SET request_id=?, request_json=?, "
            "request_fingerprint=? WHERE request_id=?",
            (
                reserved_request_id,
                request_json,
                payload_fingerprint(request),
                operation["request_id"],
            ),
        )

        with pytest.raises(sqlite3.DatabaseError, match="命名空间"):
            migration_current.validate(db._conn)

    def test_current_validator_rejects_noncanonical_lifecycle_request_id(self, db):
        _seed(db)
        batch, _result = _queued(db, request_id="validator-lifecycle-canonical")
        operation = db._conn.execute(
            "SELECT request_id, request_json FROM study_suggestion_operations "
            "WHERE batch_id=? AND operation_kind='batch_queued'",
            (batch["batch_id"],),
        ).fetchone()
        request = json.loads(operation["request_json"])
        forged_request_id = "study-lifecycle:batch_queued:not-canonical"
        request["request_id"] = forged_request_id
        request_json = canonical_json(request)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            "UPDATE study_suggestion_operations SET request_id=?, request_json=?, "
            "request_fingerprint=? WHERE request_id=?",
            (
                forged_request_id,
                request_json,
                payload_fingerprint(request),
                operation["request_id"],
            ),
        )

        with pytest.raises(sqlite3.DatabaseError, match="lifecycle"):
            migration_current.validate(db._conn)

    @pytest.mark.parametrize(
        "forged_request_id",
        [
            "identity-transition:short",
            "identity-transition:ABCDEF0123456789ABCDEF0123456789",
        ],
    )
    def test_current_validator_requires_lower_hex_identity_request_id(
        self, db, forged_request_id
    ):
        _seed(db)
        _ready(db, request_id="validator-identity-id")
        db.rename_domain("ml", "machine-learning")
        operation = db._conn.execute(
            "SELECT request_id, request_json FROM study_suggestion_operations "
            "WHERE operation_kind='identity_transition'"
        ).fetchone()
        request = json.loads(operation["request_json"])
        request["request_id"] = forged_request_id
        request_json = canonical_json(request)
        _guarded_update(
            db,
            "study_suggestion_operation_immutable",
            "UPDATE study_suggestion_operations SET request_id=?, request_json=?, "
            "request_fingerprint=? WHERE request_id=?",
            (
                forged_request_id,
                request_json,
                payload_fingerprint(request),
                operation["request_id"],
            ),
        )

        with pytest.raises(sqlite3.DatabaseError, match="identity_transition request_id"):
            migration_current.validate(db._conn)

    def test_multi_batch_identity_clamps_every_row_to_global_lower_bound(
        self, db, monkeypatch
    ):
        _seed(db, job_id="jobs_study_1", concept="concept-a")
        _seed(
            db,
            job_id="jobs_study_2",
            concept="concept-a",
            body="## 优化\n\n梯度下降沿负梯度方向更新参数。",
        )
        batch_one, suggestion_one = _ready(
            db,
            request_id="multi-batch-clock-one",
            job_id="jobs_study_1",
            concept="concept-a",
            knowledge_key="multi-batch-one",
            front="first batch?",
        )
        batch_two, suggestion_two = _ready(
            db,
            request_id="multi-batch-clock-two",
            job_id="jobs_study_2",
            concept="concept-a",
            knowledge_key="multi-batch-two",
            front="second batch?",
        )
        base = max(
            datetime.fromisoformat(batch_one["updated_at"]),
            datetime.fromisoformat(batch_two["updated_at"]),
        )
        first_bound = base + timedelta(seconds=1)
        global_bound = base + timedelta(seconds=2)
        monkeypatch.setattr(db_module, "utc_now", lambda: first_bound)
        db.apply_study_suggestion_operations(
            request_id="multi-batch-review-one",
            batch_id=batch_one["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion_one["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {"front": "first bounded"},
                }
            ],
        )
        monkeypatch.setattr(db_module, "utc_now", lambda: global_bound)
        db.apply_study_suggestion_operations(
            request_id="multi-batch-review-two",
            batch_id=batch_two["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion_two["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {"front": "second bounded"},
                }
            ],
        )
        monkeypatch.setattr(
            db_module, "_now_iso", lambda: (base - timedelta(minutes=1)).isoformat()
        )

        db.rename_domain("ml", "machine-learning")

        identity_rows = db._conn.execute(
            "SELECT batch_id, outcome_json, created_at "
            "FROM study_suggestion_operations WHERE operation_kind='identity_transition' "
            "ORDER BY batch_id"
        ).fetchall()
        assert len(identity_rows) == 2
        assert {row["created_at"] for row in identity_rows} == {global_bound.isoformat()}
        for row in identity_rows:
            outcome = json.loads(row["outcome_json"])
            assert outcome["batch_id"] == row["batch_id"]
            assert len(outcome["suggestion_ids"]) == 1
        assert {
            row["updated_at"]
            for row in db._conn.execute(
                "SELECT updated_at FROM study_suggestion_batches ORDER BY batch_id"
            ).fetchall()
        } == {global_bound.isoformat()}
        assert {
            row["updated_at"]
            for row in db._conn.execute(
                "SELECT updated_at FROM study_suggestions ORDER BY suggestion_id"
            ).fetchall()
        } == {global_bound.isoformat()}
        migration_current.validate(db._conn)

    def test_operation_ledger_sequence_is_canonical_when_rowid_order_changes(
        self, db, monkeypatch
    ):
        """同时间独立操作只按稳定 ledger sequence 重放,rowid 不再承载语义."""
        _seed(db, concept="concept-a")
        batch = db.create_study_suggestion_batch(
            request_id="ledger-rowid-create",
            domain="ml",
            job_ids=["jobs_study_1"],
            concept_terms=["concept-a"],
            max_cards=5,
        )
        db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        evidence = batch["llm_request"]["evidence"][0]
        concept_id = batch["llm_request"]["concepts"][0]["input_id"]
        suggestions = db.materialize_study_suggestions(
            batch["batch_id"],
            task_id=batch["task_id"],
            result={
                "schema_version": 1,
                "suggestions": [
                    {
                        "knowledge_key": f"ledger-rowid-{index}",
                        "concept_input_id": concept_id,
                        "card_type": "basic",
                        "front": f"ledger rowid {index}?",
                        "back": f"answer {index}",
                        "explanation": "stable sequence",
                        "evidence": [
                            {
                                "evidence_id": evidence["evidence_id"],
                                "quote": "链式法则",
                            }
                        ],
                    }
                    for index in (1, 2)
                ],
            },
        )
        fixed = datetime.fromisoformat(
            db.get_study_suggestion_batch(batch["batch_id"])["updated_at"]
        )
        monkeypatch.setattr(db_module, "utc_now", lambda: fixed)
        for index, suggestion in enumerate(suggestions, 1):
            db.apply_study_suggestion_operations(
                request_id=f"ledger-rowid-review-{index}",
                batch_id=batch["batch_id"],
                items=[
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "expected_revision": 1,
                        "action": "edit",
                        "patch": {"front": f"ledger edited {index}"},
                    }
                ],
            )

        rows = db._conn.execute(
            "SELECT rowid, ledger_seq, request_id FROM study_suggestion_operations "
            "WHERE request_id LIKE 'ledger-rowid-review-%' ORDER BY ledger_seq"
        ).fetchall()
        assert [row["request_id"] for row in rows] == [
            "ledger-rowid-review-1",
            "ledger-rowid-review-2",
        ]
        first_rowid, second_rowid = int(rows[0]["rowid"]), int(rows[1]["rowid"])
        trigger_sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='study_suggestion_operation_immutable'"
        ).fetchone()[0]
        db._conn.execute("DROP TRIGGER study_suggestion_operation_immutable")
        db._conn.execute(
            "UPDATE study_suggestion_operations SET rowid=-1 WHERE rowid=?",
            (first_rowid,),
        )
        db._conn.execute(
            "UPDATE study_suggestion_operations SET rowid=? WHERE rowid=?",
            (first_rowid, second_rowid),
        )
        db._conn.execute(
            "UPDATE study_suggestion_operations SET rowid=? WHERE rowid=-1",
            (second_rowid,),
        )
        db._conn.execute(trigger_sql)
        db._conn.commit()

        migration_current.validate(db._conn)
        replay_order = db._conn.execute(
            "SELECT request_id FROM study_suggestion_operations "
            "WHERE request_id LIKE 'ledger-rowid-review-%' ORDER BY ledger_seq"
        ).fetchall()
        assert [row["request_id"] for row in replay_order] == [
            "ledger-rowid-review-1",
            "ledger-rowid-review-2",
        ]

    @pytest.mark.parametrize("target", ["gap", "swap", "hash"])
    def test_current_validator_rejects_operation_ledger_tamper(self, db, target):
        _seed(db)
        batch, suggestion = _ready(db, request_id=f"ledger-tamper-{target}")
        db.apply_study_suggestion_operations(
            request_id=f"ledger-tamper-review-{target}",
            batch_id=batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestion["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {"front": f"ledger tamper {target}"},
                }
            ],
        )
        rows = db._conn.execute(
            "SELECT ledger_seq FROM study_suggestion_operations ORDER BY ledger_seq"
        ).fetchall()
        trigger_sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='study_suggestion_operation_immutable'"
        ).fetchone()[0]
        db._conn.execute("DROP TRIGGER study_suggestion_operation_immutable")
        if target == "gap":
            db._conn.execute(
                "UPDATE study_suggestion_operations SET ledger_seq=? "
                "WHERE ledger_seq=?",
                (len(rows) + 10, len(rows)),
            )
        elif target == "swap":
            first, second = len(rows) - 1, len(rows)
            temporary = len(rows) + 100
            db._conn.execute(
                "UPDATE study_suggestion_operations SET ledger_seq=? "
                "WHERE ledger_seq=?",
                (temporary, first),
            )
            db._conn.execute(
                "UPDATE study_suggestion_operations SET ledger_seq=? "
                "WHERE ledger_seq=?",
                (first, second),
            )
            db._conn.execute(
                "UPDATE study_suggestion_operations SET ledger_seq=? "
                "WHERE ledger_seq=?",
                (second, temporary),
            )
        else:
            db._conn.execute(
                "UPDATE study_suggestion_operations SET ledger_sha256=? "
                "WHERE ledger_seq=?",
                ("0" * 64, len(rows)),
            )
        db._conn.execute(trigger_sql)
        db._conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="ledger|账本"):
            migration_current.validate(db._conn)

    def test_operation_ledger_rejects_duplicate_sequence_at_write_time(self, db):
        _seed(db)
        _ready(db, request_id="ledger-duplicate")
        rows = db._conn.execute(
            "SELECT ledger_seq FROM study_suggestion_operations ORDER BY ledger_seq"
        ).fetchall()
        trigger_sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='study_suggestion_operation_immutable'"
        ).fetchone()[0]
        db._conn.execute("DROP TRIGGER study_suggestion_operation_immutable")
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "UPDATE study_suggestion_operations SET ledger_seq=? "
                "WHERE ledger_seq=?",
                (rows[0]["ledger_seq"], rows[1]["ledger_seq"]),
            )
        db._conn.rollback()
        db._conn.execute(trigger_sql)
        db._conn.commit()

    def test_global_ledger_clock_clamps_cross_batch_lifecycle_and_review(
        self, db, monkeypatch
    ):
        _seed(db, job_id="jobs_study_1", concept="concept-a")
        _seed(
            db,
            job_id="jobs_study_2",
            concept="concept-a",
            body="## 优化\n\n梯度下降沿负梯度方向更新参数。",
        )
        first_batch, first_suggestion = _ready(
            db,
            request_id="global-clock-first",
            job_id="jobs_study_1",
            concept="concept-a",
            knowledge_key="global-clock-first",
            front="first global clock?",
        )
        base = datetime.fromisoformat(first_batch["updated_at"])
        global_bound = base + timedelta(seconds=2)
        monkeypatch.setattr(db_module, "utc_now", lambda: global_bound)
        db.apply_study_suggestion_operations(
            request_id="global-clock-first-review",
            batch_id=first_batch["batch_id"],
            items=[
                {
                    "suggestion_id": first_suggestion["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {"front": "global lower bound"},
                }
            ],
        )

        rolled_back = base + timedelta(seconds=1)
        monkeypatch.setattr(db_module, "utc_now", lambda: rolled_back)
        monkeypatch.setattr(db_module, "_now_iso", lambda: rolled_back.isoformat())
        second_batch, result = _queued(
            db,
            request_id="global-clock-second",
            job_id="jobs_study_2",
            concept="concept-a",
            knowledge_key="global-clock-second",
            front="second global clock?",
        )
        queued = db.get_study_suggestion_batch(second_batch["batch_id"])
        assert second_batch["created_at"] == global_bound.isoformat()
        assert queued["updated_at"] == global_bound.isoformat()

        failed = db.fail_study_suggestion_batch(
            second_batch["batch_id"],
            task_id=second_batch["task_id"],
            expected_revision=queued["revision"],
            error_code="timeout",
            error_message="retry after rollback",
        )
        retried = db.retry_study_suggestion_batch(
            second_batch["batch_id"],
            request_id="global-clock-second-retry",
            expected_revision=failed["revision"],
        )
        queued_retry = db.mark_study_suggestion_batch_queued(
            second_batch["batch_id"],
            task_id=retried["task_id"],
            expected_revision=retried["revision"],
        )
        suggestions = db.materialize_study_suggestions(
            second_batch["batch_id"],
            task_id=retried["task_id"],
            result=result,
        )
        reviewed = db.apply_study_suggestion_operations(
            request_id="global-clock-second-review",
            batch_id=second_batch["batch_id"],
            items=[
                {
                    "suggestion_id": suggestions[0]["suggestion_id"],
                    "expected_revision": 1,
                    "action": "edit",
                    "patch": {"front": "still globally clamped"},
                }
            ],
        )
        assert {
            failed["updated_at"],
            retried["updated_at"],
            queued_retry["updated_at"],
            db.get_study_suggestion_batch(second_batch["batch_id"])["updated_at"],
            reviewed["items"][0]["updated_at"],
        } == {global_bound.isoformat()}

        db.rename_domain("ml", "machine-learning")
        identity_times = {
            row["created_at"]
            for row in db._conn.execute(
                "SELECT created_at FROM study_suggestion_operations "
                "WHERE operation_kind='identity_transition'"
            ).fetchall()
        }
        assert identity_times == {global_bound.isoformat()}
        migration_current.validate(db._conn)


class TestStudySuggestionApi:
    @pytest.mark.asyncio
    async def test_create_poll_list_accept_and_mastery_contract(self, client, db):
        _seed(db)
        created = await client.post(
            "/api/study/suggestion-batches",
            json={
                "request_id": "api-batch",
                "domain": "ml",
                "job_ids": ["jobs_study_1"],
                "concept_terms": ["反向传播"],
                "max_cards": 5,
            },
        )
        assert created.status_code == 202
        batch = created.json()
        db.mark_study_suggestion_batch_queued(
            batch["batch_id"], task_id=batch["task_id"], expected_revision=1
        )
        persisted = db.get_study_suggestion_batch(batch["batch_id"])
        evidence = persisted["llm_request"]["evidence"][0]
        concept = persisted["llm_request"]["concepts"][0]
        suggestion = db.materialize_study_suggestions(
            batch["batch_id"],
            task_id=batch["task_id"],
            result={
                "schema_version": 1,
                "suggestions": [
                    {
                        "knowledge_key": "api-key",
                        "concept_input_id": concept["input_id"],
                        "card_type": "basic",
                        "front": "Q",
                        "back": "A",
                        "explanation": "E",
                        "evidence": [
                            {
                                "evidence_id": evidence["evidence_id"],
                                "quote": "链式法则",
                            }
                        ],
                    }
                ],
            },
        )[0]

        polled = await client.get(f"/api/study/suggestion-batches/{batch['batch_id']}")
        listed = await client.get(
            f"/api/study/suggestions?batch_id={batch['batch_id']}&status=suggested"
        )
        accepted = await client.post(
            "/api/study/suggestions/operations",
            json={
                "request_id": "api-accept",
                "batch_id": batch["batch_id"],
                "items": [
                    {
                        "suggestion_id": suggestion["suggestion_id"],
                        "expected_revision": 1,
                        "action": "accept",
                    }
                ],
            },
        )
        mastery = await client.get("/api/study/mastery?domain=ml")

        assert polled.status_code == 200
        assert polled.json()["status"] == "ready"
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert accepted.status_code == 200
        assert accepted.json()["items"][0]["status"] == "accepted"
        assert mastery.status_code == 200
        assert mastery.json() == {"total": 0, "items": []}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"request_id": "x", "domain": "ml", "max_cards": True},
            {"request_id": "x", "domain": "ml", "max_cards": 0},
            {"request_id": "x", "domain": "ml", "max_cards": 51},
            {"request_id": "x", "domain": "ml", "extra": "bad"},
            {"request_id": "   ", "domain": "ml"},
        ],
    )
    async def test_create_rejects_invalid_json_as_422(self, client, payload):
        assert (
            await client.post("/api/study/suggestion-batches", json=payload)
        ).status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,payload",
        [
            (
                "/api/study/suggestion-batches",
                {
                    "request_id": "study-lifecycle:batch_queued:external",
                    "domain": "ml",
                    "job_ids": ["jobs_study_1"],
                },
            ),
            (
                "/api/study/suggestions/operations",
                {
                    "request_id": "identity-transition:external",
                    "batch_id": "ssb_missing",
                    "items": [
                        {
                            "suggestion_id": "ss_missing",
                            "expected_revision": 1,
                            "action": "reject",
                            "reason": "duplicate",
                        }
                    ],
                },
            ),
        ],
    )
    async def test_internal_request_namespace_is_structured_422(
        self, client, db, path, payload
    ):
        _seed(db)
        response = await client.post(path, json=payload)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_and_idempotency_conflict_are_structured(self, client, db):
        missing = await client.get("/api/study/suggestion-batches/ssb_missing")
        assert missing.status_code == 404
        _seed(db)
        payload = {
            "request_id": "same-api-key",
            "domain": "ml",
            "job_ids": ["jobs_study_1"],
            "max_cards": 5,
        }
        assert (
            await client.post("/api/study/suggestion-batches", json=payload)
        ).status_code == 202
        payload["max_cards"] = 6
        conflict = await client.post("/api/study/suggestion-batches", json=payload)
        assert conflict.status_code == 409

    @pytest.mark.asyncio
    async def test_operation_boundaries_are_422_not_500(self, client):
        base = {
            "request_id": "op",
            "batch_id": "ssb_missing",
            "items": [
                {
                    "suggestion_id": "ss_missing",
                    "expected_revision": 1,
                    "action": "reject",
                    "reason": "   ",
                }
            ],
        }
        assert (
            await client.post("/api/study/suggestions/operations", json=base)
        ).status_code == 422
        base["items"][0]["expected_revision"] = 1 << 63
        assert (
            await client.post("/api/study/suggestions/operations", json=base)
        ).status_code == 422
        base["items"] = [
            {
                "suggestion_id": f"ss-{index}",
                "expected_revision": 1,
                "action": "accept",
            }
            for index in range(101)
        ]
        assert (
            await client.post("/api/study/suggestions/operations", json=base)
        ).status_code == 422

    @pytest.mark.asyncio
    async def test_accepted_card_delete_is_structured_409_not_sqlite_500(self, client, db):
        _seed(db)
        batch, suggestion = _ready(db)
        card = _accept(db, batch, suggestion)["cards"][0]

        response = await client.delete(f"/api/study/cards/{card['card_id']}")

        assert response.status_code == 409
        assert response.json()["message"]["code"] == "study_card_audit_protected"
        assert not db._conn.in_transaction
        assert db.get_study_card(card["card_id"]) is not None
