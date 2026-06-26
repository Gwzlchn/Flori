"""Redis 客户端封装：队列 / 资源池 / Job 状态 / Worker / 事件。"""

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
        resources: list[str] | None = None,
    ) -> None:
        payload = {
            "job_id": job_id, "step": step, "tags": sorted(tags),
            "require_tags": sorted(require_tags) if require_tags else [],
        }
        # 仅在声明了资源槽时才写 resources 键:无声明时 task JSON 与旧形态逐字一致(向后兼容)。
        if resources:
            payload["resources"] = sorted(resources)
        task = json.dumps(payload, sort_keys=True)
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

    # ── 资源槽(单账号/单出口IP 等池粒度外的细粒度并发,复用池槽 Lua)──
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

    # ── 池上限运行时覆盖(前端可调,即时生效,无需改 pools.yaml/重启)──
    # claim_step 取 limit 时优先读此覆盖,否则用 pools.yaml 默认(默认 1024≈不限,即"完全由
    # worker 自报并发"); 覆盖是 opt-in 的系统级天花板(如 ai 池设小以护 Claude 速率)。
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

    async def try_acquire_resource(self, resource: str, limit: int) -> bool:
        # 复用池槽 Lua;资源无 frozen 概念,frozen 键永不置位故恒放行该检查。
        result = await self.r.eval(
            _LUA_ACQUIRE_SLOT, 2,
            f"res:{resource}:count", f"res:{resource}:frozen", str(limit),
        )
        return result == 1

    async def release_resource(self, resource: str) -> bool:
        result = await self.r.eval(_LUA_RELEASE_SLOT, 1, f"res:{resource}:count")
        return result == 1

    async def get_resource_count(self, resource: str) -> int:
        val = await self.r.get(f"res:{resource}:count")
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

    async def set_step_resources(
        self, job_id: str, step: str, resources: list[str]
    ) -> None:
        # 记本步占用的资源槽,供 release/orphan 回收据此释放(gateway 模式 release 请求不回传
        # 资源列表,故统一存 redis 由共享 release_step/_reclaim_step 读取)。
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
        # 清单步重试计数(rerun 用):否则重跑曾耗尽重试的步骤会零重试预算(审计 I-H4)。
        await self.r.hdel(f"job:{job_id}:retries", step)

    async def delete_step_status(self, job_id: str, step: str) -> None:
        # 清该步在所有 per-step hash 的 field(对齐 cleanup_job 清单),避免 resubmit 残留惰性垃圾(审计 I-L11)。
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

    # ── Worker ──

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

    # ── 网关中转流量(产物代理:pull=NAS→worker 出库 / push=worker→NAS 入库)──
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

    # ── MCP 工具调用计数(可观测;由 MCP server 进程 best-effort 写入,API 只读透出)──
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

    # ── 组件心跳(scheduler 等无 DB 行的服务,与 worker:{id} 模式一致)──
    # 键 component:{name},TTL=900(=stale_window):超窗 key 自动消失 → API 读不到 → down
    # (而非永久 degraded)。scheduler 每 10s 续约,容忍丢 2 拍仍 up。
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

    # ── 接入 token（homelab 可复用 + 可重置）──

    # 不放 worker: 命名空间:否则 list_worker_ids 的 worker:* 扫描会把它当成 worker,
    # 对这个 string 键做 hgetall 触发 WRONGTYPE → /api/workers 500。
    _REGISTRATION_TOKEN_KEY = "runner:registration_token"

    async def get_registration_token(self) -> str | None:
        return await self.r.get(self._REGISTRATION_TOKEN_KEY)

    async def get_registration_token_ttl(self) -> int:
        """接入 token 剩余有效秒:>0=剩余秒数,-1=永不过期,-2=不存在。"""
        return await self.r.ttl(self._REGISTRATION_TOKEN_KEY)

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
