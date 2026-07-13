"""Redis 客户端封装:队列 / 资源池 / Job 状态 / Worker / 事件。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import AsyncIterator

import redis.asyncio as aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from shared.status import DEFAULT_ONLINE_WINDOW_SEC


# Lua 脚本

_LUA_ACQUIRE_SLOT = """
-- 占池槽:KEYS[1]=holders SET,KEYS[2]=frozen;ARGV[1]=limit,ARGV[2]=holder。
-- used=SCARD,占=SADD,配对释放走 SREM(幂等,无双减/幽灵泄漏)。已持有则幂等放行:满载时同一执行重占不被误拒。
local frozen = redis.call('GET', KEYS[2])
if frozen == '1' then return 0 end
if redis.call('SISMEMBER', KEYS[1], ARGV[2]) == 1 then return 1 end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[2])
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
-- 放池槽:KEYS[1]=holders SET,ARGV[1]=holder。SREM 幂等:放两次或放不存在的都是安全 no-op,无双减。
return redis.call('SREM', KEYS[1], ARGV[1])
"""

_LUA_VALIDATE_TASK_LEASE = """
-- KEYS: lease hash,step worker hash,step exec hash,step status hash.
-- ARGV: worker_id,job_id,step,exec_id,ttl,renew,require_active,expected_pool.
if redis.call('HGET', KEYS[1], 'worker_id') ~= ARGV[1]
    or redis.call('HGET', KEYS[1], 'job_id') ~= ARGV[2]
    or redis.call('HGET', KEYS[1], 'step') ~= ARGV[3]
    or redis.call('HGET', KEYS[1], 'exec_id') ~= ARGV[4] then
    return 0
end
if ARGV[8] ~= '' and redis.call('HGET', KEYS[1], 'pool') ~= ARGV[8] then return 0 end
if redis.call('HGET', KEYS[2], ARGV[3]) ~= ARGV[1]
    or redis.call('HGET', KEYS[3], ARGV[3]) ~= ARGV[4] then
    return 0
end
if ARGV[7] == '1' then
    if redis.call('HGET', KEYS[4], ARGV[3]) ~= 'running'
        or redis.call('HGET', KEYS[1], 'terminal') then
        return 0
    end
end
if ARGV[6] == '1' then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
end
return 1
"""

_LUA_BEGIN_TASK_TERMINAL = """
-- 与普通守卫相同,但原子占用 terminal.1=首次,2=同结果重放,0=无效或冲突.
if redis.call('HGET', KEYS[1], 'worker_id') ~= ARGV[1]
    or redis.call('HGET', KEYS[1], 'job_id') ~= ARGV[2]
    or redis.call('HGET', KEYS[1], 'step') ~= ARGV[3]
    or redis.call('HGET', KEYS[1], 'exec_id') ~= ARGV[4]
    or redis.call('HGET', KEYS[2], ARGV[3]) ~= ARGV[1]
    or redis.call('HGET', KEYS[3], ARGV[3]) ~= ARGV[4] then
    return 0
end
if ARGV[7] ~= '' and redis.call('HGET', KEYS[1], 'pool') ~= ARGV[7] then return 0 end
local terminal = redis.call('HGET', KEYS[1], 'terminal')
if terminal then
    if terminal == ARGV[5] then return 2 end
    return 0
end
if redis.call('HGET', KEYS[4], ARGV[3]) ~= 'running' then return 0 end
redis.call('HSET', KEYS[1], 'terminal', ARGV[5])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[6]))
return 1
"""

_LUA_RESET_TASK_TERMINAL = """
if redis.call('HGET', KEYS[1], 'worker_id') == ARGV[1]
    and redis.call('HGET', KEYS[1], 'job_id') == ARGV[2]
    and redis.call('HGET', KEYS[1], 'step') == ARGV[3]
    and redis.call('HGET', KEYS[1], 'exec_id') == ARGV[4]
    and redis.call('HGET', KEYS[1], 'terminal') == ARGV[5] then
    redis.call('HDEL', KEYS[1], 'terminal')
    return 1
