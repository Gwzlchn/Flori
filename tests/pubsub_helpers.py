"""为 fakeredis pub/sub 测试提供可观测的订阅屏障."""

from __future__ import annotations

import asyncio


def subscription_barrier(redis_client, monkeypatch, *, expected: int = 1) -> asyncio.Event:
    """在预期数量的 subscribe 调用完成后置位,不依赖调度延时."""
    if expected < 1:
        raise ValueError("expected 必须大于 0")
    ready = asyncio.Event()
    subscribed = 0
    real_pubsub = redis_client.r.pubsub

    def tracked_pubsub(*args, **kwargs):
        pubsub = real_pubsub(*args, **kwargs)
        real_subscribe = pubsub.subscribe

        async def tracked_subscribe(*channels, **options):
            nonlocal subscribed
            result = await real_subscribe(*channels, **options)
            subscribed += 1
            if subscribed >= expected:
                ready.set()
            return result

        pubsub.subscribe = tracked_subscribe
        return pubsub

    monkeypatch.setattr(redis_client.r, "pubsub", tracked_pubsub)
    return ready
