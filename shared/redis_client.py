"""Redis 客户端封装：队列 / 资源池 / Job 状态 / Worker / 事件。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import AsyncIterator

import redis.asyncio as aioredis


# ── Lua 脚本 ──

_LUA_ACQUIRE_SLOT = """
local frozen = redis.call('GET', KEYS[2])
if frozen == '1' then return 0 end
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
return 1
"""

_LUA_CAS_STATUS = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
    redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
    return 1
end
return 0
"""

_LUA_RELEASE_SLOT = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v > 0 then
    redis.call('DECR', KEYS[1])
    return 1
end
return 0
"""


class RedisClient:
    def __init__(self, url: str = "redis://localhost:6379/0"):
        self._url = url
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        # RESP2 让 decode_responses 对 hash 等 map 类型回复也生效。
        self._redis = aioredis.from_url(self._url, decode_responses=True, protocol=2)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def ping(self) -> bool:
        return await self.r.ping()

    @property
    def r(self) -> aioredis.Redis:
        assert self._redis is not None, "call connect() first"
        return self._redis

    # ── 队列操作 ──

    async def enqueue_step(
        self,
        pool: str,
        job_id: str,
        step: str,
        tags: list[str],
        priority: int,
        require_tags: list[str] | None = None,
    ) -> None:
        task = json.dumps(
            {"job_id": job_id, "step": step, "tags": sorted(tags),
             "require_tags": sorted(require_tags) if require_tags else []},
            sort_keys=True,
        )
        await self.r.zadd(f"queue:{pool}", {task: priority})

    async def dequeue_step(self, pool: str) -> tuple[dict, float] | None:
        items = await self.r.zpopmin(f"queue:{pool}", count=1)
        if not items:
            return None
        task_json, score = items[0]
        return json.loads(task_json), score

    async def return_step(self, pool: str, task_json: str, score: float) -> None:
        await self.r.zadd(f"queue:{pool}", {task_json: score})

    async def dequeue_step_raw(self, pool: str) -> tuple[str, dict, float] | None:
        """弹出最高优先级任务，返回 (raw_json, parsed_dict, score)。Worker 专用。"""
        items = await self.r.zpopmin(f"queue:{pool}", count=1)
        if not items:
            return None
        task_json, score = items[0]
        return task_json, json.loads(task_json), score

    async def get_queue_info(self, pool: str) -> dict:
        length = await self.r.zcard(f"queue:{pool}")
        return {"length": length}

    # ── 资源池（Lua 原子操作）──

    async def try_acquire_slot(self, pool: str, limit: int) -> bool:
        result = await self.r.eval(
            _LUA_ACQUIRE_SLOT,
            2,
            f"pool:{pool}:count",
            f"pool:{pool}:frozen",
            str(limit),
        )
        return result == 1

    async def release_slot(self, pool: str) -> bool:
        result = await self.r.eval(
            _LUA_RELEASE_SLOT, 1, f"pool:{pool}:count"
        )
        return result == 1

    async def freeze_pool(self, pool: str) -> None:
        await self.r.set(f"pool:{pool}:frozen", "1")

    async def unfreeze_pool(self, pool: str) -> None:
        await self.r.delete(f"pool:{pool}:frozen")

    async def is_pool_frozen(self, pool: str) -> bool:
        return await self.r.get(f"pool:{pool}:frozen") == "1"

    async def get_pool_count(self, pool: str) -> int:
        val = await self.r.get(f"pool:{pool}:count")
        return int(val) if val else 0

    # ── Job 实时状态 ──

    async def init_job(self, job_id: str, pipeline: str, info: dict) -> None:
        await self.r.hset(f"job:{job_id}", mapping={
            "pipeline": pipeline,
            **{k: json.dumps(v) if isinstance(v, (list, dict)) else str(v) for k, v in info.items()},
        })

    async def get_job_pipeline(self, job_id: str) -> str | None:
        return await self.r.hget(f"job:{job_id}", "pipeline")

    async def get_job_info(self, job_id: str) -> dict:
        data = await self.r.hgetall(f"job:{job_id}")
        return data or {}

    async def set_step_status(self, job_id: str, step: str, status: str) -> None:
        await self.r.hset(f"job:{job_id}:steps", step, status)

    async def get_step_status(self, job_id: str, step: str) -> str | None:
        return await self.r.hget(f"job:{job_id}:steps", step)

    async def get_all_step_statuses(self, job_id: str) -> dict[str, str]:
        return await self.r.hgetall(f"job:{job_id}:steps") or {}

    async def cas_step_status(
        self, job_id: str, step: str, expected: str, new: str
    ) -> bool:
        result = await self.r.eval(
            _LUA_CAS_STATUS,
            1,
            f"job:{job_id}:steps",
            step,
            expected,
            new,
        )
        return result == 1

    async def set_step_worker(self, job_id: str, step: str, worker_id: str) -> None:
        await self.r.hset(f"job:{job_id}:step_worker", step, worker_id)

    async def get_step_worker(self, job_id: str, step: str) -> str | None:
        return await self.r.hget(f"job:{job_id}:step_worker", step)

    async def incr_step_retries(self, job_id: str, step: str) -> int:
        return await self.r.hincrby(f"job:{job_id}:retries", step, 1)

    async def get_step_retries(self, job_id: str, step: str) -> int:
        val = await self.r.hget(f"job:{job_id}:retries", step)
        return int(val) if val else 0

    async def delete_step_status(self, job_id: str, step: str) -> None:
        await self.r.hdel(f"job:{job_id}:steps", step)

    async def cleanup_job(self, job_id: str) -> None:
        keys = [
            f"job:{job_id}",
            f"job:{job_id}:steps",
            f"job:{job_id}:retries",
            f"job:{job_id}:step_worker",
        ]
        await self.r.delete(*keys)

    # ── Worker ──

    async def register_worker(self, worker_id: str, info: dict, ttl: int = 30) -> None:
        await self.r.hset(f"worker:{worker_id}", mapping=info)
        await self.r.expire(f"worker:{worker_id}", ttl)

    async def heartbeat(self, worker_id: str, ttl: int = 30) -> None:
        key = f"worker:{worker_id}"
        await self.r.hset(key, "last_heartbeat", datetime.now().isoformat())
        await self.r.expire(key, ttl)

    async def set_worker_field(self, worker_id: str, field: str, value: str) -> None:
        await self.r.hset(f"worker:{worker_id}", field, value)

    async def get_worker_info(self, worker_id: str) -> dict | None:
        data = await self.r.hgetall(f"worker:{worker_id}")
        return data if data else None

    async def worker_exists(self, worker_id: str) -> bool:
        return await self.r.exists(f"worker:{worker_id}") > 0

    async def list_worker_ids(self) -> list[str]:
        keys = []
        async for key in self.r.scan_iter(match="worker:*"):
            worker_id = key.split(":", 1)[1]
            keys.append(worker_id)
        return keys

    # ── 活跃 Job 集合 ──

    async def add_active_job(self, job_id: str) -> None:
        await self.r.sadd("active_jobs", job_id)

    async def remove_active_job(self, job_id: str) -> None:
        await self.r.srem("active_jobs", job_id)

    async def get_active_jobs(self) -> set[str]:
        return await self.r.smembers("active_jobs")

    # ── 事件 Pub/Sub ──

    async def publish(self, channel: str, data: dict) -> None:
        await self.r.publish(channel, json.dumps(data, ensure_ascii=False))

    async def subscribe(self, *channels: str) -> AsyncIterator[dict]:
        pubsub = self.r.pubsub()
        await pubsub.subscribe(*channels)
        try:
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    yield json.loads(msg["data"])
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()
