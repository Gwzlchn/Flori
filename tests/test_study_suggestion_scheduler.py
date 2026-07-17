"""学习建议 Prompt 快照和 Scheduler 持久对账测试."""

from __future__ import annotations

import base64
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scheduler.scheduler import Scheduler
from shared.models import Job, JobStatus
from shared.study_suggestions import resolve_study_suggestion_prompt
from tests.conftest import make_fakeredis


@pytest.fixture
async def redis():
    client = make_fakeredis()
    yield client
    await client.close()


def _seed(db, *, job_id: str = "job-suggestion-scheduler") -> None:
    db.create_job(
        Job(
            id=job_id,
            content_type="document",
            document_kind="article",
            pipeline="document",
            status=JobStatus.DONE,
            title="反向传播",
            domain="ml",
        )
    )
    db.index_job_notes(
        job_id,
        "smart",
        "反向传播",
        "反向传播通过链式法则高效计算梯度。",
        content_type="document",
        domain="ml",
    )
    db.upsert_glossary_term("ml", "反向传播", "链式法则求梯度", status="accepted")


def _result(batch: dict) -> dict:
    evidence = batch["llm_request"]["evidence"][0]
    concept = batch["llm_request"]["concepts"][0]
    return {
        "schema_version": 1,
        "suggestions": [{
            "knowledge_key": "backprop-chain-rule",
            "concept_input_id": concept["input_id"],
            "card_type": "basic",
            "front": "反向传播如何计算梯度?",
            "back": "沿计算图反向应用链式法则。",
            "explanation": "复用局部导数。",
            "evidence": [{
                "evidence_id": evidence["evidence_id"],
                "quote": "反向传播通过链式法则高效计算梯度。",
            }],
        }],
    }


def test_prompt_snapshot_preserves_exact_bytes_and_changes_new_generator(
    db, test_config, tmp_path,
) -> None:
    hot = tmp_path / "hot"
    hot.mkdir()
    image = test_config.config_dir / "prompts" / "templates"
    prompt_path = hot / "study_suggestions.md"
    original = b"first line\r\nsecond line\r\n"
    prompt_path.write_bytes(original)
    first = resolve_study_suggestion_prompt(hot_dir=hot, image_dir=image)
    _seed(db)
    batch = db.create_study_suggestion_batch(
        request_id="prompt-snapshot-1",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
        concept_terms=["反向传播"],
        prompt_snapshot=first,
    )

    prompt_path.write_text("changed", encoding="utf-8")
    second = resolve_study_suggestion_prompt(hot_dir=hot, image_dir=image)

    persisted = batch["llm_request"]["prompt_snapshot"]
    assert base64.b64decode(persisted["content_b64"]) == original
    assert persisted["source"] == "hot" and persisted["version"] is None
    assert persisted["sha256"].startswith("sha256:")
    assert batch["generator_fingerprint"].startswith("sha256:")
    assert second["sha256"] != first["sha256"]
    assert "path" not in persisted
    assert Scheduler._study_suggestion_ai_payload(batch)["request"]["system"] == original.decode()


@pytest.mark.asyncio
async def test_two_schedulers_enqueue_once_and_harvest_redis_result(
    db, redis, test_config,
) -> None:
    _seed(db)
    created = db.create_study_suggestion_batch(
        request_id="scheduler-create-1",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
        concept_terms=["反向传播"],
    )
    first = Scheduler(redis, db, test_config)
    second = Scheduler(redis, db, test_config)

    await asyncio.gather(
        first.reconcile_study_suggestion_batches(),
        second.reconcile_study_suggestion_batches(),
    )
    queued = db.get_study_suggestion_batch(created["batch_id"])
    assert queued["status"] == "queued"
    assert await redis.r.zcard("queue:ai") == 1

    await redis.set_ai_result(
        created["task_id"], {"content": json.dumps(_result(queued), ensure_ascii=False)},
    )
    assert await first.reconcile_study_suggestion_batches() == 1
    ready = db.get_study_suggestion_batch(created["batch_id"])
    assert ready["status"] == "ready" and ready["suggestion_count"] == 1


