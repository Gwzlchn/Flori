"""Redis 客户端封装:队列 / 资源池 / Job 状态 / Worker / 事件。"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from typing import AsyncIterator

import redis.asyncio as aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    ResponseError,
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

_LUA_CLAIM_PIPELINE_STEP = """
-- 一个脚本封闭 dequeue 到租约建立的崩溃窗口。Redis 为单机部署,可安全使用动态 job/resource key。
-- KEYS: queue,pool holders,pool frozen,enqueued,pool overrides,resource limits.
-- ARGV: pool,worker,exec,default limit,worker tags,reject tags,lease ttl,now.
local pool = ARGV[1]
local worker = ARGV[2]
local exec_id = ARGV[3]
local limit = tonumber(redis.call('HGET', KEYS[5], pool) or ARGV[4])
if redis.call('GET', KEYS[3]) == '1' then return nil end
if redis.call('SCARD', KEYS[2]) >= limit then return nil end

local worker_tags = cjson.decode(ARGV[5])
local reject_tags = cjson.decode(ARGV[6])
local function contains(items, wanted)
    for _, value in ipairs(items or {}) do
        if value == wanted then return true end
    end
    return false
end
local function matches(task)
    if task['kind'] == 'ai' then return false end
    for _, tag in ipairs(task['require_tags'] or {}) do
        if not contains(worker_tags, tag) then return false end
    end
    for _, tag in ipairs(task['tags'] or {}) do
        if contains(reject_tags, tag) then return false end
    end
    return true
end

local items = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
for index = 1, #items, 2 do
    local raw = items[index]
    local ok, task = pcall(cjson.decode, raw)
    if not ok or type(task) ~= 'table' then
        redis.call('ZREM', KEYS[1], raw)
        redis.call('XADD', 'flori:lifecycle:poison', '*',
            'source', KEYS[1], 'payload', raw, 'reason', 'invalid_json')
    elseif matches(task) then
        local job_id = task['job_id']
        local step = task['step']
        local status_key = 'job:' .. job_id .. ':steps'
        if redis.call('HGET', status_key, step) == 'ready' then
            local available = true
            for _, resource in ipairs(task['resources'] or {}) do
                local resource_limit = redis.call('HGET', KEYS[6], resource)
                if resource_limit and
                    redis.call('SCARD', 'res:' .. resource .. ':holders') >= tonumber(resource_limit) then
                    available = false
                    break
                end
            end
            if available then
                local job_key = 'job:' .. job_id
                local generation = redis.call('HGET', job_key, 'lifecycle_generation')
                if not generation then
                    generation = '1'
                    redis.call('HSET', job_key, 'lifecycle_generation', generation)
                end
                redis.call('ZREM', KEYS[1], raw)
                redis.call('HDEL', KEYS[4], pool .. '|' .. job_id .. '|' .. step)
                redis.call('SADD', KEYS[2], exec_id)
                for _, resource in ipairs(task['resources'] or {}) do
                    if redis.call('HGET', KEYS[6], resource) then
                        redis.call('SADD', 'res:' .. resource .. ':holders', exec_id)
                    end
                end
                redis.call('HSET', status_key, step, 'running')
                redis.call('HSET', 'job:' .. job_id .. ':step_worker', step, worker)
                redis.call('HSET', 'job:' .. job_id .. ':step_exec', step, exec_id)
                redis.call('HSET', 'job:' .. job_id .. ':step_generation', step, generation)
                redis.call('HSET', 'job:' .. job_id .. ':step_progress', step, ARGV[8])
                if #(task['resources'] or {}) > 0 then
                    redis.call('HSET', 'job:' .. job_id .. ':step_resources', step,
                        cjson.encode(task['resources']))
                end
                local lease_key = 'runner:lease:' .. exec_id
                redis.call('HSET', lease_key,
                    'worker_id', worker, 'job_id', job_id, 'step', step,
                    'exec_id', exec_id, 'pool', pool, 'generation', generation)
                redis.call('EXPIRE', lease_key, tonumber(ARGV[7]))
                return cjson.encode({job_id=job_id, step=step, pool=pool,
                    exec_id=exec_id, generation=tonumber(generation)})
            end
        end
    end
end
return nil
"""

_LUA_APPEND_TERMINAL = """
-- 校验当前 execution/generation/job 未终态后,以 exec_id 单次追加 terminal Stream。
local job_id = ARGV[1]
local step = ARGV[2]
local exec_id = ARGV[3]
local generation = ARGV[4]
local outcome = ARGV[5]
if redis.call('HGET', KEYS[1], step) ~= 'running'
    or redis.call('HGET', KEYS[2], step) ~= exec_id
    or redis.call('HGET', KEYS[3], step) ~= generation
    or redis.call('HGET', KEYS[4], 'lifecycle_generation') ~= generation
    or redis.call('HGET', KEYS[4], 'terminal_generation') == generation then
    return {0, ''}
end
local prior = redis.call('HGET', KEYS[5], exec_id)
if prior then
    local separator = string.find(prior, ':')
    if string.sub(prior, 1, separator - 1) == outcome then
        return {2, string.sub(prior, separator + 1)}
    end
    return {0, ''}
