"""Redis 客户端封装：队列 / 资源池 / Job 状态 / Worker / 事件。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import AsyncIterator

import redis.asyncio as aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)


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

    async def reconnect(self) -> None:
        """重建底层连接池（连接级异常后调用）。"""
        old = self._redis
        self._redis = None
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass
        await self.connect()

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
        # 仅测试用:生产认领走 dequeue_step_raw(runner_ops/worker transport)。保留薄实现供单测。
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

    async def set_step_exec_id(self, job_id: str, step: str, exec_id: str) -> None:
        # 记当前在跑的执行实例 id;迟到的旧执行完成事件据此识别并丢弃,防陈旧顶替/双执行。
        await self.r.hset(f"job:{job_id}:step_exec", step, exec_id)

    async def get_step_exec_id(self, job_id: str, step: str) -> str | None:
        return await self.r.hget(f"job:{job_id}:step_exec", step)

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
            f"job:{job_id}:step_exec",
        ]
        await self.r.delete(*keys)

    # ── Worker ──

    async def register_worker(self, worker_id: str, info: dict, ttl: int = 30) -> None:
        await self.r.hset(f"worker:{worker_id}", mapping=info)
        await self.r.expire(f"worker:{worker_id}", ttl)

    async def heartbeat(self, worker_id: str, ttl: int = 30) -> None:
        key = f"worker:{worker_id}"
        await self.r.hset(key, "last_heartbeat", datetime.now(timezone.utc).isoformat())
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
            # 防御:历史上接入 token 曾占 worker:registration_token(string,非 hash),
            # 会让后续 hgetall 报 WRONGTYPE 把 /api/workers 打成 500。跳过非 worker 键。
            if worker_id == "registration_token":
                continue
            keys.append(worker_id)
        return keys

    async def incr_worker_stat(
        self, worker_id: str, field: str, amount: int | float
    ) -> None:
        """累计 worker 统计到 Redis hash。整数走 HINCRBY，浮点走 HINCRBYFLOAT，
        避免整数字段被写成 '1.0' 让消费侧 int() 解析失败。"""
        key = f"worker:{worker_id}"
        if isinstance(amount, int):
            await self.r.hincrby(key, field, amount)
        else:
            await self.r.hincrbyfloat(key, field, amount)

    async def delete_worker(self, worker_id: str) -> None:
        """删掉 Redis 里的 worker 记录(liveness)。活着的远程 worker 仅删 SQLite
        会在下次扫描又冒出来，必须连 Redis key 一起清。"""
        await self.r.delete(f"worker:{worker_id}")

    # ── 接入 token（homelab 可复用 + 可重置）──

    # 不放 worker: 命名空间:否则 list_worker_ids 的 worker:* 扫描会把它当成 worker,
    # 对这个 string 键做 hgetall 触发 WRONGTYPE → /api/workers 500。
    _REGISTRATION_TOKEN_KEY = "runner:registration_token"

    async def get_registration_token(self) -> str | None:
        return await self.r.get(self._REGISTRATION_TOKEN_KEY)

    async def set_registration_token(self, token: str, ttl_sec: int | None = None) -> None:
        # ttl_sec 给接入 token 设过期,泄漏后自动失效;None 表示不过期(向后兼容)。
        if ttl_sec:
            await self.r.set(self._REGISTRATION_TOKEN_KEY, token, ex=ttl_sec)
        else:
            await self.r.set(self._REGISTRATION_TOKEN_KEY, token)

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
        """订阅频道并 yield 解码后的消息。

        实现要点（曾踩坑）：不用 ``pubsub.listen()`` 异步生成器——它在 redis
        关闭空闲 pubsub 连接后会静默挂死或停止迭代（订阅消失但不报错），导致
        调度器进程仍 Up 却收不到任何事件、任务永远 pending。改用带超时的
        ``get_message`` 轮询：连接断开会抛 Timeout/Connection 异常，被捕获后
        指数退避重连重订阅。绝不让异常逃逸导致上层崩溃；仅 CancelledError 透传。
        """
        import asyncio

        backoff = 1
        pubsub = None
        subscribed = False
        try:
            while True:
                if pubsub is None or not subscribed:
                    if pubsub is not None:
                        try:
                            await pubsub.aclose()
                        except Exception:
                            pass
                    pubsub = self.r.pubsub()
                    try:
                        await pubsub.subscribe(*channels)
                        subscribed = True
                        backoff = 1
                    except asyncio.CancelledError:
                        raise
                    except (RedisConnectionError, RedisTimeoutError, OSError):
                        subscribed = False
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                        try:
                            await self.reconnect()
                        except Exception:
                            pass
                        continue

                try:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                except asyncio.CancelledError:
                    raise
                except (RedisConnectionError, RedisTimeoutError, OSError):
                    # 连接级故障：标记需重订阅，退避后重连。
                    subscribed = False
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    try:
                        await self.reconnect()
                    except Exception:
                        pass
                    continue

                if msg is None:
                    continue
                if msg.get("type") == "message":
                    backoff = 1
                    yield json.loads(msg["data"])
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(*channels)
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
