"""学习建议经真 Redis 和生产 Worker 执行的持久闭环."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from scheduler.scheduler import Scheduler
from shared.models import Job, JobStatus, LLMResponse
from shared.storage import LocalStorage
from worker.transport import RedisTransport
from worker.worker import Worker


pytestmark = pytest.mark.integration
UTC = timezone.utc


class _ControlledGateway:
    """只替换外部 AI provider,其余认领,执行和持久化走生产实现."""

    def __init__(self, response: LLMResponse):
        self._response = response

    async def call(self, step_name, request):
        assert step_name == "study_suggestions"
        assert request.system
        return self._response


def _seed_knowledge(db) -> None:
    db.create_job(
        Job(
            id="study-e2e-job",
            content_type="article",
            pipeline="article",
            status=JobStatus.DONE,
            title="反向传播",
            domain="ml",
        )
    )
    db.index_job_notes(
        "study-e2e-job",
        "smart",
        "反向传播",
        "## 反向传播\n\n反向传播通过链式法则高效计算梯度。",
        content_type="article",
        domain="ml",
    )
    db.upsert_glossary_term(
        "ml", "反向传播", "用链式法则求梯度", status="accepted",
    )


def _result_for(batch: dict) -> dict:
    evidence = batch["llm_request"]["evidence"][0]
    quote = next(
        line.strip()
        for line in reversed(evidence["untrusted_body"].splitlines())
        if line.strip()
    )
    concept = batch["llm_request"]["concepts"][0]
    return {
        "schema_version": 1,
        "suggestions": [
            {
                "knowledge_key": "backprop-gradient",
                "concept_input_id": concept["input_id"],
                "card_type": "basic",
                "front": "反向传播解决什么问题?",
                "back": "它从输出误差向前高效计算各层梯度。",
                "explanation": "核心是链式法则。",
                "evidence": [
                    {"evidence_id": evidence["evidence_id"], "quote": quote},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_production_worker_reap_restart_and_mastery_closure(
    db, test_config, tmp_path, integration_redis, monkeypatch,
):
    """崩溃认领只回队一次,持久审计可在结果 TTL 丢失后恢复完整学习闭环."""
    redis = integration_redis
    _seed_knowledge(db)
    batch = db.create_study_suggestion_batch(
        request_id="study-e2e-create",
        domain="ml",
        job_ids=["study-e2e-job"],
        concept_terms=["反向传播"],
        max_cards=1,
    )

    first_scheduler = Scheduler(redis, db, test_config)
    assert await first_scheduler.reconcile_study_suggestion_batches() == 1
    queued = db.get_study_suggestion_batch(batch["batch_id"])
    assert queued is not None and queued["status"] == "queued"
    assert await redis.r.zcard("queue:ai") == 1

    expired = await redis.claim_ai_task(
        worker_id="crashed-ai-worker",
        claim_id="crashed-claim",
        tags={"claude-cli"},
        lease_seconds=1,
        now_epoch=time.time() - 10,
    )
    assert expired is not None and expired["task_id"] == batch["task_id"]
    assert await redis.r.zcard("queue:ai") == 0

    restarted_scheduler = Scheduler(redis, db, test_config)
    await restarted_scheduler.reconcile_study_suggestion_batches()
    reaped = await redis.get_ai_task_claim(batch["task_id"])
    assert reaped is not None
    assert reaped["state"] == "requeued" and reaped["requeue_count"] == 1
    assert await redis.r.zcard("queue:ai") == 1

    result = _result_for(queued)
    response = LLMResponse(
        content=json.dumps(result, ensure_ascii=False),
        model=queued["model"],
        provider=queued["provider"],
        input_tokens=12,
        output_tokens=34,
        cost_usd=0.01,
        duration_sec=0.1,
        attempts=[{"tier": "primary", "ok": True}],
    )
    monkeypatch.setattr(
        "worker.worker.AIGateway",
        lambda providers, pipelines: _ControlledGateway(response),
    )
    monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "study-ai-worker.id"))
    worker = Worker(
        RedisTransport(redis, db),
        test_config,
        LocalStorage(test_config.jobs_dir),
        worker_type="ai",
        pools=["ai"],
        tags={"claude-cli"},
        reject_tags=set(),
        concurrency=1,
    )
    await worker.register()
    claim = await worker.transport.request_step(
        worker.worker_id,
        worker.pools,
        worker._pool_limits(),
        worker.tags,
        worker.reject_tags,
    )
    assert claim is not None and claim["kind"] == "ai"
    assert claim["task_id"] == batch["task_id"]
    assert claim["requeue_count"] == 1
    await worker.execute(claim)

    terminal = await redis.get_ai_task_claim(batch["task_id"])
    assert terminal is not None and terminal["state"] == "succeeded"
    assert terminal["worker_id"] == worker.worker_id
    assert await redis.get_pool_count("ai") == 0
    assert await redis.r.zcard("queue:ai") == 0
    assert await redis.r.hlen("queue:enqueued") == 0
    assert await redis.r.zcard("ai:claims:expiry") == 0
    assert len(db.get_ai_task_logs(batch["task_id"])) == 1

    await redis.r.delete(f"airesult:{batch['task_id']}")
    result_scheduler = Scheduler(redis, db, test_config)
    assert await result_scheduler.reconcile_study_suggestion_batches() == 1
    ready = db.get_study_suggestion_batch(batch["batch_id"])
    assert ready is not None and ready["status"] == "ready"
    total, suggestions = db.list_study_suggestions(batch_id=batch["batch_id"])
    assert total == 1

    accepted = db.apply_study_suggestion_operations(
        request_id="study-e2e-accept",
        batch_id=batch["batch_id"],
        items=[
            {
                "suggestion_id": suggestions[0]["suggestion_id"],
                "expected_revision": suggestions[0]["revision"],
                "action": "accept",
                "patch": {},
            }
        ],
    )
    card = accepted["cards"][0]
    due_total, due = db.list_due_study_cards(domain="ml")
    assert due_total == 1 and due[0]["card_id"] == card["card_id"]
    reviewed = db.record_study_review(
        request_id="study-e2e-review",
        card_id=card["card_id"],
        grade="good",
        expected_revision=card["revision"],
        reviewed_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert reviewed["revision"] == 2
    mastery = db.get_study_mastery(domain="ml")
    assert mastery["total"] == 1
    assert mastery["items"][0]["score"] == 80

    counts = {
        table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "study_suggestion_batches",
            "study_suggestions",
            "study_cards",
            "study_reviews",
            "study_review_logs",
            "ai_task_logs",
        )
    }
    assert counts == {
        "study_suggestion_batches": 1,
        "study_suggestions": 1,
        "study_cards": 1,
        "study_reviews": 1,
        "study_review_logs": 1,
        "ai_task_logs": 1,
    }
    operation_rows = db._conn.execute(
        """SELECT ledger_seq, request_id FROM study_suggestion_operations
           ORDER BY ledger_seq"""
    ).fetchall()
    assert [row["ledger_seq"] for row in operation_rows] == [1, 2, 3, 4]
    assert len({row["request_id"] for row in operation_rows}) == 4
    assert await redis.r.keys("ai:claim:*") == [f"ai:claim:{batch['task_id']}"]
    assert await redis.r.exists(f"ai:submitted:{batch['task_id']}") == 1
