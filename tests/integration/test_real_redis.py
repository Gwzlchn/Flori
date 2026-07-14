"""用两个独立客户端验证真实 Redis 的跨连接与 Lua 原子语义."""

from __future__ import annotations

import asyncio
import os
import pytest

from shared.redis_client import RedisClient


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