end
local message_id = redis.call('XADD', KEYS[6], '*',
    'topic', ARGV[6], 'payload', ARGV[7], 'emitted_at', ARGV[8], 'schema', '1')
redis.call('HSET', KEYS[5], exec_id, outcome .. ':' .. message_id)
return {1, message_id}
"""

_LUA_FINALIZE_JOB = """
-- generation 绑定 job 终态。1=首次赢家,2=同结果重放,0=旧代或冲突。
local current = redis.call('HGET', KEYS[1], 'lifecycle_generation')
if not current or current ~= ARGV[1] then return 0 end
local terminal_generation = redis.call('HGET', KEYS[1], 'terminal_generation')
local terminal_outcome = redis.call('HGET', KEYS[1], 'terminal_outcome')
if terminal_generation == ARGV[1] then
    if terminal_outcome == ARGV[2] then return 2 end
    return 0
end
redis.call('HSET', KEYS[1], 'terminal_generation', ARGV[1], 'terminal_outcome', ARGV[2])
return 1
"""

_LUA_ACQUIRE_JOB_FINALIZER = """
-- 1=持有 applying,2=已 applied,0=旧代/冲突/另一活 owner。
local current = redis.call('HGET', KEYS[1], 'lifecycle_generation')
if not current or current ~= ARGV[1] then return 0 end
local terminal_generation = redis.call('HGET', KEYS[1], 'terminal_generation')
local terminal_outcome = redis.call('HGET', KEYS[1], 'terminal_outcome')
if terminal_generation and (terminal_generation ~= ARGV[1] or terminal_outcome ~= ARGV[2]) then
    return 0
end
local state = redis.call('HGET', KEYS[2], 'state')
if state == 'applied' then return 2 end
local lease_until = tonumber(redis.call('HGET', KEYS[2], 'lease_until') or '0')
if state == 'applying' and lease_until > tonumber(ARGV[4]) then return 0 end
redis.call('HSET', KEYS[1], 'terminal_generation', ARGV[1], 'terminal_outcome', ARGV[2])
redis.call('HSET', KEYS[2],
    'generation', ARGV[1], 'outcome', ARGV[2], 'state', 'applying',
    'owner', ARGV[3], 'lease_until', ARGV[5])
return 1
"""

_LUA_COMPLETE_JOB_FINALIZER = """
if redis.call('HGET', KEYS[1], 'generation') == ARGV[1]
    and redis.call('HGET', KEYS[1], 'outcome') == ARGV[2]
    and redis.call('HGET', KEYS[1], 'state') == 'applying'
    and redis.call('HGET', KEYS[1], 'owner') == ARGV[3] then
    redis.call('HSET', KEYS[1], 'state', 'applied', 'lease_until', '0')
    return 1
end
return 0
"""

_LUA_ADVANCE_GENERATION_ONCE = """
local prior = redis.call('HGET', KEYS[2], ARGV[1])
if prior then
    local separator = string.find(prior, ':')
    local generation = tonumber(string.sub(prior, 1, separator - 1))
    local state = string.sub(prior, separator + 1)
    return {generation, state == 'done' and 0 or 1}
end
local generation = redis.call('HINCRBY', KEYS[1], 'lifecycle_generation', 1)
redis.call('HDEL', KEYS[1], 'terminal_generation', 'terminal_outcome')
redis.call('DEL', KEYS[3])
redis.call('HSET', KEYS[2], ARGV[1], generation .. ':applying')
return {generation, 1}
"""

_LUA_ENQUEUE_AI_ONCE = """
-- KEYS: submitted marker,queue ZSET,enqueued hash.
-- ARGV: task JSON,priority,enqueued field,epoch seconds,marker ttl.
local marker_type = redis.call('TYPE', KEYS[1])['ok']
local queue_type = redis.call('TYPE', KEYS[2])['ok']
local enqueued_type = redis.call('TYPE', KEYS[3])['ok']
if marker_type ~= 'none' and marker_type ~= 'string' then
    return redis.error_reply('WRONGTYPE submitted marker must be a string')
end
if queue_type ~= 'none' and queue_type ~= 'zset' then
    return redis.error_reply('WRONGTYPE AI queue must be a sorted set')
end
if enqueued_type ~= 'none' and enqueued_type ~= 'hash' then
    return redis.error_reply('WRONGTYPE enqueue timestamps must be a hash')
end
if marker_type == 'string' then
    if redis.call('GET', KEYS[1]) == ARGV[1] then return {0, 'replay'} end
    return {-1, 'payload_conflict'}
