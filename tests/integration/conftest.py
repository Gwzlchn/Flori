"""真实依赖测试的隔离 fixture."""

from __future__ import annotations

import os

import pytest

from shared.redis_client import RedisClient


@pytest.fixture
async def integration_redis():
    """提供每例 flush 的真 Redis 客户端,连接由 integration compose 提供."""
    client = RedisClient(os.environ["INTEGRATION_REDIS_URL"])
    await client.connect()
    await client.r.flushdb()
    try:
        yield client
    finally:
        await client.r.flushdb()
        await client.close()
