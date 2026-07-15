"""用两个独立客户端验证真实 Redis 的跨连接与 Lua 原子语义."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ResponseError

from shared.redis_client import AIEnqueueConflictError, RedisClient
from api.main import create_app


pytestmark = pytest.mark.integration


@pytest.fixture
async def real_redis_clients(integration_redis):
    first = integration_redis
    second = RedisClient(os.environ["INTEGRATION_REDIS_URL"])
    await second.connect()
    try:
        yield first, second
    finally:
        await second.close()


async def test_two_clients_share_server_but_not_connections(real_redis_clients) -> None:
    first, second = real_redis_clients

    first_id, second_id = await asyncio.gather(
        first.r.client_id(), second.r.client_id(),
    )
    assert first_id != second_id

    await first.r.hset("integration:handoff", mapping={"state": "ready"})
    assert await second.r.hget("integration:handoff", "state") == "ready"


async def test_pubsub_handoff_uses_subscription_ack(real_redis_clients) -> None:
    first, second = real_redis_clients
    pubsub = first.r.pubsub()
    try:
        await pubsub.subscribe("integration:events")
        ack = await asyncio.wait_for(pubsub.get_message(timeout=2), timeout=3)
        assert ack is not None and ack["type"] == "subscribe"

        subscribers = await second.r.publish("integration:events", "ready")
        assert subscribers == 1
        message = await asyncio.wait_for(
            pubsub.get_message(ignore_subscribe_messages=True, timeout=2),
            timeout=3,
        )
        assert message is not None
        assert message["data"] == "ready"
    finally:
        await pubsub.aclose()


async def test_lua_slot_limit_is_atomic_across_clients(real_redis_clients) -> None:
    first, second = real_redis_clients
    acquired = await asyncio.gather(
        first.try_acquire_slot("integration", 1, "exec-a"),
        second.try_acquire_slot("integration", 1, "exec-b"),
    )

    assert sorted(acquired) == [False, True]
    assert await first.get_pool_count("integration") == 1
    holders = await second.get_pool_holders("integration")
    assert holders in ({"exec-a"}, {"exec-b"})


async def test_api_rate_limit_is_atomic_across_clients(real_redis_clients) -> None:
    first, second = real_redis_clients

    outcomes = await asyncio.gather(
        first.consume_rate_limit("jobs:create", "token:shared", 1, 60),
        second.consume_rate_limit("jobs:create", "token:shared", 1, 60),
    )

    assert sorted(result[0] for result in outcomes) == [False, True]
    assert sorted(result[1] for result in outcomes) == [1, 2]
    assert all(1 <= result[2] <= 60 for result in outcomes)


async def test_two_apps_compete_for_one_shared_job_quota(
    real_redis_clients, db, test_config, monkeypatch,
) -> None:
    first, second = real_redis_clients
    monkeypatch.setenv("FLORI_JOBS_CREATE_RATE_LIMIT", "1")
    monkeypatch.setenv("FLORI_JOBS_CREATE_RATE_WINDOW_SEC", "60")
    worker = {
        "pools": "io,cpu,ai", "tags": "claude-cli,read,vision,net-cn,net-global",
        "reject_tags": "", "status": "idle", "admin_status": "active",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
    }
    await first.register_worker("integration-all", worker)
    apps = [
        create_app(db=db, redis=first, config=test_config),
        create_app(db=db, redis=second, config=test_config),
    ]
    clients = [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        for app in apps
    ]
    try:
        responses = await asyncio.gather(
            clients[0].post("/api/jobs", json={"url": "BV1xx411c7mD"}),
            clients[1].post("/api/jobs", json={"url": "BV1xx411c7mE"}),
        )
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))

    assert sorted(response.status_code for response in responses) == [201, 429]
    rejected = next(response for response in responses if response.status_code == 429)
    assert rejected.json()["error"] == "rate_limited"
    assert int(rejected.headers["retry-after"]) > 0
    assert db.list_jobs(limit=10)[0] == 1


async def test_real_redis_wrongtype_rate_key_is_stable_unavailable(
    integration_redis, db, test_config, monkeypatch,
) -> None:
    monkeypatch.setenv("API_TOKEN", "integration-secret")
    monkeypatch.delenv("API_ALLOW_NO_AUTH", raising=False)
    principal = "token:" + hashlib.sha256(b"integration-secret").hexdigest()
    key = "rate:api:jobs_create:" + hashlib.sha256(principal.encode()).hexdigest()
    await integration_redis.r.hset(key, mapping={"bad": "type"})
    app = create_app(db=db, redis=integration_redis, config=test_config)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/jobs", json={"url": "BV1xx411c7mD"},
            headers={"Authorization": "Bearer integration-secret"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    assert db.list_jobs(limit=1)[0] == 0


async def test_pipeline_claim_and_terminal_fence_are_atomic_across_clients(
    real_redis_clients,
) -> None:
    first, second = real_redis_clients
    await first.init_job("integration-claim", "test", {})
    await first.set_step_status("integration-claim", "A", "ready")
    await first.enqueue_step("cpu", "integration-claim", "A", [], priority=0)

    claims = await asyncio.gather(
        first.claim_pipeline_step_atomic(
            pool="cpu", worker_id="worker-a", exec_id="exec-a",
            default_limit=1, tags=set(), reject_tags=set(),
        ),
        second.claim_pipeline_step_atomic(
            pool="cpu", worker_id="worker-b", exec_id="exec-b",
            default_limit=1, tags=set(), reject_tags=set(),
        ),
    )
    winner = next(claim for claim in claims if claim is not None)
    assert sum(claim is not None for claim in claims) == 1
    assert await first.get_step_status("integration-claim", "A") == "running"
    assert await first.get_step_exec_id("integration-claim", "A") == winner["exec_id"]
    assert await first.r.zcard("queue:cpu") == 0
    assert await first.get_pool_count("cpu") == 1

    generation = winner["generation"]
    terminals = await asyncio.gather(
        first.try_finalize_job("integration-claim", generation, "done"),
        second.try_finalize_job("integration-claim", generation, "failed"),
    )
    assert sorted(terminals) == [0, 1]


async def test_lifecycle_stream_survives_offline_and_reclaims_unacked(
    real_redis_clients,
) -> None:
    first, second = real_redis_clients
    message_id = await first.append_lifecycle_event(
        "job_command", {"action": "new_job", "job_id": "offline-job"},
    )

    first_delivery = await second.read_lifecycle_events(
        "scheduler-old", block_ms=1, reclaim_idle_ms=0,
    )
    assert first_delivery[0][0] == message_id
    reclaimed = await first.read_lifecycle_events(
        "scheduler-new", block_ms=1, reclaim_idle_ms=0,
    )
    assert reclaimed[0][0] == message_id
    assert json.loads(reclaimed[0][1]["payload"])["job_id"] == "offline-job"

    await first.ack_lifecycle_event(message_id)
    assert await first.r.xlen(first.LIFECYCLE_STREAM) == 0
    assert (await first.r.xpending(first.LIFECYCLE_STREAM, first.LIFECYCLE_GROUP))["pending"] == 0


async def test_lifecycle_poison_isolated_without_blocking_next_message(
    integration_redis,
) -> None:
    await integration_redis.ensure_lifecycle_group()
    poison_id = await integration_redis.r.xadd(
        integration_redis.LIFECYCLE_STREAM,
        {"topic": "job_command", "payload": "not-json"},
    )
    good_id = await integration_redis.append_lifecycle_event(
        "job_command", {"action": "new_job", "job_id": "good-job"},
    )
    messages = await integration_redis.read_lifecycle_events(
        "scheduler", block_ms=1, reclaim_idle_ms=0,
    )
    by_id = dict(messages)
    assert poison_id in by_id and good_id in by_id
    assert await integration_redis.reject_lifecycle_event(
        poison_id, by_id[poison_id], "invalid json", max_attempts=1,
    )
    await integration_redis.ack_lifecycle_event(good_id)
    assert await integration_redis.r.xlen(
        integration_redis.LIFECYCLE_POISON_STREAM,
    ) == 1
    assert await integration_redis.r.xlen(integration_redis.LIFECYCLE_STREAM) == 0


async def test_terminal_append_rejects_old_exec_duplicate_and_terminal_sibling(
    integration_redis,
) -> None:
    await integration_redis.init_job("terminal-job", "test", {})
    for step, exec_id in (("A", "exec-a"), ("B", "exec-b")):
        await integration_redis.set_step_status("terminal-job", step, "running")
        await integration_redis.set_step_exec_id("terminal-job", step, exec_id)
        await integration_redis.r.hset(
            "job:terminal-job:step_generation", step, "1",
        )
    stale, _ = await integration_redis.append_terminal_if_current(
        "step_completed",
        {"job_id": "terminal-job", "step": "A", "exec_id": "old", "generation": 1},
    )
    first, message_id = await integration_redis.append_terminal_if_current(
        "step_completed",
        {"job_id": "terminal-job", "step": "A", "exec_id": "exec-a", "generation": 1},
    )
    duplicate, duplicate_id = await integration_redis.append_terminal_if_current(
        "step_completed",
        {"job_id": "terminal-job", "step": "A", "exec_id": "exec-a", "generation": 1},
    )
    assert (stale, first, duplicate) == (0, 1, 2)
    assert duplicate_id == message_id
    assert await integration_redis.r.xlen(integration_redis.LIFECYCLE_STREAM) == 1

    owner = "finalizer"
    assert await integration_redis.acquire_job_finalizer(
        "terminal-job", 1, "failed", owner, now=100, lease_sec=10,
    ) == 1
    sibling, _ = await integration_redis.append_terminal_if_current(
        "step_completed",
        {"job_id": "terminal-job", "step": "B", "exec_id": "exec-b", "generation": 1},
    )
    assert sibling == 0


async def test_ai_enqueue_once_is_unique_and_rejects_payload_conflict(
    real_redis_clients,
) -> None:
    first, second = real_redis_clients
    payload = {
        "kind": "ai",
        "task_id": "integration-ai-once",
        "request": {"messages": [{"role": "user", "content": "same"}]},
    }

    results = await asyncio.gather(
        first.enqueue_ai_task_once(payload, priority=-3),
        second.enqueue_ai_task_once(payload, priority=-3),
    )
    assert sorted(results) == [False, True]
    assert await first.r.zrange("queue:ai", 0, -1, withscores=True) == [
        (json.dumps(payload, sort_keys=True), -3.0)
    ]
    assert await first.r.hgetall("queue:enqueued") == {
        "ai|ai|integration-ai-once": await first.r.hget(
            "queue:enqueued", "ai|ai|integration-ai-once"
        )
    }

    conflicting = {**payload, "request": {"messages": []}}
    with pytest.raises(AIEnqueueConflictError) as conflict:
        await second.enqueue_ai_task_once(conflicting)
    assert conflict.value.code == "ai_task_payload_conflict"
    assert await first.r.zcard("queue:ai") == 1
    assert json.loads(await first.r.get("ai:submitted:integration-ai-once")) == payload


async def test_ai_claim_is_atomic_across_clients_and_stale_owner_is_rejected(
    real_redis_clients,
) -> None:
    first, second = real_redis_clients
    payload = {
        "kind": "ai", "task_id": "integration-ai-claim",
        "batch_id": "ssb-integration", "attempt": 2, "revision": 7,
        "request": {},
    }
    await first.enqueue_ai_task_once(payload)

    claims = await asyncio.gather(
        first.claim_ai_task(worker_id="worker-a", lease_seconds=60, now_epoch=100),
        second.claim_ai_task(worker_id="worker-b", lease_seconds=60, now_epoch=100),
    )
    winner = next(claim for claim in claims if claim is not None)
    assert sum(claim is not None for claim in claims) == 1
    assert await first.r.zcard("queue:ai") == 0
    assert not await second.mark_ai_task_executing(
        task_id=payload["task_id"], batch_id=payload["batch_id"],
        claim_id=winner["claim_id"], worker_id="worker-attacker",
        attempt=2, revision=7, now_epoch=101,
    )
    owner = winner["worker_id"]
    client = first if owner == "worker-a" else second
    assert await client.mark_ai_task_executing(
        task_id=payload["task_id"], batch_id=payload["batch_id"],
        claim_id=winner["claim_id"], worker_id=owner,
        attempt=2, revision=7, now_epoch=101,
    )


async def test_real_redis_executing_expiry_is_ambiguous_without_requeue(
    integration_redis,
) -> None:
    payload = {
        "kind": "ai", "task_id": "integration-ai-ambiguous",
        "batch_id": "ssb-ambiguous", "attempt": 1, "revision": 2,
        "request": {},
    }
    await integration_redis.enqueue_ai_task_once(payload)
    claim = await integration_redis.claim_ai_task(
        worker_id="worker-paid", lease_seconds=10, now_epoch=200,
    )
    assert await integration_redis.mark_ai_task_executing(
        task_id=payload["task_id"], batch_id=payload["batch_id"],
        claim_id=claim["claim_id"], worker_id="worker-paid",
        attempt=1, revision=2, now_epoch=201,
    )

    assert await integration_redis.reconcile_ai_task_claims(now_epoch=212) == [
        {"task_id": payload["task_id"], "action": "ambiguous"}
    ]
    assert await integration_redis.r.zcard("queue:ai") == 0


async def test_real_redis_cancel_wrongtype_preserves_claim_and_expiry(
    integration_redis,
) -> None:
    payload = {
        "kind": "ai", "task_id": "integration-cancel-wrongtype",
        "batch_id": "ssb-cancel-wrongtype", "attempt": 1, "revision": 2,
        "step": "synthesis", "request": {},
    }
    await integration_redis.enqueue_ai_task_once(payload)
    await integration_redis.claim_ai_task(
        worker_id="worker-cancel", claim_id="claim-cancel",
        lease_seconds=60, now_epoch=300,
    )
    expiry_before = await integration_redis.r.zscore(
        "ai:claims:expiry", payload["task_id"],
    )
    await integration_redis.r.set("pool:ai:holders", "wrong-type")

    with pytest.raises(ResponseError, match="WRONGTYPE"):
        await integration_redis.cancel_ai_task_before_execution(payload)

    claim = await integration_redis.get_ai_task_claim(payload["task_id"])
    assert claim["state"] == "claimed"
    assert claim["lease_until"] == expiry_before
    assert await integration_redis.r.zscore(
        "ai:claims:expiry", payload["task_id"],
    ) == expiry_before


@pytest.mark.parametrize(
    ("wrong_key", "wrong_value"),
    [
        ("ai:submitted:integration-wrongtype", {"field": "value"}),
        ("queue:ai", "not-a-zset"),
        ("queue:enqueued", "not-a-hash"),
    ],
)
async def test_ai_enqueue_once_wrongtype_has_no_partial_writes(
    integration_redis,
    wrong_key: str,
    wrong_value: object,
) -> None:
    payload = {
        "kind": "ai",
        "task_id": "integration-wrongtype",
        "request": {},
    }
    if isinstance(wrong_value, dict):
        await integration_redis.r.hset(wrong_key, mapping=wrong_value)
    else:
        await integration_redis.r.set(wrong_key, wrong_value)

    with pytest.raises(ResponseError, match="WRONGTYPE"):
        await integration_redis.enqueue_ai_task_once(payload)

    if wrong_key != "ai:submitted:integration-wrongtype":
        assert not await integration_redis.r.exists(
            "ai:submitted:integration-wrongtype"
        )
    if wrong_key != "queue:ai":
        assert await integration_redis.r.zcard("queue:ai") == 0
    if wrong_key != "queue:enqueued":
        assert await integration_redis.r.hlen("queue:enqueued") == 0