end
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[3], ARGV[4])
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[5]))
return {1, 'enqueued'}
"""

_LUA_CLAIM_AI_TASK = """
-- KEYS: queue ZSET,enqueued HASH,expiry ZSET,claim key prefix.
-- ARGV: worker,claim_id,lease_until,now,max_scan,accepted tags JSON,rejected tags JSON,lease seconds.
local accepted = cjson.decode(ARGV[6])
local rejected = cjson.decode(ARGV[7])
local accepted_set = {}
local rejected_set = {}
for _, tag in ipairs(accepted) do accepted_set[tag] = true end
for _, tag in ipairs(rejected) do rejected_set[tag] = true end
local entries = redis.call('ZRANGE', KEYS[1], 0, tonumber(ARGV[5]) - 1, 'WITHSCORES')
for index = 1, #entries, 2 do
    local raw = entries[index]
    local score = entries[index + 1]
    local ok, task = pcall(cjson.decode, raw)
    if ok and task['kind'] == 'ai' and type(task['task_id']) == 'string'
        and task['task_id'] ~= '' then
        local eligible = true
        local required = task['require_tags'] or {}
        local tags = task['tags'] or {}
        for _, tag in ipairs(required) do
            if not accepted_set[tag] then eligible = false end
        end
        for _, tag in ipairs(tags) do
            if rejected_set[tag] then eligible = false end
        end
        if eligible then
            local task_id = task['task_id']
            local claim_key = KEYS[4] .. task_id
            local previous_state = redis.call('HGET', claim_key, 'state')
            if not previous_state or previous_state == 'requeued' then
                if redis.call('ZREM', KEYS[1], raw) == 1 then
                    local requeue_count = redis.call('HGET', claim_key, 'requeue_count') or '0'
                    local batch_id = task['batch_id'] or ''
                    local attempt = task['attempt'] or 0
                    local revision = task['revision'] or 0
                    redis.call('HSET', claim_key,
                        'task_id', task_id,
                        'step', tostring(task['step'] or 'ai'),
                        'batch_id', tostring(batch_id),
                        'attempt', tostring(attempt),
                        'revision', tostring(revision),
                        'worker_id', ARGV[1],
                        'claim_id', ARGV[2],
                        'state', 'claimed',
                        'lease_until', ARGV[3],
                        'lease_seconds', ARGV[8],
                        'raw_json', raw,
                        'score', score,
                        'requeue_count', requeue_count)
                    redis.call('ZADD', KEYS[3], ARGV[3], task_id)
                    redis.call('HDEL', KEYS[2], 'ai|ai|' .. task_id)
                    return {raw, score, requeue_count}
                end
            end
        end
    end
end
return nil
"""

_LUA_AI_CLAIM_CAS = """
-- KEYS: claim HASH,expiry ZSET. ARGV: task,batch,attempt,revision,worker,claim,
-- expected state,new state,lease until,terminal flag.
if redis.call('HGET', KEYS[1], 'task_id') ~= ARGV[1]
    or redis.call('HGET', KEYS[1], 'batch_id') ~= ARGV[2]
    or redis.call('HGET', KEYS[1], 'attempt') ~= ARGV[3]
    or redis.call('HGET', KEYS[1], 'revision') ~= ARGV[4]
    or redis.call('HGET', KEYS[1], 'worker_id') ~= ARGV[5]
    or redis.call('HGET', KEYS[1], 'claim_id') ~= ARGV[6]
    or redis.call('HGET', KEYS[1], 'state') ~= ARGV[7] then
    return 0
end
redis.call('HSET', KEYS[1], 'state', ARGV[8], 'lease_until', ARGV[9])
if ARGV[10] == '1' then
    redis.call('ZREM', KEYS[2], ARGV[1])
else
    redis.call('ZADD', KEYS[2], ARGV[9], ARGV[1])
end
return 1
"""

_LUA_RECONCILE_AI_CLAIMS = """
-- KEYS: expiry ZSET,queue ZSET,enqueued HASH,claim key prefix.
-- ARGV: now epoch.
local task_ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local actions = {}
for _, task_id in ipairs(task_ids) do
    local claim_key = KEYS[4] .. task_id
    local state = redis.call('HGET', claim_key, 'state')
    local lease_until = tonumber(redis.call('HGET', claim_key, 'lease_until') or '0')
    if lease_until <= tonumber(ARGV[1]) then
        if state == 'claimed' then
            local count = tonumber(redis.call('HGET', claim_key, 'requeue_count') or '0')
            if count < 1 then
                local raw = redis.call('HGET', claim_key, 'raw_json')
                local score = redis.call('HGET', claim_key, 'score')
                if raw and score then
                    redis.call('ZADD', KEYS[2], 'NX', score, raw)
                    redis.call('HSET', KEYS[3], 'ai|ai|' .. task_id, ARGV[1])
                    redis.call('HSET', claim_key, 'state', 'requeued',
                        'requeue_count', '1', 'lease_until', '0')
                    redis.call('ZREM', KEYS[1], task_id)
                    table.insert(actions, task_id)
                    table.insert(actions, 'requeued')
                end
            else
                redis.call('HSET', claim_key, 'state', 'ambiguous', 'lease_until', '0')
                redis.call('ZREM', KEYS[1], task_id)
                table.insert(actions, task_id)
                table.insert(actions, 'ambiguous')
            end
        elseif state == 'executing' then
            redis.call('HSET', claim_key, 'state', 'ambiguous', 'lease_until', '0')
            redis.call('ZREM', KEYS[1], task_id)
            table.insert(actions, task_id)
            table.insert(actions, 'ambiguous')
        else
            redis.call('ZREM', KEYS[1], task_id)
        end
    end
