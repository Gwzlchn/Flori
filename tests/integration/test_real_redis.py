"""用两个独立客户端验证真实 Redis 的跨连接与 Lua 原子语义."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from redis.exceptions import ResponseError

from shared.redis_client import AIEnqueueConflictError, RedisClient


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