end
return 0
"""


class RedisClient:
    TASK_LEASE_TTL_SEC = 180

    def __init__(self, url: str = "redis://localhost:6379/0"):
        self._url = url
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        # RESP2 让 decode_responses 对 hash 等 map 类型回复也生效。
        self._redis = aioredis.from_url(self._url, decode_responses=True, protocol=2)

    async def reconnect(self) -> None:
        """重建底层连接池(连接级异常后调用)。"""
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

    # 队列操作

    @staticmethod
    def _enqueued_field(pool: str, parsed: dict) -> str:
        """queue:enqueued 的 field key(kind 感知):
        pipeline-step task 用 {pool}|{job_id}|{step};
        AI task(kind='ai')用 {pool}|ai|{task_id}(无 job_id,用 task_id)."""
        if parsed.get("kind") == "ai":
            return f"{pool}|ai|{parsed.get('task_id')}"
        return f"{pool}|{parsed.get('job_id')}|{parsed.get('step')}"

    async def enqueue_step(
        self,
        pool: str,
        job_id: str,
        step: str,
        tags: list[str],
        priority: int,
        require_tags: list[str] | None = None,
        resources: list[str] | None = None,
    ) -> None:
        payload = {
            "job_id": job_id, "step": step, "tags": sorted(tags),
            "require_tags": sorted(require_tags) if require_tags else [],
        }
        # 仅在声明了资源槽时才写 resources 键:无声明时 task JSON 不含该键(向后兼容)。
        if resources:
            payload["resources"] = sorted(resources)
        task = json.dumps(payload, sort_keys=True)
        await self.r.zadd(f"queue:{pool}", {task: priority})
        # 入队时间戳存独立 hash(不进 ZSET 成员,避免改成员破坏 ZADD 去重),供展示已等待多久。
        try:
            await self.r.hset("queue:enqueued", self._enqueued_field(pool, payload), str(time.time()))
        except Exception:
            pass

    async def enqueue_ai_task(self, payload: dict, priority: int = 0) -> None:
        """投递独立 AI task(kind='ai')到 queue:ai。payload 由 AITask.to_task_payload() 生成
        (内联 LLMRequest + require_tags=[provider]);供 /api/ask、/digest 把 CLI/API 调用交给 ai-worker。
        与普通 task 同框、能进 /system 队列窥视;由 ai-worker 认领执行,结果回 airesult:{task_id}。"""
        task = json.dumps(payload, sort_keys=True)
        await self.r.zadd("queue:ai", {task: priority})
        try:
            await self.r.hset("queue:enqueued", self._enqueued_field("ai", payload), str(time.time()))
        except Exception:
            pass

    async def set_ai_result(self, task_id: str, result: dict, ttl: int = 600) -> None:
        """回写 AI task 结果(LLMResponse.to_jsonable() 或 {'error': ...}),airesult:{task_id} 带 TTL,供 API 取回。"""
        await self.r.set(f"airesult:{task_id}", json.dumps(result, ensure_ascii=False), ex=ttl)

    async def get_ai_result(self, task_id: str) -> dict | None:
        """取 AI task 结果;未就绪/已过期时返回 None."""
        raw = await self.r.get(f"airesult:{task_id}")
        return json.loads(raw) if raw else None

    # 自动周报投递锁。

    async def try_mark_auto_digest(self, domain: str, day: str, ttl_sec: int = 3 * 86400) -> bool:
        """自动周报当日锁(radar:digest:auto:{domain}:{day},SET NX):True=首次(可投),
        False=当日已处理。periodic 循环 30s 一拍,靠这把锁幂等。"""
        return bool(await self.r.set(
            f"radar:digest:auto:{domain}:{day}", "1", nx=True, ex=ttl_sec,
        ))

    async def set_latest_auto_digest(self, domain: str, info: dict) -> None:
        """最新自动周报(radar:digest:latest:{domain},无 TTL,下次覆盖):
        {task_id, queued_at, [markdown, generated_at, error]}。airesult 的 TTL 只有约 600s,
        自动周报没人守屏,调度器收割结果后搬到这里长存。"""
        await self.r.set(
            f"radar:digest:latest:{domain}", json.dumps(info, ensure_ascii=False),
        )

    async def get_latest_auto_digest(self, domain: str) -> dict | None:
        raw = await self.r.get(f"radar:digest:latest:{domain}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def dequeue_step(self, pool: str) -> tuple[dict, float] | None:
        # 仅测试用:生产认领走 dequeue_step_raw(runner_ops/worker transport)。保留薄实现供单测。
        items = await self.r.zpopmin(f"queue:{pool}", count=1)
        if not items:
            return None
        task_json, score = items[0]
        return json.loads(task_json), score

    async def return_step(self, pool: str, task_json: str, score: float) -> None:
        await self.r.zadd(f"queue:{pool}", {task_json: score})
        # 退回队列 = 重新等待,重置入队时间戳。
        try:
            t = json.loads(task_json)
            await self.r.hset("queue:enqueued", self._enqueued_field(pool, t), str(time.time()))
        except Exception:
            pass

    async def dequeue_step_raw(self, pool: str) -> tuple[str, dict, float] | None:
        """弹出最高优先级任务,返回 (raw_json, parsed_dict, score)。Worker 专用。"""
        items = await self.r.zpopmin(f"queue:{pool}", count=1)
        if not items:
            return None
        task_json, score = items[0]
        parsed = json.loads(task_json)
        # 出队即离开队列,清入队时间戳避免 hash 堆积孤儿.
        try:
            await self.r.hdel("queue:enqueued", self._enqueued_field(pool, parsed))
        except Exception:
            pass
        return task_json, parsed, score

    async def get_queue_info(self, pool: str) -> dict:
        length = await self.r.zcard(f"queue:{pool}")
        return {"length": length}

    async def list_queue(self, pool: str, limit: int = 200) -> list[dict]:
        """只读窥视队列:ZRANGE 不弹出,按优先级序返回排队中的 task。
        每条:{job_id, step, priority, enqueued_at(秒,无则 None), tags, require_tags, resources}。失败回 []。"""
        try:
            items = await self.r.zrange(f"queue:{pool}", 0, limit - 1, withscores=True)
            if not items:
                return []
            ats = await self.r.hgetall("queue:enqueued") or {}
            out: list[dict] = []
            for member, score in items:
                try:
                    t = json.loads(member)
                except Exception:
                    continue
                at_raw = ats.get(self._enqueued_field(pool, t))
                out.append({
                    "kind": t.get("kind", "step"),          # 'step'(缺省)或 'ai'(独立 AI task)
                    "task_id": t.get("task_id"),            # ai task 有;step task 为 None
                    "job_id": t.get("job_id"), "step": t.get("step"), "priority": int(score),
                    "enqueued_at": float(at_raw) if at_raw else None,
                    "tags": t.get("tags", []), "require_tags": t.get("require_tags", []),
                    "resources": t.get("resources", []),
                })
            return out
        except Exception:
            return []

    # 资源池(Lua 原子操作)

    async def try_acquire_slot(self, pool: str, limit: int, holder: str) -> bool:
        """占池槽:把 holder(=exec_id,唯一)加入 pool:{pool}:holders 集合,前提是未 frozen 且 SCARD<limit。
        used=SCARD;同一 holder 重占幂等放行。配对的 release_slot(pool, holder) 用 SREM,幂等。"""
        result = await self.r.eval(
            _LUA_ACQUIRE_SLOT,
            2,
            f"pool:{pool}:holders",
            f"pool:{pool}:frozen",
            str(limit),
            holder,
        )
        return result == 1

    async def release_slot(self, pool: str, holder: str) -> bool:
        """放池槽:SREM holder,幂等。worker finally / reclaim / 删除多方释放同一 holder 都安全,无双减。"""
        result = await self.r.eval(
            _LUA_RELEASE_SLOT, 1, f"pool:{pool}:holders", holder
        )
        return result == 1

    async def freeze_pool(self, pool: str) -> None:
        await self.r.set(f"pool:{pool}:frozen", "1")

    async def unfreeze_pool(self, pool: str) -> None:
        await self.r.delete(f"pool:{pool}:frozen")

    async def is_pool_frozen(self, pool: str) -> bool:
        return await self.r.get(f"pool:{pool}:frozen") == "1"

    async def get_pool_count(self, pool: str) -> int:
        """已占槽数 = holders 集合基数(SCARD)。从结构上 = 当前真实持有者数,不会幽灵泄漏。"""
        return int(await self.r.scard(f"pool:{pool}:holders"))

    async def get_pool_holders(self, pool: str) -> set[str]:
        return set(await self.r.smembers(f"pool:{pool}:holders") or [])

    # 资源槽(单账号/单出口IP 等池粒度外的细粒度并发,复用池槽 Lua)
    # limit 由 scheduler 从 configs/resources.yaml 推到 redis hash(单一事实源),
    # claim_step 按任务声明的 resources 占槽;无声明=零开销,未配上限=不限(安全降级)。

    _RESOURCE_LIMITS_KEY = "resource_limits"

    async def set_resource_limits(self, limits: dict) -> None:
        """把资源上限刷进 redis(先清后写,删掉的资源不残留)。"""
        await self.r.delete(self._RESOURCE_LIMITS_KEY)
        if limits:
            await self.r.hset(
                self._RESOURCE_LIMITS_KEY,
                mapping={k: str(int(v)) for k, v in limits.items()},
            )

    async def get_resource_limit(self, resource: str) -> int | None:
        val = await self.r.hget(self._RESOURCE_LIMITS_KEY, resource)
        return int(val) if val is not None else None

    # 池上限运行时覆盖(前端可调,即时生效,无需改 pools.yaml/重启)
    # claim_step 取 limit 时优先读此覆盖,否则用 pools.yaml 默认(1024≈不限,即完全由
    # worker 自报并发);覆盖是 opt-in 的系统级天花板,如 ai 池设小以护 Claude 速率。
    _POOL_LIMIT_OVERRIDES_KEY = "pool_limit_overrides"

    async def get_pool_limit_override(self, pool: str) -> int | None:
        val = await self.r.hget(self._POOL_LIMIT_OVERRIDES_KEY, pool)
        return int(val) if val is not None else None

    async def set_pool_limit_override(self, pool: str, limit: int) -> None:
        await self.r.hset(self._POOL_LIMIT_OVERRIDES_KEY, pool, str(int(limit)))

    async def clear_pool_limit_override(self, pool: str) -> None:
        await self.r.hdel(self._POOL_LIMIT_OVERRIDES_KEY, pool)

    async def get_all_pool_limit_overrides(self) -> dict[str, int]:
        raw = await self.r.hgetall(self._POOL_LIMIT_OVERRIDES_KEY)
        out: dict[str, int] = {}
        for k, v in (raw or {}).items():
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    async def try_acquire_resource(self, resource: str, limit: int, holder: str) -> bool:
        # 复用池槽 holder-set Lua;资源无 frozen 概念,frozen 键永不置位故恒放行该检查。holder=exec_id。
        result = await self.r.eval(
            _LUA_ACQUIRE_SLOT, 2,
            f"res:{resource}:holders", f"res:{resource}:frozen", str(limit), holder,
        )
        return result == 1

    async def release_resource(self, resource: str, holder: str) -> bool:
        result = await self.r.eval(_LUA_RELEASE_SLOT, 1, f"res:{resource}:holders", holder)
        return result == 1

    async def get_resource_count(self, resource: str) -> int:
        return int(await self.r.scard(f"res:{resource}:holders"))

    async def _scan_holder_keys(self) -> list[str]:
        """扫出所有 pool:*:holders 与 res:*:holders 集合 key(对账/定向释放共用)。"""
        keys: list[str] = []
        for pat in ("pool:*:holders", "res:*:holders"):
            cursor = 0
            while True:
                cursor, batch = await self.r.scan(cursor, match=pat, count=200)
                keys.extend(batch)
                if cursor == 0:
                    break
        return keys

    async def get_all_holders(self) -> set[str]:
        """所有 pool:*:holders / res:*:holders 集合成员的并集(= 当前持有任意槽的 holder/exec_id 全集)。
        供 scheduler 周期对账:与"当前 running 步的 exec_id 集"作差,找出疑似泄漏的陈旧 holder。"""
        out: set[str] = set()
        try:
            for k in await self._scan_holder_keys():
                out |= set(await self.r.smembers(k) or [])
        except Exception:
            pass
        return out

    async def release_holders(self, holders: set[str]) -> int:
        """定向释放:把给定 holder(=exec_id)集合从所有 pool/res holder 集合里 SREM 掉,幂等。
        删 running job 时按其 running 步的 exec_id 调,立即归还其占的池槽/资源槽;worker 迟到的
        release_step 再 SREM 同 holder 也无害。返回清掉的成员数。"""
        if not holders:
            return 0
        removed = 0
        try:
            for k in await self._scan_holder_keys():
                for h in holders:
                    removed += await self.r.srem(k, h)
        except Exception:
            pass
        return removed

    # Job 实时状态

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

    @staticmethod
    def _task_lease_key(exec_id: str) -> str:
        return f"runner:lease:{exec_id}"

    async def create_task_lease(
        self, worker_id: str, job_id: str, step: str, exec_id: str,
        pool: str = "",
        ttl_sec: int | None = None,
    ) -> None:
        """创建四元组任务租约;当前 step 元数据仍是防 rerun/stale 的第二道约束."""
        key = self._task_lease_key(exec_id)
        fields = {
            "worker_id": worker_id,
            "job_id": job_id,
            "step": step,
            "exec_id": exec_id,
            "pool": pool,
        }
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(key, mapping=fields)
        pipe.expire(key, ttl_sec or self.TASK_LEASE_TTL_SEC)
        await pipe.execute()

    async def validate_task_lease(
        self, worker_id: str, job_id: str, step: str, exec_id: str, *,
        renew: bool = False, require_active: bool = True,
        expected_pool: str = "",
        ttl_sec: int | None = None,
    ) -> bool:
        result = await self.r.eval(
            _LUA_VALIDATE_TASK_LEASE,
            4,
            self._task_lease_key(exec_id),
            f"job:{job_id}:step_worker",
            f"job:{job_id}:step_exec",
            f"job:{job_id}:steps",
            worker_id,
            job_id,
            step,
            exec_id,
            str(ttl_sec or self.TASK_LEASE_TTL_SEC),
            "1" if renew else "0",
            "1" if require_active else "0",
            expected_pool,
        )
        return result == 1

    async def begin_task_terminal(
        self, worker_id: str, job_id: str, step: str, exec_id: str, outcome: str,
        expected_pool: str = "",
        ttl_sec: int | None = None,
    ) -> int:
        """原子占用终态上报;返回 1 首次,2 同结果重放,0 无效/冲突."""
        return int(await self.r.eval(
            _LUA_BEGIN_TASK_TERMINAL,
            4,
            self._task_lease_key(exec_id),
            f"job:{job_id}:step_worker",
            f"job:{job_id}:step_exec",
            f"job:{job_id}:steps",
            worker_id,
            job_id,
            step,
            exec_id,
            outcome,
            str(ttl_sec or self.TASK_LEASE_TTL_SEC),
            expected_pool,
        ))

    async def reset_task_terminal(
        self, worker_id: str, job_id: str, step: str, exec_id: str, outcome: str,
    ) -> bool:
        return bool(await self.r.eval(
            _LUA_RESET_TASK_TERMINAL,
            1,
            self._task_lease_key(exec_id),
            worker_id,
            job_id,
            step,
            exec_id,
            outcome,
        ))

    async def revoke_task_lease(self, exec_id: str) -> None:
        await self.r.delete(self._task_lease_key(exec_id))

    async def mark_task_lease_released(
        self, worker_id: str, job_id: str, step: str, exec_id: str, pool: str,
        ttl_sec: int = 300,
    ) -> None:
        """正常 release 留短期幂等墓碑;安全回收/过期撤销不调用本方法."""
        key = f"runner:released:{exec_id}"
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(key, mapping={
            "worker_id": worker_id,
            "job_id": job_id,
            "step": step,
            "exec_id": exec_id,
            "pool": pool,
        })
        pipe.expire(key, ttl_sec)
        pipe.delete(self._task_lease_key(exec_id))
        await pipe.execute()

    async def validate_released_task_lease(
        self, worker_id: str, job_id: str, step: str, exec_id: str, pool: str,
    ) -> bool:
        data = await self.r.hgetall(f"runner:released:{exec_id}")
        return data == {
            "worker_id": worker_id,
            "job_id": job_id,
            "step": step,
            "exec_id": exec_id,
            "pool": pool,
        }

    async def set_step_resources(
        self, job_id: str, step: str, resources: list[str]
    ) -> None:
        # 记本步占用的资源槽,供 release/orphan 回收据此释放。gateway 模式 release 请求不回传
        # 资源列表,故统一存 redis,由共享 release_step/_reclaim_step 读取。
        await self.r.hset(
            f"job:{job_id}:step_resources", step, json.dumps(resources),
        )

    async def get_step_resources(self, job_id: str, step: str) -> list[str]:
        raw = await self.r.hget(f"job:{job_id}:step_resources", step)
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    async def clear_step_resources(self, job_id: str, step: str) -> None:
        await self.r.hdel(f"job:{job_id}:step_resources", step)

    async def set_step_progress_at(self, job_id: str, step: str) -> None:
        # 步进度心跳:worker on_tick(每 10s,仅子进程存活时)刷新。供 check_stuck 对远程
        # (产物不落调度器盘)job 判进度停滞;本地 job 仍读 .{step}.progress 文件。
        await self.r.hset(f"job:{job_id}:step_progress", step, str(time.time()))

    async def get_step_progress_at(self, job_id: str, step: str) -> float | None:
        val = await self.r.hget(f"job:{job_id}:step_progress", step)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def incr_step_retries(self, job_id: str, step: str) -> int:
        return await self.r.hincrby(f"job:{job_id}:retries", step, 1)

    async def get_step_retries(self, job_id: str, step: str) -> int:
        val = await self.r.hget(f"job:{job_id}:retries", step)
        return int(val) if val else 0

    async def reset_step_retries(self, job_id: str, step: str) -> None:
        # 清单步重试计数,供 rerun 用:否则重跑曾耗尽重试预算的步骤会零重试预算。
        await self.r.hdel(f"job:{job_id}:retries", step)

    async def delete_step_status(self, job_id: str, step: str) -> None:
        # 清该步在所有 per-step hash 的 field(对齐 cleanup_job 清单),避免 resubmit 残留惰性垃圾。
        for sub in ("steps", "retries", "step_worker", "step_exec",
                    "step_resources", "step_progress"):
            await self.r.hdel(f"job:{job_id}:{sub}", step)

    async def cleanup_job(self, job_id: str) -> None:
        keys = [
            f"job:{job_id}",
            f"job:{job_id}:steps",
            f"job:{job_id}:retries",
            f"job:{job_id}:step_worker",
            f"job:{job_id}:step_exec",
            f"job:{job_id}:step_resources",
            f"job:{job_id}:step_progress",
        ]
        await self.r.delete(*keys)

    async def remove_job_tasks(self, job_id: str) -> int:
        """精准删该 job 在各 queue:{pool} 队列里尚未被认领的排队 task,并清 queue:enqueued 残留时间戳。
        返回删除的成员数。供删除 job 时清队列残留,否则成指向已删 job 的孤儿 task。
        成员是 enqueue_step 写入的 sort_keys JSON,只能逐成员 json.loads 比对 job_id 后 ZREM 整段成员,
        无法按 job_id 模式匹配删。失败安全:任何异常吞掉、返回已删数,不反噬删除主流程。"""
        removed = 0
        try:
            pool_keys: list[str] = []
            cursor = 0
            while True:
                cursor, keys = await self.r.scan(cursor, match="queue:*", count=200)
                for k in keys:
                    if k != "queue:enqueued":   # 同前缀的入队时间戳 hash,跳过
                        pool_keys.append(k)
                if cursor == 0:
                    break
            for qk in pool_keys:
                pool = qk.split("queue:", 1)[1]
                for member in await self.r.zrange(qk, 0, -1):
                    try:
                        t = json.loads(member)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if t.get("job_id") != job_id:
                        continue
                    await self.r.zrem(qk, member)
                    await self.r.hdel("queue:enqueued", f"{pool}|{job_id}|{t.get('step')}")
                    removed += 1
        except Exception:
            pass
        return removed

    # Worker

    # TTL 缺省取 online_window 兜底常量(单一事实源):worker liveness key 的过期窗口
    # 应与对外"在线"判定窗口一致。API 端会用 config 的 online_window_sec 覆盖此默认。
    async def register_worker(
        self, worker_id: str, info: dict, ttl: int = DEFAULT_ONLINE_WINDOW_SEC,
    ) -> None:
        await self.r.hset(f"worker:{worker_id}", mapping=info)
        await self.r.expire(f"worker:{worker_id}", ttl)

    async def heartbeat(self, worker_id: str, ttl: int = DEFAULT_ONLINE_WINDOW_SEC) -> None:
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
            # 防御:redis 里可能残留 string 型的 worker:registration_token(非 hash),
            # 对它 hgetall 会报 WRONGTYPE 把 /api/workers 打成 500。跳过非 worker 键。
            if worker_id == "registration_token":
                continue
            keys.append(worker_id)
        return keys

    async def incr_worker_stat(
        self, worker_id: str, field: str, amount: int | float
    ) -> None:
        """累计 worker 统计到 Redis hash。整数走 HINCRBY,浮点走 HINCRBYFLOAT,
        避免整数字段被写成 '1.0' 让消费侧 int() 解析失败。"""
        key = f"worker:{worker_id}"
        if isinstance(amount, int):
            await self.r.hincrby(key, field, amount)
        else:
            await self.r.hincrbyfloat(key, field, amount)

    async def delete_worker(self, worker_id: str) -> None:
        """删掉 Redis 里的 worker 记录(liveness)。活着的远程 worker 仅删 SQLite
        会在下次扫描又冒出来,必须连 Redis key 一起清。"""
        await self.r.delete(f"worker:{worker_id}")

    # 网关中转流量(产物代理:pull 为 NAS 到 worker 出库,push 为 worker 到 NAS 入库)
    # 按方向 + worker 归因累计字节。埋点在 api/routes/runner.py 的 get/put_artifact;
    # best-effort:计数失败绝不影响产物传输(故全程 try/except 吞异常)。
    # 总量另存哨兵 field "_"(`traffic:{direction}:total` hash),省得每次读全 by_worker 求和。

    async def incr_traffic(self, direction: str, worker_id: str, n: int) -> None:
        """累计中转流量字节:`traffic:{direction}` 按 worker_id,`traffic:{direction}:total` 总量。
        direction ∈ {pull,push}。失败静默(产物传输优先,统计可丢)。"""
        try:
            if not worker_id or n <= 0:
                return
            await self.r.hincrby(f"traffic:{direction}", worker_id, n)
            await self.r.hincrby(f"traffic:{direction}:total", "_", n)
        except Exception:
            pass

    async def get_traffic(self, direction: str) -> dict:
        """读某方向流量:{"total": int, "by_worker": {wid: int}}。读失败回零(不抛)。"""
        try:
            total_raw = await self.r.hget(f"traffic:{direction}:total", "_")
            total = int(total_raw) if total_raw else 0
            by_worker_raw = await self.r.hgetall(f"traffic:{direction}") or {}
            by_worker: dict[str, int] = {}
            for wid, v in by_worker_raw.items():
                try:
                    by_worker[wid] = int(v)
                except (TypeError, ValueError):
                    continue
            return {"total": total, "by_worker": by_worker}
        except Exception:
            return {"total": 0, "by_worker": {}}

    # MCP 工具调用计数(可观测;由 MCP server 进程 best-effort 写入,API 只读透出)
    # 写在 api/mcp_server/server.py 的工具包装里:总计 mcp:calls:total + 按工具 mcp:calls:tool:{name}。
    # 读失败回零(不抛):统计是 best-effort,绝不让 /api/mcp/info 因 redis 抖动 5xx。

    async def get_mcp_call_stats(self) -> dict:
        """读 MCP 工具调用计数:{"total": int, "by_tool": {name: int}}。读失败回零。"""
        try:
            total_raw = await self.r.get("mcp:calls:total")
            total = int(total_raw) if total_raw else 0
            by_tool: dict[str, int] = {}
            async for key in self.r.scan_iter(match="mcp:calls:tool:*"):
                name = key.split("mcp:calls:tool:", 1)[1]
                val = await self.r.get(key)
                try:
                    by_tool[name] = int(val) if val else 0
                except (TypeError, ValueError):
                    continue
            return {"total": total, "by_tool": by_tool}
        except Exception:
            return {"total": 0, "by_tool": {}}

    # 链路流量(ECS-NAS 隧道 + 网关聚合 + 速率快照),由 tunnel_stats 上报器周期写,/api/status 读
    async def set_link_traffic(self, payload: dict) -> None:
        """写链路流量快照(隧道 rx/tx + 每隧道 + up + 网关聚合 + 当前速率)。失败静默。"""
        try:
            await self.r.set("link:traffic", json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    async def get_link_traffic(self) -> dict | None:
        """读链路流量快照;无/失败回 None。"""
        try:
            raw = await self.r.get("link:traffic")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def push_traffic_sample(self, sample: dict, cap: int = 180) -> None:
        """追加流量时间线样本(LPUSH+LTRIM 保留最近 cap 个,最近在前),供前端算速率/趋势。失败静默。"""
        try:
            await self.r.lpush("traffic:timeline", json.dumps(sample, ensure_ascii=False))
            await self.r.ltrim("traffic:timeline", 0, cap - 1)
        except Exception:
            pass

    async def get_traffic_timeline(self, limit: int = 180) -> list[dict]:
        """读流量时间线(最近 limit 个,最近在前);失败回空。"""
        try:
            raw = await self.r.lrange("traffic:timeline", 0, limit - 1) or []
            out: list[dict] = []
            for s in raw:
                try:
                    out.append(json.loads(s))
                except Exception:
                    continue
            return out
        except Exception:
            return []

    # 组件心跳(scheduler 等无 DB 行的服务,与 worker:{id} 模式一致)
    # 键 component:{name},TTL=900(=stale_window):超窗 key 自动消失,API 读不到即判 down,
    # 而非永久 degraded。scheduler 每 10s 续约,容忍丢 2 拍仍 up。
    COMPONENT_TTL = 900

    async def set_component_heartbeat(self, name: str, fields: dict) -> None:
        key = f"component:{name}"
        payload = {**fields, "ts": datetime.now(timezone.utc).isoformat()}
        await self.r.hset(key, mapping={k: str(v) for k, v in payload.items()})
        await self.r.expire(key, self.COMPONENT_TTL)

    async def get_component_heartbeat(self, name: str) -> dict | None:
        data = await self.r.hgetall(f"component:{name}")
        return data or None

    async def server_info(self) -> dict:
        """Redis 探活 + INFO 采集(供 /api/status 的 redis 组件)。ping 计时 + version/内存/连接数。
        调用方包 asyncio.wait_for 超时;异常透传由调用方转 down。"""
        t0 = time.perf_counter()
        await self.r.ping()
        ping_ms = round((time.perf_counter() - t0) * 1000, 1)
        info = await self.r.info("server")
        mem = await self.r.info("memory")
        cli = await self.r.info("clients")
        used = int(mem.get("used_memory", 0) or 0)
        maxmem = int(mem.get("maxmemory", 0) or 0)
        return {
            "version": info.get("redis_version"),
            "ping_ms": ping_ms,
            "used_memory_human": mem.get("used_memory_human"),
            "used_memory_mb": round(used / 1048576, 1),
            "maxmemory_mb": round(maxmem / 1048576, 1),
            "uptime_sec": info.get("uptime_in_seconds"),
            "connected_clients": int(cli.get("connected_clients", 0) or 0),
        }

    # 接入 token(homelab 可复用 + 可重置)

    # 不放 worker: 命名空间:否则 list_worker_ids 的 worker:* 扫描会把它当成 worker,
    # 对这个 string 键做 hgetall 会触发 WRONGTYPE 并导致 /api/workers 500.
    _REGISTRATION_TOKEN_KEY = "runner:registration_token"

    async def get_registration_token(self) -> str | None:
        return await self.r.get(self._REGISTRATION_TOKEN_KEY)

    async def get_registration_token_ttl(self) -> int:
        """接入 token 剩余有效秒:>0=剩余秒数,-1=永不过期,-2=不存在。"""
        return await self.r.ttl(self._REGISTRATION_TOKEN_KEY)

    # 下载凭证镜像(cred:{dispatch_key}):DB 是持久源,此处只是分发缓存,无 TTL
    # (更新/清除由中心写入驱动,见 shared/credentials.mirror_credential)。

    async def set_dispatch_credential(self, key: str, value: str | None) -> None:
        if value:
            await self.r.set(f"cred:{key}", value)
        else:
            await self.r.delete(f"cred:{key}")

    async def get_dispatch_credential(self, key: str) -> str | None:
        return await self.r.get(f"cred:{key}")

    async def push_event(self, kind: str, **fields) -> None:
        """系统事件环形列表(events:system,LPUSH+LTRIM 保留最近 200,最近在上);供 /api/events 透出。
        None 字段剔除。best-effort:事件透出失败绝不影响调度主流程。"""
        evt = {"ts": time.time(), "kind": kind, **{k: v for k, v in fields.items() if v is not None}}
        try:
            await self.r.lpush("events:system", json.dumps(evt, ensure_ascii=False))
            await self.r.ltrim("events:system", 0, 199)
        except Exception:
            pass

    async def set_registration_token(self, token: str, ttl_sec: int | None = None) -> None:
        # ttl_sec 给接入 token 设过期,泄漏后自动失效;None 表示不过期(向后兼容)。
        if ttl_sec:
            await self.r.set(self._REGISTRATION_TOKEN_KEY, token, ex=ttl_sec)
        else:
            await self.r.set(self._REGISTRATION_TOKEN_KEY, token)

    # 活跃 Job 集合

    async def add_active_job(self, job_id: str) -> None:
        await self.r.sadd("active_jobs", job_id)

    async def remove_active_job(self, job_id: str) -> None:
        await self.r.srem("active_jobs", job_id)

    async def get_active_jobs(self) -> set[str]:
        return await self.r.smembers("active_jobs")

    # 事件 Pub/Sub

    async def publish(self, channel: str, data: dict) -> None:
        await self.r.publish(channel, json.dumps(data, ensure_ascii=False))

    async def subscribe(self, *channels: str) -> AsyncIterator[dict]:
        """订阅频道并 yield 解码后的消息。

        实现要点(曾踩坑):不用 ``pubsub.listen()`` 异步生成器.它在 redis
        关闭空闲 pubsub 连接后会静默挂死或停止迭代(订阅消失但不报错),导致
        调度器进程仍 Up 却收不到任何事件、任务永远 pending。改用带超时的
        ``get_message`` 轮询:连接断开会抛 Timeout/Connection 异常,被捕获后
        指数退避重连重订阅。绝不让异常逃逸导致上层崩溃;仅 CancelledError 透传。
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
                    # 连接级故障:标记需重订阅,退避后重连。
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