end
return actions
"""

_LUA_CANCEL_AI_BEFORE_EXECUTION = """
-- KEYS: queue ZSET,enqueued HASH,claim HASH,expiry ZSET,pool holders SET.
-- ARGV: task,batch,attempt,revision,raw JSON,enqueued field.
local queue_type = redis.call('TYPE', KEYS[1])['ok']
local enqueued_type = redis.call('TYPE', KEYS[2])['ok']
local claim_type = redis.call('TYPE', KEYS[3])['ok']
local expiry_type = redis.call('TYPE', KEYS[4])['ok']
local holders_type = redis.call('TYPE', KEYS[5])['ok']
if queue_type ~= 'none' and queue_type ~= 'zset' then
    return redis.error_reply('WRONGTYPE AI queue must be a sorted set')
end
if enqueued_type ~= 'none' and enqueued_type ~= 'hash' then
    return redis.error_reply('WRONGTYPE enqueue timestamps must be a hash')
end
if claim_type ~= 'none' and claim_type ~= 'hash' then
    return redis.error_reply('WRONGTYPE AI claim must be a hash')
end
if expiry_type ~= 'none' and expiry_type ~= 'zset' then
    return redis.error_reply('WRONGTYPE AI claim expiry must be a sorted set')
end
if holders_type ~= 'none' and holders_type ~= 'set' then
    return redis.error_reply('WRONGTYPE AI pool holders must be a set')
end
local state = redis.call('HGET', KEYS[3], 'state')
if state then
    if redis.call('HGET', KEYS[3], 'task_id') ~= ARGV[1]
        or redis.call('HGET', KEYS[3], 'batch_id') ~= ARGV[2]
        or redis.call('HGET', KEYS[3], 'attempt') ~= ARGV[3]
        or redis.call('HGET', KEYS[3], 'revision') ~= ARGV[4] then
        return 'stale'
    end
    if state == 'canceled' then return 'canceled' end
    if state == 'executing' then return 'executing' end
    if state == 'claimed' then
        local claim_id = redis.call('HGET', KEYS[3], 'claim_id') or ''
        redis.call('HSET', KEYS[3], 'state', 'canceled', 'lease_until', '0')
        redis.call('ZREM', KEYS[4], ARGV[1])
        if claim_id ~= '' then redis.call('SREM', KEYS[5], claim_id) end
        redis.call('HDEL', KEYS[2], ARGV[6])
        return 'canceled'
    end
    if state == 'requeued' then
        if redis.call('ZREM', KEYS[1], ARGV[5]) ~= 1 then return 'race' end
        local claim_id = redis.call('HGET', KEYS[3], 'claim_id') or ''
        redis.call('HSET', KEYS[3], 'state', 'canceled', 'lease_until', '0')
        redis.call('ZREM', KEYS[4], ARGV[1])
        if claim_id ~= '' then redis.call('SREM', KEYS[5], claim_id) end
        redis.call('HDEL', KEYS[2], ARGV[6])
        return 'canceled'
    end
    return state
end
if redis.call('ZREM', KEYS[1], ARGV[5]) ~= 1 then return 'missing' end
redis.call('HSET', KEYS[3],
    'task_id', ARGV[1],
    'batch_id', ARGV[2],
    'attempt', ARGV[3],
    'revision', ARGV[4],
    'worker_id', '',
    'claim_id', '',
    'state', 'canceled',
    'lease_until', '0',
    'lease_seconds', '0',
    'raw_json', ARGV[5],
    'requeue_count', '0')
