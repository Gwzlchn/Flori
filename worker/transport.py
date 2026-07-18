"""WorkerTransport: worker 与协调/状态后端之间的唯一接口。

RedisTransport 直连 redis_client + db;逐方法转调。
GatewayTransport 实现同一 Protocol,全部换成出站 HTTPS,worker.py 不动。
worker.py 只依赖此 Protocol,不 import redis_client / Database。

把 register/heartbeat/update_status/update_step_result 的 "Redis+DB 双写" 封在
transport 内部,worker.py 不出现 asyncio.to_thread(self.db.xxx),双写顺序集中一处。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import structlog

from shared import runner_ops
from shared.db import Database
from shared.errors import (
    WorkerAuthRejected,
    WorkerConfigError,
    WorkerContractError,
    WorkerFatalError,
)
from shared.models import AIUsage, Worker as WorkerModel
from shared.redis_client import RedisClient, worker_info_from_model
from shared.step_output_commit import StaleCommitError


class WorkerTransport(Protocol):
    # per-worker token,供 GatewayStorage 经网关代理产物时鉴权;直连模式为空串。
    worker_token: str

    # 生命周期 / 心跳
    async def register(
        self, worker_id: str, worker_type: str, pools: list[str],
        tags: set[str], reject_tags: set[str], hostname: str, now: datetime,
        concurrency: int = 1, spec: dict | None = None,
    ) -> str: ...

    # 心跳返回中心期望配置 {"desired_config": dict|None, "cfg_rev": int}(热下发通道,
    # docs/03 §1.7.2);None = 本拍未取到(网络抖动等),worker 保持现配置。
    # applied_cfg_rev:worker 回报已生效版本,中心据此显示"待同步/已生效"。
    async def heartbeat(
        self, worker_id: str, load: dict | None = None, applied_cfg_rev: int = 0,
        concurrency: int | None = None,
    ) -> dict | None: ...

    async def update_status(
        self, worker_id: str, status: str,
        current_job: str = "", current_step: str = "",
    ) -> None: ...

    async def get_worker_status(self, worker_id: str) -> str | None: ...

    # 粗粒度认领/上报:编排封装在 transport 内,worker.execute 不直接调细粒度方法。
    # pool_limits:每池槽位上限(由 worker 从 config 算好传入,transport 保持不持有 config)。
    async def request_step(
        self, worker_id: str, pools: list[str], pool_limits: dict[str, int],
        tags: set[str], reject_tags: set[str],
    ) -> dict | None: ...

    # commit_token:成功路径 manifest 提交协议的一次性凭据(§2.6-8,done 与 token 同源);
    # None = 本步未发布 manifest(dual 阶段无 outputs 声明等保守跳过),走既有语义。
    async def report_done(
        self, claim: dict, duration: float, started_at: float,
        commit_token: dict | None = None,
    ) -> None: ...

    async def report_failed(
        self, claim: dict, error: str, error_type: str,
        duration: float, started_at: float, count_stats: bool,
    ) -> None: ...

    async def release(self, claim: dict) -> None: ...

    # 步骤产物 commit fence(§2.6-3/4):begin 签发一次性 token(None=围栏拒绝,陈旧执行);
    # confirm 校验 token(phase 非空时原子推进阶段),promote 前后与 manifest 发布前逐次调用。
    async def begin_step_commit(
        self, claim: dict, candidate_digest: str,
    ) -> dict | None: ...

    async def confirm_step_commit(
        self, claim: dict, token: dict, phase: str = "",
    ) -> bool: ...

    # 资源池 / 队列认领 + 步骤状态机(细粒度)
    # worker.execute 只用上面的粗粒度方法(request_step/report_*/release 等)。下面这些细粒度
    # 方法 worker 侧零调用:编排已封装在 runner_ops.claim_step/report_*/release_step 内,这里保留
    # 仅为与 RedisTransport/gateway 同 Protocol 的防御接口,非可用入口,避免误当 worker 可调。
    async def is_pool_frozen(self, pool: str) -> bool: ...
    async def try_acquire_slot(self, pool: str, limit: int, holder: str) -> bool: ...
    async def release_slot(self, pool: str, holder: str) -> None: ...
    async def freeze_pool(self, pool: str) -> None: ...
    async def unfreeze_pool(self, pool: str) -> None: ...
    async def dequeue_step_raw(self, pool: str) -> tuple[str, dict, float] | None: ...
    async def return_step(self, pool: str, raw_json: str, score: float) -> None: ...

    # 步骤状态机
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

    # 独立 AI task(kind='ai')
    async def set_ai_result(self, task_id: str, result: dict) -> None: ...
    async def record_ai_task_log(self, log: dict) -> bool: ...
    async def mark_ai_task_executing(self, claim: dict) -> bool: ...
    async def renew_ai_task_claim(self, claim: dict) -> bool: ...
    async def finish_ai_task_claim(self, claim: dict, outcome: str) -> bool: ...

    # Job 上下文
    async def get_job_pipeline(self, job_id: str) -> str | None: ...
    async def get_job_info(self, job_id: str) -> dict: ...

    # 下载凭证领取(docs/03 §1.7.1):中心分发,worker 零预置;未配置返回 None(匿名降级)。
    async def get_credential(self, key: str) -> str | None: ...

    # 事件
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

    # 生命周期 / 心跳
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
        # ttl 用 redis_client 的单一事实源默认(=online_window 兜底常量),不在此硬编码。
        await self._redis.register_worker(worker_id, info)
        worker_model = WorkerModel(
            id=worker_id, type=worker_type, pools=pools,
            tags=tags, reject_tags=reject_tags, hostname=hostname,
            status="idle", admin_status=admin_status, concurrency=concurrency,
            started_at=now, first_seen=now, last_heartbeat=now,
        )
        await asyncio.to_thread(self._db.upsert_worker, worker_model)
        # 注册即取中心期望配置(与 gateway register 响应对齐,首拍即齐);worker 读该属性应用。
        desired, cfg_rev = await asyncio.to_thread(
            self._db.get_worker_desired_config, worker_id)
        self.initial_config = {"desired_config": desired, "cfg_rev": cfg_rev}
        return worker_id

    async def heartbeat(self, worker_id, load=None, applied_cfg_rev=0,
                        concurrency: int | None = None):
        presence_existed = await self._redis.heartbeat(worker_id)
        if presence_existed is False:
            existing = await asyncio.to_thread(self._db.get_worker, worker_id)
            if existing:
                await self._redis.register_worker(
                    worker_id,
                    worker_info_from_model(
                        existing,
                        at=datetime.now(timezone.utc),
                        concurrency=concurrency,
                    ),
                )
        # live 负载落 redis worker hash 的 load 字段(JSON);/api/workers 读出透传到 WorkerResponse.load。
        # 仅 redis(实时态,不进 DB);采集为空则不写,保留上次。
        if load:
            await self._redis.set_worker_field(worker_id, "load", json.dumps(load))
        if applied_cfg_rev:
            await self._redis.set_worker_field(
                worker_id, "cfg_applied_rev", str(applied_cfg_rev))
        if concurrency is not None:
            await self._redis.set_worker_field(worker_id, "concurrency", str(concurrency))
        await asyncio.to_thread(
            self._db.update_worker_heartbeat,
            worker_id,
            concurrency=concurrency,
        )
        # 中心期望配置随心跳带回(与 gateway 心跳响应同契约)。
        desired, cfg_rev = await asyncio.to_thread(
            self._db.get_worker_desired_config, worker_id)
        return {"desired_config": desired, "cfg_rev": cfg_rev}

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

    async def report_done(self, claim, duration, started_at, commit_token=None):
        if commit_token is not None:
            # done 与 manifest 同 token(§2.6-8):token 已失效说明执行被换代,不得上报。
            if not await self._redis.validate_step_commit(
                claim["job_id"], claim["step"], commit_token,
            ):
                return
        await runner_ops.report_step_done(
            self._redis, self._db, self._worker_id, claim, duration, started_at,
        )
        if commit_token is not None:
            await self._redis.finish_step_commit(
                claim["job_id"], claim["step"], commit_token,
            )

    async def begin_step_commit(self, claim, candidate_digest):
        token, reason = await self._redis.begin_step_commit(
            job_id=claim["job_id"], step=claim["step"], exec_id=claim["exec_id"],
            generation=int(claim["generation"]), candidate_digest=candidate_digest,
            worker_id=self._worker_id,
        )
        if token is None:
            # 围栏拒绝=执行已换代,抛错跳过 done;返回 None 语义保留给
            # "中心不支持提交协议"(gateway 混跑窗口),两类日志事件名分开。
            structlog.get_logger(component="worker_transport").warning(
                "step_commit_fence_rejected", job_id=claim["job_id"],
                step=claim["step"], exec_id=claim["exec_id"], reason=reason,
            )
            raise StaleCommitError(f"commit fence rejected: {reason}")
        return token

    async def confirm_step_commit(self, claim, token, phase=""):
        ok = await self._redis.validate_step_commit(
            claim["job_id"], claim["step"], token, phase=phase,
        )
        if not ok:
            # 诊断分流(审查 P2-4):key 消失=TTL 过期/已消费;仍在但校验失败=执行已换代。
            state = await self._redis.get_step_commit(claim["job_id"], claim["step"])
            structlog.get_logger(component="worker_transport").warning(
                "step_commit_token_expired" if state is None else "step_commit_superseded",
                job_id=claim["job_id"], step=claim["step"], exec_id=claim["exec_id"],
            )
        return ok

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

    # 资源池 / 队列(纯转调)
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

    # 步骤状态机
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
        # 也累计进 Redis hash:远程(仅 Redis)worker 的统计才不会在 /api/workers 显示 0。
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
        return await asyncio.to_thread(self._db.record_ai_task_log, log)

    async def mark_ai_task_executing(self, claim):
        return await self._redis.mark_ai_task_executing(
            task_id=claim["task_id"], batch_id=claim.get("batch_id", ""),
            attempt=int(claim.get("attempt", 0)), revision=int(claim.get("revision", 0)),
            worker_id=self._worker_id, claim_id=claim["claim_id"],
        )

    async def renew_ai_task_claim(self, claim):
        return await self._redis.renew_ai_task_claim(
            task_id=claim["task_id"], batch_id=claim.get("batch_id", ""),
            attempt=int(claim.get("attempt", 0)), revision=int(claim.get("revision", 0)),
            worker_id=self._worker_id, claim_id=claim["claim_id"],
        )

    async def finish_ai_task_claim(self, claim, outcome):
        return await self._redis.finish_ai_task_claim(
            task_id=claim["task_id"], batch_id=claim.get("batch_id", ""),
            attempt=int(claim.get("attempt", 0)), revision=int(claim.get("revision", 0)),
            worker_id=self._worker_id, claim_id=claim["claim_id"], outcome=outcome,
        )

    # Job 上下文
    async def get_job_pipeline(self, job_id):
        return await self._redis.get_job_pipeline(job_id)

    async def get_job_info(self, job_id):
        return await self._redis.get_job_info(job_id)

    async def get_credential(self, key):
        # redis 镜像优先;miss 落 DB 解析(直连模式有 db 句柄)并回灌镜像。
        value = await self._redis.get_dispatch_credential(key)
        if value is None:
            from shared.credentials import resolve_from_db
            value = await asyncio.to_thread(resolve_from_db, self._db, key)
            if value:
                await self._redis.set_dispatch_credential(key, value)
        return value

    # 事件
    async def publish_step_event(self, channel, data):
        await self._redis.publish(channel, data)

    async def report_step_alive(self, job_id, step):
        await self._redis.set_step_progress_at(job_id, step)

    async def close(self):
        # redis/db 的关闭由 main.py 负责,此处 no-op。
        pass


_id_log = structlog.get_logger(component="worker_transport")


def default_worker_id_file() -> str:
    """worker id 缓存文件默认位置(直连与 gateway 共用,单一来源)。

    布局:`/data/workers/<name>/` 是该 worker 的家目录:id 缓存、Claude 凭证与配置、
    CLI transcript 等一切 worker 私有状态都收敛在自己目录下;id 文件为
    `<name>/worker.id`。WORKER_ID_FILE 显式覆盖语义不变;缺省 WORKER_NAME 归入 `default/`。

    旧布局(/data/workers/<name> 是平铺 id 文件)在此幂等迁移:读出 id,原地换成目录,
    写回 worker.id。id 内容不变,不触发重注册。迁移在代码里,任何宿主(NAS/边缘 ECS)自适用。"""
    explicit = os.environ.get("WORKER_ID_FILE", "").strip()
    if explicit:
        return explicit
    name = os.environ.get("WORKER_NAME", "").strip() or "default"
    new = Path(f"/data/workers/{name}/worker.id")
    # 幂等迁移旧平铺布局:有名 worker 的旧文件 = /data/workers/<name>(即新家目录同路径);
    # 无名 worker 的旧文件 = /data/workers/worker.id。缓存可选:无 /data 挂载写不了无碍(纯网关 id 服务端确定)。
    legacy_candidates = [Path(f"/data/workers/{name}")] + (
        [Path("/data/workers/worker.id")] if name == "default" else []
    )
    for legacy in legacy_candidates:
        try:
            if legacy.is_file() and not new.exists():
                wid = legacy.read_text().strip()
                legacy.unlink()
                new.parent.mkdir(parents=True, exist_ok=True)
                new.write_text(wid)
                _id_log.info("worker_id_file_migrated", src=str(legacy), dst=str(new), worker_id=wid)
                break
        except OSError:
            pass
    return str(new)


def create_transport(
    redis: RedisClient | None, db: Database | None,
) -> WorkerTransport:
    """按 env 切换:GATEWAY_URL 有值时使用 GatewayTransport(出站 HTTPS),否则 RedisTransport(直连)。

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
