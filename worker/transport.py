"""WorkerTransport：worker 与协调/状态后端之间的唯一接口。

RedisTransport 直连 redis_client + db;逐方法转调。
GatewayTransport 实现同一 Protocol,全部换成出站 HTTPS,worker.py 不动。
worker.py 只依赖此 Protocol,不再 import redis_client / Database。

把 register/heartbeat/update_status/update_step_result 的 "Redis+DB 双写" 封在
transport 内部,worker.py 不再出现 asyncio.to_thread(self.db.xxx),双写顺序集中一处。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Protocol

from shared import runner_ops
from shared.db import Database
from shared.models import AIUsage, Worker as WorkerModel
from shared.redis_client import RedisClient


class WorkerAuthRejected(Exception):
    """网关 runner 请求被服务端以 401 拒绝(per-worker token 失效/被吊销)。
    worker 据此【重注册 + 指数退避】;连续失败超时(默认 6h,env AUTH_GIVEUP_SEC)则自杀退出。
    仅 GatewayTransport(出站 HTTPS)会抛;直连 RedisTransport 无此异常。见 worker.Worker._handle_auth_failure。"""


class WorkerTransport(Protocol):
    # per-worker token,供 GatewayStorage 经网关代理产物时鉴权;直连模式为空串。
    worker_token: str

    # ── 生命周期 / 心跳 ──
    async def register(
        self, worker_id: str, worker_type: str, pools: list[str],
        tags: set[str], reject_tags: set[str], hostname: str, now: datetime,
        concurrency: int = 1, spec: dict | None = None,
    ) -> str: ...

    async def heartbeat(self, worker_id: str, load: dict | None = None) -> None: ...

    async def update_status(
        self, worker_id: str, status: str,
        current_job: str = "", current_step: str = "",
    ) -> None: ...

    async def get_worker_status(self, worker_id: str) -> str | None: ...

    # ── 粗粒度认领/上报:编排封装在 transport 内,worker.execute 不直接调细粒度方法 ──
    # pool_limits:每池槽位上限(由 worker 从 config 算好传入,transport 保持不持有 config)。
    async def request_step(
        self, worker_id: str, pools: list[str], pool_limits: dict[str, int],
        tags: set[str], reject_tags: set[str],
    ) -> dict | None: ...

    async def report_done(self, claim: dict, duration: float, started_at: float) -> None: ...

    async def report_failed(
        self, claim: dict, error: str, error_type: str,
        duration: float, started_at: float, count_stats: bool,
    ) -> None: ...

    async def release(self, claim: dict) -> None: ...

    # ── 资源池 / 队列认领 + 步骤状态机(细粒度)──
    # 注:worker.execute 只用上面的粗粒度方法(request_step/report_*/release 等)。下面这些细粒度
    # 方法 worker 侧零调用——编排已封装在 runner_ops.claim_step/report_*/release_step 内,这里保留
    # 仅为「与 RedisTransport/gateway 同 Protocol 的防御接口」,非可用入口(避免误当 worker 可调,见 B14)。
    async def is_pool_frozen(self, pool: str) -> bool: ...
    async def try_acquire_slot(self, pool: str, limit: int, holder: str) -> bool: ...
    async def release_slot(self, pool: str, holder: str) -> None: ...
    async def freeze_pool(self, pool: str) -> None: ...
    async def unfreeze_pool(self, pool: str) -> None: ...
    async def dequeue_step_raw(self, pool: str) -> tuple[str, dict, float] | None: ...
    async def return_step(self, pool: str, raw_json: str, score: float) -> None: ...

    # ── 步骤状态机 ──
    async def cas_step_status(
        self, job_id: str, step: str, expected: str, new: str,
    ) -> bool: ...
    async def set_step_worker(self, job_id: str, step: str, worker_id: str) -> None: ...
    async def update_step_result(
        self, job_id: str, step: str, *,
        status: str, worker_id: str,
        started_at: datetime, finished_at: datetime, duration_sec: float,
        error: str | None = None,
    ) -> None: ...
    async def increment_worker_stats(
        self, worker_id: str, *,
        completed: int = 0, failed: int = 0, duration: float = 0.0,
    ) -> None: ...
    async def record_ai_usage(self, usage: AIUsage) -> None: ...

    # ── 独立 AI task(kind='ai')──
    async def set_ai_result(self, task_id: str, result: dict) -> None: ...
    async def record_ai_task_log(self, log: dict) -> None: ...

    # ── Job 上下文 ──
    async def get_job_pipeline(self, job_id: str) -> str | None: ...
    async def get_job_info(self, job_id: str) -> dict: ...

    # ── 事件 ──
    async def publish_step_event(self, channel: str, data: dict) -> None: ...

    async def report_step_alive(self, job_id: str, step: str) -> None: ...

    async def close(self) -> None: ...


class RedisTransport:
    """直连 redis_client + db,逐方法转调。"""

    def __init__(self, redis: RedisClient, db: Database):
        self._redis = redis
        self._db = db
        # 粗粒度上报需要 worker_id,注册/认领时记下,report_*/release 据此回写。
        self._worker_id = ""
        # 直连模式不经网关代理产物,无 per-worker token(满足 Protocol、避免误用时 AttributeError)。
        self.worker_token = ""

    # ── 生命周期 / 心跳 ──
    async def register(self, worker_id, worker_type, pools, tags,
                       reject_tags, hostname, now, concurrency: int = 1,
                       spec: dict | None = None):
        # 重注册保留管理员暂停态(同 api register):从 DB 读回 admin_status,重启不清暂停。
        existing = await asyncio.to_thread(self._db.get_worker, worker_id)
        admin_status = existing.admin_status if existing else ""
        info = {
            "type": worker_type,
            "pools": ",".join(pools),
            "tags": ",".join(sorted(tags)),
            "reject_tags": ",".join(sorted(reject_tags)),
            "hostname": hostname,
            "status": "idle",
            "admin_status": admin_status,
            "concurrency": str(concurrency),
            "spec": json.dumps(spec or {}),   # 版本/机器配置(redis-only,前端 worker 详情展示)
            "started_at": now.isoformat(),
            "last_heartbeat": now.isoformat(),
        }
        self._worker_id = worker_id
        # 不再硬编码 ttl=30:用 redis_client 的单一事实源默认(=online_window 兜底常量)。
        await self._redis.register_worker(worker_id, info)
        worker_model = WorkerModel(
            id=worker_id, type=worker_type, pools=pools,
            tags=tags, reject_tags=reject_tags, hostname=hostname,
            status="idle", admin_status=admin_status, concurrency=concurrency,
            started_at=now, first_seen=now, last_heartbeat=now,
        )
        await asyncio.to_thread(self._db.upsert_worker, worker_model)
        return worker_id

    async def heartbeat(self, worker_id, load=None):
        await self._redis.heartbeat(worker_id)
        # live 负载落 redis worker hash 的 load 字段(JSON);/api/workers 读出透传到 WorkerResponse.load。
        # 仅 redis(实时态,不进 DB);采集为空则不写,保留上次。
        if load:
            await self._redis.set_worker_field(worker_id, "load", json.dumps(load))
        await asyncio.to_thread(self._db.update_worker_heartbeat, worker_id)

    async def update_status(self, worker_id, status,
                            current_job="", current_step=""):
        await self._redis.set_worker_field(worker_id, "status", status)
        await self._redis.set_worker_field(worker_id, "current_job", current_job)
        await self._redis.set_worker_field(worker_id, "current_step", current_step)
        await asyncio.to_thread(
            self._db.update_worker_heartbeat, worker_id,
            status=status, current_job=current_job, current_step=current_step,
        )

    async def get_worker_status(self, worker_id):
        info = await self._redis.get_worker_info(worker_id)
        return info.get("status") if info else None

    async def request_step(self, worker_id, pools, pool_limits, tags, reject_tags):
        self._worker_id = worker_id
        return await runner_ops.claim_step(
            self._redis, self._db, worker_id, pools, pool_limits, tags, reject_tags,
        )

    async def report_done(self, claim, duration, started_at):
        await runner_ops.report_step_done(
            self._redis, self._db, self._worker_id, claim, duration, started_at,
        )

    async def report_failed(self, claim, error, error_type, duration,
                            started_at, count_stats):
        await runner_ops.report_step_failed(
            self._redis, self._db, self._worker_id, claim,
            error, error_type, duration, started_at, count_stats,
        )

    async def release(self, claim):
        await runner_ops.release_step(
            self._redis, self._db, self._worker_id, claim,
        )

    # ── 资源池 / 队列(纯转调) ──
    async def is_pool_frozen(self, pool):
        return await self._redis.is_pool_frozen(pool)

    async def try_acquire_slot(self, pool, limit, holder):
        return await self._redis.try_acquire_slot(pool, limit, holder)

    async def release_slot(self, pool, holder):
        await self._redis.release_slot(pool, holder)

    async def freeze_pool(self, pool):
        await self._redis.freeze_pool(pool)

    async def unfreeze_pool(self, pool):
        await self._redis.unfreeze_pool(pool)

    async def dequeue_step_raw(self, pool):
        return await self._redis.dequeue_step_raw(pool)

    async def return_step(self, pool, raw_json, score):
        await self._redis.return_step(pool, raw_json, score)

    # ── 步骤状态机 ──
    async def cas_step_status(self, job_id, step, expected, new):
        return await self._redis.cas_step_status(job_id, step, expected, new)

    async def set_step_worker(self, job_id, step, worker_id):
        await self._redis.set_step_worker(job_id, step, worker_id)

    async def update_step_result(self, job_id, step, *, status, worker_id,
                                 started_at, finished_at, duration_sec,
                                 error=None):
        kwargs = dict(status=status, worker_id=worker_id,
                      started_at=started_at, finished_at=finished_at,
                      duration_sec=duration_sec)
        if error is not None:
            kwargs["error"] = error
        await asyncio.to_thread(self._db.update_step, job_id, step, **kwargs)

    async def increment_worker_stats(self, worker_id, *, completed=0,
                                     failed=0, duration=0.0):
        await asyncio.to_thread(
            self._db.increment_worker_stats, worker_id,
            completed=completed, failed=failed, duration=duration,
        )
        # 也累计进 Redis hash：远程(仅 Redis)worker 的统计才不会在 /api/workers 显示 0。
        if completed:
            await self._redis.incr_worker_stat(worker_id, "tasks_completed", completed)
        if failed:
            await self._redis.incr_worker_stat(worker_id, "tasks_failed", failed)
        if duration:
            await self._redis.incr_worker_stat(worker_id, "total_duration_sec", duration)

    async def record_ai_usage(self, usage):
        await asyncio.to_thread(self._db.record_ai_usage, usage)

    async def set_ai_result(self, task_id, result):
        await self._redis.set_ai_result(task_id, result)

    async def record_ai_task_log(self, log):
        await asyncio.to_thread(self._db.record_ai_task_log, log)

    # ── Job 上下文 ──
    async def get_job_pipeline(self, job_id):
        return await self._redis.get_job_pipeline(job_id)

    async def get_job_info(self, job_id):
        return await self._redis.get_job_info(job_id)

    # ── 事件 ──
    async def publish_step_event(self, channel, data):
        await self._redis.publish(channel, data)

    async def report_step_alive(self, job_id, step):
        await self._redis.set_step_progress_at(job_id, step)

    async def close(self):
        # redis/db 的关闭由 main.py 负责,此处 no-op。
        pass


def default_worker_id_file() -> str:
    """worker id 缓存文件默认位置(直连与 gateway 共用,单一来源):统一收进 /data/workers/ 文件夹,
    不再散在 /data 根。WORKER_ID_FILE 显式覆盖;否则 WORKER_NAME→/data/workers/<name>,缺省→worker.id。"""
    explicit = os.environ.get("WORKER_ID_FILE", "").strip()
    if explicit:
        return explicit
    name = os.environ.get("WORKER_NAME", "").strip()
    return f"/data/workers/{name}" if name else "/data/workers/worker.id"


def create_transport(
    redis: RedisClient | None, db: Database | None,
) -> WorkerTransport:
    """按 env 切换:GATEWAY_URL 有值→GatewayTransport(出站 HTTPS),否则 RedisTransport(直连)。

    GATEWAY_URL 模式下 redis/db 可为 None(纯网关零隧道):此时内层为 None,
    认领/产物/生命周期全走 gateway,worker 不连 redis/minio。
    """
    base_url = os.environ.get("GATEWAY_URL")
    if base_url:
        from worker.gateway_transport import GatewayTransport

        return GatewayTransport(
            base_url,
            registration_token=os.environ.get("WORKER_REGISTRATION_TOKEN", ""),
            id_file=default_worker_id_file(),
            inner=RedisTransport(redis, db) if redis is not None else None,
        )
    return RedisTransport(redis, db)