redis.call('HDEL', KEYS[2], ARGV[6])
return 'canceled'
"""


class AIEnqueueConflictError(RuntimeError):
    """同一 AI task_id 已绑定不同 canonical payload."""

    code = "ai_task_payload_conflict"


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
        task = await self._bind_ai_task_anchor(payload)
        await self.r.zadd("queue:ai", {task: priority})
        try:
            await self.r.hset("queue:enqueued", self._enqueued_field("ai", payload), str(time.time()))
        except Exception:
            pass

    async def enqueue_ai_task_once(
        self,
        payload: dict,
        priority: int = 0,
        marker_ttl_sec: int = 7 * 86_400,
    ) -> bool:
        """按 task_id 原子标记并入队,防止 Scheduler 重启后重复付费投递."""
        task_id = payload.get("task_id")
        if (
            payload.get("kind") != "ai"
            or not isinstance(task_id, str)
            or not task_id.strip()
        ):
            raise ValueError("AI task payload 必须包含非空 task_id 且 kind='ai'")
        if type(priority) is not int:
            raise ValueError("priority 必须是整数")
        if type(marker_ttl_sec) is not int or not 60 <= marker_ttl_sec <= 30 * 86_400:
            raise ValueError("marker_ttl_sec 必须是 60..2592000 的整数")
        if payload.get("step") == "study_suggestions":
            from shared.study_suggestions import validate_study_suggestion_task_payload

            validate_study_suggestion_task_payload(payload)
        task = await self._bind_ai_task_anchor(payload, ttl=marker_ttl_sec)
        result = await self.r.eval(
            _LUA_ENQUEUE_AI_ONCE,
            3,
            f"ai:submitted:{task_id}",
            "queue:ai",
            "queue:enqueued",
            task,
            str(priority),
            self._enqueued_field("ai", payload),
            str(time.time()),
            str(marker_ttl_sec),
        )
        status = int(result[0])
        if status == -1:
            raise AIEnqueueConflictError(
                f"AI task_id 已绑定不同 payload: {task_id}"
            )
        return status == 1

    async def _bind_ai_task_anchor(
        self, payload: dict, *, ttl: int = 7 * 86_400,
    ) -> str:
        """服务端按 task_id 冻结原始 payload;Worker 结果不能覆盖该锚点。"""
        task_id = payload.get("task_id") if type(payload) is dict else None
        if (
            type(payload) is not dict
            or payload.get("kind") != "ai"
            or type(task_id) is not str
            or not task_id.strip()
        ):
            raise ValueError("AI task payload 必须包含非空 task_id 且 kind='ai'")
        task = json.dumps(payload, sort_keys=True)
        key = f"ai:anchor:{task_id}"
        if await self.r.set(key, task, nx=True, ex=ttl):
            return task
        if await self.r.get(key) != task:
            raise AIEnqueueConflictError(
                f"AI task_id 已绑定不同服务端锚点: {task_id}"
            )
        return task

    @staticmethod
    def _validate_ai_lease_inputs(
        *, worker_id: str, lease_seconds: int, now_epoch: int | float,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id 必须是非空字符串")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds 必须是 1..86400 的整数")
        if isinstance(now_epoch, bool) or not isinstance(now_epoch, (int, float)):
            raise ValueError("now_epoch 必须是 epoch 秒")

    async def claim_ai_task(
        self,
        *,
        worker_id: str,
        lease_seconds: int = TASK_LEASE_TTL_SEC,
        now_epoch: int | float | None = None,
        claim_id: str | None = None,
        tags: set[str] | list[str] | None = None,
        reject_tags: set[str] | list[str] | None = None,
        max_scan: int = 20,
    ) -> dict | None:
        """原子把匹配的 queue:ai 成员转成 claimed 租约,消除 ZPOPMIN 崩溃窗口."""
        now = time.time() if now_epoch is None else now_epoch
        self._validate_ai_lease_inputs(
            worker_id=worker_id, lease_seconds=lease_seconds, now_epoch=now,
        )
        if type(max_scan) is not int or not 1 <= max_scan <= 200:
            raise ValueError("max_scan 必须是 1..200 的整数")
        token = claim_id or secrets.token_urlsafe(24)
        if not isinstance(token, str) or not token:
            raise ValueError("claim_id 必须是非空字符串")
        lease_until = float(now) + lease_seconds
        result = await self.r.eval(
            _LUA_CLAIM_AI_TASK,
            4,
            "queue:ai",
            "queue:enqueued",
            "ai:claims:expiry",
            "ai:claim:",
            worker_id,
            token,
            str(lease_until),
            str(float(now)),
            str(max_scan),
            json.dumps(sorted(set(tags or []))),
            json.dumps(sorted(set(reject_tags or []))),
            str(lease_seconds),
        )
        if not result:
            return None
        raw_json, score, requeue_count = result
        payload = json.loads(raw_json)
        return {
            **payload,
            "state": "claimed",
            "claim_id": token,
            "worker_id": worker_id,
            "lease_until": lease_until,
            "lease_seconds": lease_seconds,
            "score": float(score),
            "requeue_count": int(requeue_count),
        }

    async def _ai_claim_cas(
        self,
        *,
        task_id: str,
        claim_id: str,
        worker_id: str,
        attempt: int,
        revision: int,
        batch_id: str | None,
        expected_state: str,
        new_state: str,
        lease_until: float,
        terminal: bool,
    ) -> bool:
        key = f"ai:claim:{task_id}"
        bound_batch = batch_id
        if bound_batch is None:
            bound_batch = await self.r.hget(key, "batch_id")
        if bound_batch is None:
            return False
        result = await self.r.eval(
            _LUA_AI_CLAIM_CAS,
            2,
            key,
            "ai:claims:expiry",
            task_id,
            str(bound_batch),
            str(attempt),
            str(revision),
            worker_id,
            claim_id,
            expected_state,
            new_state,
            str(lease_until),
            "1" if terminal else "0",
        )
        return int(result) == 1

    async def mark_ai_task_executing(
        self,
        *,
        task_id: str,
        claim_id: str,
        worker_id: str,
        attempt: int,
        revision: int,
        now_epoch: int | float | None = None,
        lease_seconds: int | None = None,
        batch_id: str | None = None,
    ) -> bool:
        """在首次 provider 调用前把同一 claim 从 claimed CAS 为 executing."""
        now = time.time() if now_epoch is None else now_epoch
        if lease_seconds is None:
            raw_lease = await self.r.hget(f"ai:claim:{task_id}", "lease_seconds")
            lease_seconds = int(raw_lease) if raw_lease is not None else self.TASK_LEASE_TTL_SEC
        self._validate_ai_lease_inputs(
            worker_id=worker_id, lease_seconds=lease_seconds, now_epoch=now,
        )
        return await self._ai_claim_cas(
            task_id=task_id, claim_id=claim_id, worker_id=worker_id,
            attempt=attempt, revision=revision, batch_id=batch_id,
            expected_state="claimed", new_state="executing",
            lease_until=float(now) + lease_seconds, terminal=False,
        )

    async def renew_ai_task_claim(
        self,
        *,
        task_id: str,
        claim_id: str,
        worker_id: str,
        attempt: int,
        revision: int,
        now_epoch: int | float | None = None,
        lease_seconds: int | None = None,
        batch_id: str | None = None,
        state: str = "executing",
    ) -> bool:
        """续约仍由完整任务身份 CAS,陈旧 worker 不能延长新执行."""
        if state not in {"claimed", "executing"}:
            raise ValueError("state 必须是 claimed/executing")
        now = time.time() if now_epoch is None else now_epoch
        if lease_seconds is None:
            raw_lease = await self.r.hget(f"ai:claim:{task_id}", "lease_seconds")
            lease_seconds = int(raw_lease) if raw_lease is not None else self.TASK_LEASE_TTL_SEC
        self._validate_ai_lease_inputs(
            worker_id=worker_id, lease_seconds=lease_seconds, now_epoch=now,
        )
        return await self._ai_claim_cas(
            task_id=task_id, claim_id=claim_id, worker_id=worker_id,
            attempt=attempt, revision=revision, batch_id=batch_id,
            expected_state=state, new_state=state,
            lease_until=float(now) + lease_seconds, terminal=False,
        )

    async def finish_ai_task_claim(
        self,
        *,
        task_id: str,
        claim_id: str,
        worker_id: str,
        attempt: int,
        revision: int,
        outcome: str,
        batch_id: str | None = None,
    ) -> bool:
        """结果和持久审计落地后才把 executing claim 收口为终态."""
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("outcome 必须是 succeeded/failed")
        return await self._ai_claim_cas(
            task_id=task_id, claim_id=claim_id, worker_id=worker_id,
            attempt=attempt, revision=revision, batch_id=batch_id,
            expected_state="executing", new_state=outcome,
            lease_until=0, terminal=True,
        )

    async def abandon_ai_task_claim(
        self,
        *,
        task_id: str,
        claim_id: str,
        worker_id: str,
        attempt: int,
        revision: int,
        batch_id: str | None = None,
        now_epoch: int | float | None = None,
    ) -> bool:
        """provider 尚未开始时把 claimed 租约立即到期,交统一收割器安全回队."""
        now = time.time() if now_epoch is None else now_epoch
        changed = await self._ai_claim_cas(
            task_id=task_id, claim_id=claim_id, worker_id=worker_id,
            attempt=attempt, revision=revision, batch_id=batch_id,
            expected_state="claimed", new_state="claimed",
            lease_until=float(now), terminal=False,
        )
        if changed:
            await self.reconcile_ai_task_claims(now_epoch=now)
        return changed

    async def reconcile_ai_task_claims(
        self, *, now_epoch: int | float | None = None,
    ) -> list[dict[str, str]]:
        """claimed 最多安全回队一次;executing 到期只进入 ambiguous."""
        now = time.time() if now_epoch is None else now_epoch
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("now_epoch 必须是 epoch 秒")
        raw = await self.r.eval(
            _LUA_RECONCILE_AI_CLAIMS,
            4,
            "ai:claims:expiry",
            "queue:ai",
            "queue:enqueued",
            "ai:claim:",
            str(float(now)),
        )
        return [
            {"task_id": raw[index], "action": raw[index + 1]}
            for index in range(0, len(raw), 2)
        ]

    async def cancel_ai_task_before_execution(self, payload: dict) -> str:
        """只在任务尚未进入 provider 时原子撤队并终止精确批次身份."""
        if payload.get("kind") != "ai":
            raise ValueError("AI task payload kind 非法")
        task_id = payload.get("task_id")
        batch_id = payload.get("batch_id")
        attempt = payload.get("attempt")
        revision = payload.get("revision")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("AI task payload task_id 非法")
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("AI task payload batch_id 非法")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("AI task payload attempt 非法")
        if type(revision) is not int or revision < 1:
            raise ValueError("AI task payload revision 非法")
        raw = json.dumps(payload, sort_keys=True)
        result = await self.r.eval(
            _LUA_CANCEL_AI_BEFORE_EXECUTION,
            5,
            "queue:ai",
            "queue:enqueued",
            f"ai:claim:{task_id}",
            "ai:claims:expiry",
            "pool:ai:holders",
            task_id,
            batch_id,
            str(attempt),
            str(revision),
            raw,
            self._enqueued_field("ai", payload),
        )
        return str(result)

    async def get_ai_task_claim(self, task_id: str) -> dict | None:
        data = await self.r.hgetall(f"ai:claim:{task_id}")
        if not data:
            return None
        for field in ("attempt", "revision", "requeue_count", "lease_seconds"):
            try:
                data[field] = int(data[field])
            except (KeyError, TypeError, ValueError):
                pass
        try:
            data["lease_until"] = float(data["lease_until"])
        except (KeyError, TypeError, ValueError):
            pass
        return data

    async def get_ai_task_original_payload(self, task_id: str) -> dict | None:
        """读取服务端提交锚点;兼容升级前 claim raw_json,损坏时 fail closed。"""
        raw = await self.r.get(f"ai:anchor:{task_id}")
        if raw is None:
            raw = await self.r.hget(f"ai:claim:{task_id}", "raw_json")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if type(payload) is not dict or payload.get("task_id") != task_id:
            return None
        return payload

    async def get_live_ai_claim_holders(
        self, *, now_epoch: int | float | None = None,
    ) -> set[str]:
        now = time.time() if now_epoch is None else float(now_epoch)
        task_ids = await self.r.zrangebyscore("ai:claims:expiry", now, "+inf")
        holders: set[str] = set()
        for task_id in task_ids:
            data = await self.r.hmget(f"ai:claim:{task_id}", "state", "claim_id")
            if data and data[0] in {"claimed", "executing"} and data[1]:
                holders.add(data[1])
        return holders

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

    async def claim_pipeline_step_atomic(
        self,
        *,
        pool: str,
        worker_id: str,
        exec_id: str,
        default_limit: int,
        tags: set[str],
        reject_tags: set[str],
    ) -> dict | None:
        """原子匹配并认领普通 pipeline step;不兼容队头不会限制后续候选。"""
        raw = await self.r.eval(
            _LUA_CLAIM_PIPELINE_STEP,
            6,
            f"queue:{pool}",
            f"pool:{pool}:holders",
            f"pool:{pool}:frozen",
            "queue:enqueued",
            self._POOL_LIMIT_OVERRIDES_KEY,
            self._RESOURCE_LIMITS_KEY,
            pool,
            worker_id,
            exec_id,
            str(default_limit),
            json.dumps(sorted(tags)),
            json.dumps(sorted(reject_tags)),
            str(self.TASK_LEASE_TTL_SEC),
            str(time.time()),
        )
        return json.loads(raw) if raw else None

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
        key = f"job:{job_id}"
        await self.r.hset(key, mapping={
            "pipeline": pipeline,
            **{k: json.dumps(v) if isinstance(v, (list, dict)) else str(v) for k, v in info.items()},
        })
        await self.r.hsetnx(key, "lifecycle_generation", "1")

    async def get_job_generation(self, job_id: str) -> int:
        raw = await self.r.hget(f"job:{job_id}", "lifecycle_generation")
        return int(raw or 1)

    async def job_generation_is_terminal(self, job_id: str, generation: int) -> bool:
        raw = await self.r.hget(f"job:{job_id}", "terminal_generation")
        return raw == str(generation)

    async def advance_job_generation(
        self, job_id: str, idempotency_key: str | None = None,
    ) -> int:
        """开始新一轮执行并清掉上一代终态;旧事件随后会被 generation/exec 双重拒绝。"""
        key = f"job:{job_id}"
        if idempotency_key:
            result = await self.r.eval(
                _LUA_ADVANCE_GENERATION_ONCE,
                3,
                key,
                f"job:{job_id}:generation_tokens",
                f"job:{job_id}:finalizer",
                idempotency_key,
            )
            return int(result[0])
        pipe = self.r.pipeline(transaction=True)
        pipe.hincrby(key, "lifecycle_generation", 1)
        pipe.hdel(key, "terminal_generation", "terminal_outcome")
        pipe.delete(f"job:{job_id}:finalizer")
        result = await pipe.execute()
        return int(result[0])

    async def begin_job_generation(
        self, job_id: str, idempotency_key: str | None,
    ) -> tuple[int, bool]:
        if not idempotency_key:
            return await self.advance_job_generation(job_id), True
        result = await self.r.eval(
            _LUA_ADVANCE_GENERATION_ONCE,
            3,
            f"job:{job_id}",
            f"job:{job_id}:generation_tokens",
            f"job:{job_id}:finalizer",
            idempotency_key,
        )
        return int(result[0]), bool(result[1])

    async def complete_job_generation(
        self, job_id: str, idempotency_key: str | None, generation: int,
    ) -> None:
        if idempotency_key:
            await self.r.hset(
                f"job:{job_id}:generation_tokens",
                idempotency_key,
                f"{generation}:done",
            )

    async def try_finalize_job(
        self, job_id: str, generation: int, outcome: str,
    ) -> int:
        if outcome not in {"done", "failed"}:
            raise ValueError("job terminal outcome 非法")
        return int(await self.r.eval(
            _LUA_FINALIZE_JOB,
            1,
            f"job:{job_id}",
            str(generation),
            outcome,
        ))

    async def acquire_job_finalizer(
        self,
        job_id: str,
        generation: int,
        outcome: str,
        owner: str,
        *,
        now: float | None = None,
        lease_sec: float = 15.0,
    ) -> int:
        current_time = time.time() if now is None else now
        return int(await self.r.eval(
            _LUA_ACQUIRE_JOB_FINALIZER,
            2,
            f"job:{job_id}",
            f"job:{job_id}:finalizer",
            str(generation),
            outcome,
            owner,
            str(current_time),
            str(current_time + lease_sec),
        ))

    async def complete_job_finalizer(
        self, job_id: str, generation: int, outcome: str, owner: str,
    ) -> bool:
        return bool(await self.r.eval(
            _LUA_COMPLETE_JOB_FINALIZER,
            1,
            f"job:{job_id}:finalizer",
            str(generation),
            outcome,
            owner,
        ))

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

    async def get_step_generation(self, job_id: str, step: str) -> int | None:
        raw = await self.r.hget(f"job:{job_id}:step_generation", step)
        return int(raw) if raw is not None else None

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
        for sub in ("steps", "retries", "step_worker", "step_exec", "step_generation",
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
            f"job:{job_id}:step_generation",
            f"job:{job_id}:generation_tokens",
            f"job:{job_id}:terminal_events",
            f"job:{job_id}:finalizer",
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

    LIFECYCLE_STREAM = "flori:lifecycle"
    LIFECYCLE_GROUP = "flori:scheduler"
    LIFECYCLE_POISON_STREAM = "flori:lifecycle:poison"

    async def append_lifecycle_event(self, topic: str, data: dict) -> str:
        """权威生命周期事件先落 Stream,再发 Pub/Sub 唤醒/兼容通知。"""
        message_id = await self.r.xadd(
            self.LIFECYCLE_STREAM,
            {
                "topic": topic,
                "payload": json.dumps(data, ensure_ascii=False, sort_keys=True),
                "emitted_at": str(time.time()),
                "schema": "1",
            },
        )
        # Stream 是权威通道。通知发送失败不能让已持久的命令对 API
        # 表现为失败，否则客户端重试会生成第二条不同 ID 的命令。
        try:
            await self.publish(topic, data)
        except Exception:
            pass
        return str(message_id)

    async def append_terminal_if_current(
        self, topic: str, data: dict,
    ) -> tuple[int, str | None]:
        """仅当前 execution 可把 step terminal 追加到权威 Stream。"""
        job_id = data.get("job_id")
        step = data.get("step")
        exec_id = data.get("exec_id")
        generation = data.get("generation")
        if (
            not isinstance(job_id, str)
            or not isinstance(step, str)
            or not isinstance(exec_id, str)
            or not exec_id
            or type(generation) is not int
        ):
            return 0, None
        outcome = "done" if topic == "step_completed" else "failed"
        result = await self.r.eval(
            _LUA_APPEND_TERMINAL,
            6,
            f"job:{job_id}:steps",
            f"job:{job_id}:step_exec",
            f"job:{job_id}:step_generation",
            f"job:{job_id}",
            f"job:{job_id}:terminal_events",
            self.LIFECYCLE_STREAM,
            job_id,
            step,
            exec_id,
            str(generation),
            outcome,
            topic,
            json.dumps(data, ensure_ascii=False, sort_keys=True),
            str(time.time()),
        )
        return int(result[0]), str(result[1]) if result[1] else None

    async def ensure_lifecycle_group(self) -> None:
        try:
            await self.r.xgroup_create(
                self.LIFECYCLE_STREAM,
                self.LIFECYCLE_GROUP,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_lifecycle_events(
        self,
        consumer: str,
        *,
        block_ms: int = 1_000,
        reclaim_idle_ms: int = 5_000,
        count: int = 32,
    ) -> list[tuple[str, dict]]:
        """先收割未 ACK pending,再阻塞读取新消息。"""
        await self.ensure_lifecycle_group()
        claimed = await self.r.xautoclaim(
            self.LIFECYCLE_STREAM,
            self.LIFECYCLE_GROUP,
            consumer,
            min_idle_time=reclaim_idle_ms,
            start_id="0-0",
            count=count,
        )
        entries = list(claimed[1] or []) if claimed else []
        if not entries:
            batches = await self.r.xreadgroup(
                self.LIFECYCLE_GROUP,
                consumer,
                {self.LIFECYCLE_STREAM: ">"},
                count=count,
                block=block_ms,
            )
            entries = list(batches[0][1]) if batches else []
        return [(str(message_id), fields) for message_id, fields in entries]

    async def ack_lifecycle_event(self, message_id: str) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.xack(self.LIFECYCLE_STREAM, self.LIFECYCLE_GROUP, message_id)
        pipe.xdel(self.LIFECYCLE_STREAM, message_id)
        pipe.hdel("flori:lifecycle:failures", message_id)
        await pipe.execute()

    async def reject_lifecycle_event(
        self, message_id: str, fields: dict, error: str, *, max_attempts: int = 3,
    ) -> bool:
        """返回 True 表示 poison 已隔离并 ACK;否则保留 PEL 等待 XAUTOCLAIM。"""
        failures = int(await self.r.hincrby(
            "flori:lifecycle:failures", message_id, 1,
        ))
        if failures < max_attempts:
            return False
        await self.r.xadd(
            self.LIFECYCLE_POISON_STREAM,
            {
                "source_id": message_id,
                "topic": str(fields.get("topic", "")),
                "payload": str(fields.get("payload", ""))[:16_384],
                "error": error[:2_000],
                "attempts": str(failures),
            },
            maxlen=1_000,
            approximate=True,
        )
        await self.ack_lifecycle_event(message_id)
        return True

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