@pytest.mark.asyncio
async def test_scheduler_recovers_expired_redis_result_from_persistent_log(
    db, redis, test_config,
) -> None:
    _seed(db)
    created = db.create_study_suggestion_batch(
        request_id="scheduler-log-create",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
        concept_terms=["反向传播"],
    )
    scheduler = Scheduler(redis, db, test_config)
    await scheduler.reconcile_study_suggestion_batches()
    queued = db.get_study_suggestion_batch(created["batch_id"])
    result = _result(queued)
    assert db.record_ai_task_log({
        "task_id": created["task_id"],
        "exec_id": "worker:log",
        "step_name": "study_suggestions",
        "domain": "ml",
        "provider": "claude-cli",
        "model": "test",
        "ok": True,
        "record": {"task_id": created["task_id"], "output": json.dumps(result)},
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    await scheduler.reconcile_study_suggestion_batches()
    assert db.get_study_suggestion_batch(created["batch_id"])["status"] == "ready"


@pytest.mark.asyncio
async def test_old_task_result_cannot_cross_retry_revision(
    db, redis, test_config,
) -> None:
    _seed(db)
    created = db.create_study_suggestion_batch(
        request_id="scheduler-old-task",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
        concept_terms=["反向传播"],
    )
    scheduler = Scheduler(redis, db, test_config)
    await scheduler.reconcile_study_suggestion_batches()
    queued = db.get_study_suggestion_batch(created["batch_id"])
    failed = db.fail_study_suggestion_batch(
        queued["batch_id"], task_id=queued["task_id"],
        expected_revision=queued["revision"], error_code="forced", error_message="forced",
    )
    retried = db.retry_study_suggestion_batch(
        failed["batch_id"], request_id="scheduler-retry",
        expected_revision=failed["revision"],
    )
    await redis.set_ai_result(
        created["task_id"], {"content": json.dumps(_result(queued))},
    )

    await scheduler.reconcile_study_suggestion_batches()
    current = db.get_study_suggestion_batch(created["batch_id"])
    assert current["task_id"] == retried["task_id"]
    assert current["status"] == "queued" and current["result"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_state", ["queued", "claimed", "executing"])
async def test_deadline_cancels_only_before_provider_execution(
    db, redis, test_config, execution_state,
) -> None:
    _seed(db)
    created = db.create_study_suggestion_batch(
        request_id=f"scheduler-deadline-{execution_state}",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
        concept_terms=["反向传播"],
        deadline_seconds=60,
    )
    scheduler = Scheduler(redis, db, test_config)
    await scheduler.reconcile_study_suggestion_batches()
    queued = db.get_study_suggestion_batch(created["batch_id"])
    claim = None
    if execution_state != "queued":
        claim = await redis.claim_ai_task(
            worker_id="deadline-worker",
            claim_id="deadline-claim",
            tags={"claude-cli"},
            lease_seconds=3_600,
        )
        assert claim is not None
        if execution_state == "executing":
            assert await redis.mark_ai_task_executing(
                task_id=claim["task_id"],
                batch_id=claim["batch_id"],
                attempt=claim["attempt"],
                revision=claim["revision"],
                worker_id=claim["worker_id"],
                claim_id=claim["claim_id"],
            )

    after_deadline = datetime.fromisoformat(queued["deadline_at"]) + timedelta(seconds=1)
    await scheduler.reconcile_study_suggestion_batches(now=after_deadline)
    current = db.get_study_suggestion_batch(created["batch_id"])
    persisted_claim = await redis.get_ai_task_claim(created["task_id"])

    if execution_state == "executing":
        assert current["status"] == "queued"
        assert persisted_claim["state"] == "executing"
    else:
        assert current["status"] == "failed"
        assert current["error_code"] == "ai_task_timeout"
        assert persisted_claim["state"] == "canceled"
        assert await redis.r.zcard("queue:ai") == 0


@pytest.mark.asyncio
async def test_deadline_cancel_race_never_advances_database(
    db, redis, test_config,
) -> None:
    _seed(db)
    created = db.create_study_suggestion_batch(
        request_id="scheduler-deadline-race",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
        concept_terms=["反向传播"],
        deadline_seconds=60,
    )
    scheduler = Scheduler(redis, db, test_config)
    await scheduler.reconcile_study_suggestion_batches()
    queued = db.get_study_suggestion_batch(created["batch_id"])
    redis.cancel_ai_task_before_execution = AsyncMock(return_value="race")

    await scheduler.reconcile_study_suggestion_batches(
        now=datetime.fromisoformat(queued["deadline_at"]) + timedelta(seconds=1),
    )

    assert db.get_study_suggestion_batch(created["batch_id"])["status"] == "queued"
    assert await redis.r.zcard("queue:ai") == 1


def test_monotonic_clock_uses_ledger_tail_index_and_clamps_cross_batch(
    db,
) -> None:
    _seed(db)
    first = db.create_study_suggestion_batch(
        request_id="clock-tail-first",
        domain="ml",
        job_ids=["job-suggestion-scheduler"],
    )
    for index in range(200):
        db.create_study_suggestion_batch(
            request_id=f"clock-tail-replay-{index}",
            domain="ml",
            job_ids=["job-suggestion-scheduler"],
        )
    plan = db._conn.execute(
        "EXPLAIN QUERY PLAN SELECT created_at FROM study_suggestion_operations "
        "ORDER BY ledger_seq DESC LIMIT 1"
    ).fetchall()
    assert not any("TEMP B-TREE" in str(row[3]).upper() for row in plan)
    tail = db._conn.execute(
        "SELECT created_at FROM study_suggestion_operations ORDER BY ledger_seq DESC LIMIT 1"
    ).fetchone()[0]
    clamped = db._study_suggestion_monotonic_now_locked(
        [first["batch_id"]], datetime.now(timezone.utc) - timedelta(days=30),
    )
    assert clamped == datetime.fromisoformat(tail)
