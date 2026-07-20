"""Worker-gateway 路由:注册 / 心跳 / 认领 / 上报 / 产物代理(GitLab-runner 式瘦客户端控制面)。

协议契约见 docs/03-contracts.md §1.7,出站 HTTPS 网关设计理由见 ADR-0009。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from shared import runner_ops
from shared.ask_citations import validate_bound_ask_citations
from shared.config import AppConfig
from shared.db import Database
from shared.models import AIUsage, Worker, generate_worker_id
from shared.redis_client import RedisClient, worker_info_from_model
from shared.step_scope import parse_execution_step
from shared.status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    OFFLINE,
    STALE,
    compute_worker_status,
)
from shared.step_manifest import is_internal_namespace_path, manifest_relative_path
from shared.step_output_commit import build_commit_record
from shared.storage import (
    ArtifactTooLarge,
    StepCommitFenceRejected,
    StepCommitIntegrityError,
    StorageBackend,
    execution_artifact_allowed,
    is_credential_file,
)
from api.deps import (
    get_config,
    get_db,
    get_redis,
    get_storage,
    validate_path_segment,
    verify_registration_token,
    verify_worker_token,
)
from api.schemas import (
    RunnerClaimRequest,
    RunnerCompleteRequest,
    RunnerFailRequest,
    RunnerProgressRequest,
    RunnerReleaseRequest,
    RunnerUsageRequest,
)

# 注册接口自带门禁(registration token),心跳/下线走 per-worker token,故不挂全局 verify_token。
router = APIRouter(prefix="/api/runner", tags=["runner"])

logger = structlog.get_logger(component="runner")

# 长轮询:服务端持有窗口须小于 worker httpx 读超时(35s),空轮询间隔避免空转打爆 Redis。
_CLAIM_WINDOW_SEC = 25.0
_CLAIM_POLL_SEC = 0.5
_ARTIFACT_CHUNK_SIZE = 1024 * 1024
_ARTIFACT_MAX_BYTES = 10 * 1024 * 1024 * 1024


def _worker_ttl(config: AppConfig) -> int:
    """Redis worker liveness key 的 TTL 取配置的 online_window_sec,与对外在线判定
    共用同一窗口(单一事实源,避免两处常量改一处不同步);缺省回落 shared.status 的兜底常量。"""
    ws = (config.pools or {}).get("worker_status") or {}
    return int(ws.get("online_window_sec", DEFAULT_ONLINE_WINDOW_SEC))


def _worker_windows(config: AppConfig) -> tuple[int, int]:
    ws = (config.pools or {}).get("worker_status") or {}
    return (
        int(ws.get("online_window_sec", DEFAULT_ONLINE_WINDOW_SEC)),
        int(ws.get("stale_window_sec", DEFAULT_STALE_WINDOW_SEC)),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _remote_addr(request: Request) -> str:
    _xff = request.headers.get("x-forwarded-for", "")
    return _xff.split(",")[0].strip() if _xff else (request.client.host if request.client else "")


class RunnerTaskLeaseHeaders(BaseModel):
    job_id: str
    step: str
    exec_id: str


def _task_lease_headers(
    lease_job: str = Header(default="", alias="X-Flori-Lease-Job"),
    lease_step: str = Header(default="", alias="X-Flori-Lease-Step"),
    lease_exec: str = Header(default="", alias="X-Flori-Lease-Exec"),
) -> RunnerTaskLeaseHeaders:
    """把任务租约头显式纳入 OpenAPI,同时保留统一的 403 失败语义."""
    return RunnerTaskLeaseHeaders(job_id=lease_job, step=lease_step, exec_id=lease_exec)


async def _require_task_lease(
    redis: RedisClient,
    worker_id: str,
    job_id: str,
    step: str,
    exec_id: str,
    *,
    renew: bool = False,
    require_active: bool = True,
    expected_pool: str = "",
) -> None:
    """统一校验 runner 四元组租约,拒绝缺失,过期和已被 rerun 替换的执行."""
    if not job_id or not step or not exec_id:
        raise HTTPException(status_code=403, detail="active task lease required")
    validate_path_segment(job_id, "lease job_id")
    validate_path_segment(step, "lease step")
    validate_path_segment(exec_id, "lease exec_id")
    valid = await redis.validate_task_lease(
        worker_id, job_id, step, exec_id,
        renew=renew, require_active=require_active, expected_pool=expected_pool,
    )
    if not valid:
        logger.warning(
            "task_lease_rejected", worker_id=worker_id, job_id=job_id,
            step=step, exec_id=exec_id,
        )
        raise HTTPException(status_code=403, detail="task lease is stale or out of scope")


async def _require_ai_claim(
    redis: RedisClient,
    worker_id: str,
    task_id: str,
    lease: RunnerTaskLeaseHeaders,
    *,
    states: set[str] | None = None,
    require_unexpired: bool = True,
) -> dict:
    """校验独立 AI task 的 worker/task/step/exec 专用租约。"""
    if lease.job_id != task_id or not lease.step or not lease.exec_id:
        raise HTTPException(status_code=403, detail="AI task lease path mismatch")
    validate_path_segment(task_id, "task_id")
    validate_path_segment(lease.step, "lease step")
    validate_path_segment(lease.exec_id, "lease exec_id")
    claim = await redis.get_ai_task_claim(task_id)
    original = await redis.get_ai_task_original_payload(task_id)
    bound_step = claim.get("step") if claim else None
    if not bound_step and type(original) is dict:
        bound_step = original.get("step", "ai")
    if (
        not claim
        or claim.get("task_id") != task_id
        or claim.get("worker_id") != worker_id
        or claim.get("claim_id") != lease.exec_id
        or bound_step != lease.step
        or (states is not None and claim.get("state") not in states)
    ):
        raise HTTPException(status_code=403, detail="AI task lease is stale or out of scope")
    if require_unexpired:
        try:
            active = float(claim.get("lease_until", 0)) > time.time()
        except (TypeError, ValueError):
            active = False
        if not active:
            raise HTTPException(status_code=403, detail="AI task lease expired")
    return claim


async def _begin_terminal(
    redis: RedisClient,
    worker_id: str,
    job_id: str,
    step: str,
    exec_id: str,
    outcome: str,
    pool: str,
) -> bool:
    validate_path_segment(exec_id, "exec_id")
    validate_path_segment(pool, "pool")
    state = await redis.begin_task_terminal(
        worker_id, job_id, step, exec_id, outcome, expected_pool=pool,
    )
    if state == 0:
        raise HTTPException(status_code=403, detail="task lease is stale or out of scope")
    return state == 1


class RunnerRegisterRequest(BaseModel):
    worker_id: str | None = None
    type: str
    pools: list[str]
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)
    hostname: str | None = None
    concurrency: int = 1
    spec: dict = Field(default_factory=dict)   # 版本/机器配置(worker 自报,redis-only)


class RunnerResumeRequest(BaseModel):
    worker_id: str
    type: str
    pools: list[str]
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)
    hostname: str | None = None
    concurrency: int = 1
    spec: dict = Field(default_factory=dict)


class RunnerHeartbeatRequest(BaseModel):
    worker_id: str
    status: str = "idle"
    current_job: str = ""
    current_step: str = ""
    concurrency: int | None = Field(default=None, ge=1, le=64)
    load: dict = Field(default_factory=dict)   # 本机 live 负载 {cpu_pct,mem_pct,loadavg};可空
    applied_cfg_rev: int = 0                   # worker 已生效的配置版本(回报,前端显示同步态)
    # 在跑步集合 [{job_id,step,exec_id}]:心跳捎带,为每个并发步刷进度心跳.独立 alive 通道
    # 在部分外网链路不达(实测),心跳借道使 orphan_scan 判活不再依赖单点。
    running: list[dict] = Field(default_factory=list)


class RunnerAIResultRequest(BaseModel):
    result: dict = Field(default_factory=dict)


class RunnerAILogRequest(BaseModel):
    log: dict = Field(default_factory=dict)


class RunnerAIFinishRequest(BaseModel):
    outcome: str


class RunnerOfflineRequest(BaseModel):
    worker_id: str


def _bearer(request: Request) -> str:
    """从 Authorization: Bearer 头取出 token(注册接口的接入门禁用)。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _redis_worker_public_status(
    redis: RedisClient, worker_id: str, config: AppConfig,
) -> str | None:
    info = await redis.get_worker_info(worker_id)
    if not info:
        return None
    online_window, stale_window = _worker_windows(config)
    return compute_worker_status(
        last_heartbeat=_parse_iso(info.get("last_heartbeat")),
        current_job=info.get("current_job") or None,
        admin_status=info.get("admin_status"),
        online_window_sec=online_window,
        stale_window_sec=stale_window,
    )


async def _upsert_worker_presence(
    *,
    req: RunnerRegisterRequest | RunnerResumeRequest,
    worker_id: str,
    request: Request,
    db: Database,
    redis: RedisClient,
    config: AppConfig,
) -> tuple[dict | None, int]:
    """注册/resume 的单写路径:刷新 Redis 实时态并 upsert DB worker 行。

    DB 管理态来自旧行;desired_config/cfg_rev 不在 upsert 列清单中,由 DB helper 保留。"""
    now = datetime.now(timezone.utc)
    existing = await asyncio.to_thread(db.get_worker, worker_id)
    admin_status = existing.admin_status if existing else ""
    remote_addr = _remote_addr(request)
    info = {
        "type": req.type,
        "pools": ",".join(req.pools),
        "tags": ",".join(sorted(req.tags)),
        "reject_tags": ",".join(sorted(req.reject_tags)),
        "hostname": req.hostname or "",
        "status": "idle",
        "admin_status": admin_status,
        "concurrency": str(req.concurrency),
        "remote_addr": remote_addr,
        "spec": json.dumps(req.spec or {}),
        "started_at": now.isoformat(),
        "last_heartbeat": now.isoformat(),
    }
    await redis.register_worker(worker_id, info, ttl=_worker_ttl(config))
    await asyncio.to_thread(
        db.upsert_worker,
        Worker(
            id=worker_id,
            type=req.type,
            pools=req.pools,
            tags=set(req.tags),
            reject_tags=set(req.reject_tags),
            hostname=req.hostname,
            status="idle",
            admin_status=admin_status,
            concurrency=req.concurrency,
            remote_addr=remote_addr or None,
            current_job=None,
            current_step=None,
            tasks_completed=existing.tasks_completed if existing else 0,
            tasks_failed=existing.tasks_failed if existing else 0,
            total_duration_sec=existing.total_duration_sec if existing else 0.0,
            first_seen=existing.first_seen if existing else now,
            started_at=now,
            last_heartbeat=now,
            admin_note=existing.admin_note if existing else None,
        ),
    )
    return await asyncio.to_thread(db.get_worker_desired_config, worker_id)


@router.post("/register")
async def register(
    req: RunnerRegisterRequest,
    request: Request,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    """接入门禁通过后,服务端分配 worker_id、签发 per-worker token,
    并作为单一写者写 Redis + DB;返回 token 仅此一次。"""
    await verify_registration_token(_bearer(request), redis)

    worker_id = req.worker_id or generate_worker_id(req.type)
    public_status = await _redis_worker_public_status(redis, worker_id, config)
    if public_status not in (None, OFFLINE, STALE):
        raise HTTPException(
            status_code=409,
            detail="duplicate worker is already online; use cached worker token to resume",
        )

    issued = "flwt-" + secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(issued.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    await asyncio.to_thread(
        db.upsert_worker_token,
        token_hash=token_hash,
        worker_id=worker_id,
        pools=req.pools,
        tags=req.tags,
        created_at=now,
        revoked=False,
        revoke_existing=True,
    )

    desired, cfg_rev = await _upsert_worker_presence(
        req=req, worker_id=worker_id, request=request, db=db, redis=redis, config=config,
    )
    # 连接事件发到 events:system,/system 事件页可见谁连上了/重注册(含 type/host/版本/来源)。best-effort。
    try:
        await redis.push_event(
            "worker_registered", worker_id=worker_id, worker_type=req.type,
            host=req.hostname or "", source=_remote_addr(request),
            version=request.headers.get("x-worker-version", ""),
        )
    except Exception:
        pass
    response = {
        "worker_id": worker_id,
        "desired_config": desired,
        "cfg_rev": cfg_rev,
    }
    response["worker_token"] = issued
    return response


@router.post("/resume")
async def resume(
    req: RunnerResumeRequest,
    request: Request,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    """持久 per-worker token 恢复在线态;不签发新 token。"""
    if req.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="token/worker_id mismatch")
    desired, cfg_rev = await _upsert_worker_presence(
        req=req, worker_id=worker_id, request=request, db=db, redis=redis, config=config,
    )
    try:
        await redis.push_event(
            "worker_resumed", worker_id=worker_id, worker_type=req.type,
            host=req.hostname or "", source=_remote_addr(request),
            version=request.headers.get("x-worker-version", ""),
        )
    except Exception:
        pass
    return {"worker_id": worker_id, "desired_config": desired, "cfg_rev": cfg_rev}


@router.post("/heartbeat")
async def heartbeat(
    req: RunnerHeartbeatRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    """刷新 Redis TTL + DB last_heartbeat。"""
    if req.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="token/worker_id mismatch")
    presence_existed = await redis.heartbeat(worker_id, ttl=_worker_ttl(config))
    if presence_existed is False:
        existing = await asyncio.to_thread(db.get_worker, worker_id)
        if existing:
            now = datetime.now(timezone.utc)
            await redis.register_worker(
                worker_id,
                worker_info_from_model(
                    existing,
                    at=now,
                    status=req.status,
                    concurrency=req.concurrency,
                ),
                ttl=_worker_ttl(config),
            )
    for item in (req.running or [])[:64]:
        if not isinstance(item, dict):
            continue
        j, st, ex = (
            item.get("job_id", ""), item.get("step", ""), item.get("exec_id", ""),
        )
        if j and st and ex and await redis.validate_task_lease(
            worker_id, j, st, ex, renew=True,
        ):
            await redis.set_step_progress_at(j, st)
    # live 负载落 redis worker hash(实时态,不进 DB);为空则不写,保留上次。
    if req.load:
        await redis.set_worker_field(worker_id, "load", json.dumps(req.load))
    if req.concurrency is not None:
        await redis.set_worker_field(worker_id, "concurrency", str(req.concurrency))
    await asyncio.to_thread(
        db.update_worker_heartbeat,
        worker_id,
        status=req.status,
        current_job=req.current_job,
        current_step=req.current_step,
        concurrency=req.concurrency,
    )
    # worker 回报已生效配置版本,写入 redis hash 供前端显示同步状态.
    if req.applied_cfg_rev:
        await redis.set_worker_field(worker_id, "cfg_applied_rev", str(req.applied_cfg_rev))
    # 心跳回发中心期望配置(热下发通道):worker 比对 cfg_rev 决定是否应用。
    # 暂停态仍由服务端 claim_step 据 admin_status 兜底(不经心跳回发控制位)。
    desired, cfg_rev = await asyncio.to_thread(db.get_worker_desired_config, worker_id)
    return {"ok": True, "desired_config": desired, "cfg_rev": cfg_rev}


@router.post("/offline")
async def offline(
    req: RunnerOfflineRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
):
    """worker 主动下线:仅置 status=offline,不触碰 last_heartbeat。"""
    if req.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="token/worker_id mismatch")
    await asyncio.to_thread(db.set_worker_status, worker_id, "offline")
    return {"ok": True}


# 认领 / 上报:服务端执行编排,gateway 模式 worker 无需直连 redis。


async def _enrich_claim(redis: RedisClient, claim: dict) -> dict:
    """把 pipeline/domain/style_tags/source 塞进 claim,让 gateway worker 无需回读 redis;
    style_tags 解析 json-or-list,失败兜空。source 供下载步凭证按需领取。"""
    if claim.get("kind") == "ai":
        return claim
    job_id = claim["job_id"]
    pipeline = await redis.get_job_pipeline(job_id)
    job_info = await redis.get_job_info(job_id)
    domain = job_info.get("domain", "general")
    style_tags = runner_ops.parse_style_tags(job_info.get("style_tags", "[]"))
    return {
        **claim,
        "pipeline": pipeline,
        "domain": domain,
        "style_tags": style_tags,
        "source": claim.get("source") or job_info.get("source", ""),
    }


def _clamp_pool_limits(
    config: AppConfig, allowed: list[str], client_limits: dict,
) -> dict[str, int]:
    """以服务端 pools.yaml 为权威夹取每池并发上限:绝不信任 worker 自报的 pool_limits
    否则错误或恶意 worker 可用超大 limit 突破全局每池并发.
    client 值只允许调低不允许调高;缺省/非法则取服务端值。"""
    server_pools = (config.pools or {}).get("pools", {}) or {}
    effective: dict[str, int] = {}
    for pool in allowed:
        server_limit = int((server_pools.get(pool) or {}).get("limit", 999))
        raw = (client_limits or {}).get(pool, server_limit)
        try:
            client_limit = int(raw)
        except (TypeError, ValueError):
            client_limit = server_limit
        effective[pool] = max(0, min(client_limit, server_limit))
    return effective


@router.post("/jobs/request")
async def request_job(
    req: RunnerClaimRequest,
    request: Request,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    """长轮询认领一步并签发短期任务租约;窗口内无任务返回 {"claim": null}."""
    # per-token 授权:把请求池裁剪到 token 注册时授权的池子(空授权列表=不限,兼容旧 token)。
    token = getattr(request.state, "worker_token", None)
    authorized_pools = (token or {}).get("pools") or []
    if authorized_pools:
        allowed = [p for p in req.pools if p in set(authorized_pools)]
    else:
        allowed = list(req.pools)
    # 剔除 pools.yaml 未声明的池:缺失池在 _clamp/claim 都回落哨兵 999 属 fail-open,
    # 故视为无效不认领,使配置缺失/漂移 fail-safe。
    _server_pools = (config.pools or {}).get("pools", {}) or {}
    allowed = [p for p in allowed if p in _server_pools]
    # 越权或无效池被裁空时无可认领,返回 null;worker 请求范围外的池自然认不到.
    if not allowed:
        return {"claim": None}

    # 服务端权威夹取并发上限(不信任客户端自报),堵全局并发被突破。
    effective_limits = _clamp_pool_limits(config, allowed, req.pool_limits)

    deadline = time.monotonic() + _CLAIM_WINDOW_SEC
    while True:
        claim = await runner_ops.claim_step(
            redis, db, worker_id, allowed, effective_limits,
            set(req.tags), set(req.reject_tags), data_dir=config.data_dir,
        )
        if claim is not None:
            return {"claim": await _enrich_claim(redis, claim)}
        if time.monotonic() >= deadline:
            return {"claim": None}
        await asyncio.sleep(_CLAIM_POLL_SEC)


@router.post("/jobs/{job_id}/steps/{step}/complete")
async def complete_step(
    job_id: str,
    step: str,
    req: RunnerCompleteRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    # manifest 提交协议:done 与 manifest 同 token(§2.6-8),token 失效即陈旧执行。
    if req.commit_token is not None and not await redis.validate_step_commit(
        job_id, step, req.commit_token,
    ):
        return {"ok": False, "stale": True}
    first = await _begin_terminal(
        redis, worker_id, job_id, step, req.exec_id, "done", req.pool,
    )
    if not first:
        return {"ok": True, "duplicate": True}
    generation = await redis.get_step_generation(job_id, step)
    claim = {
        "job_id": job_id, "step": step, "pool": req.pool,
        "exec_id": req.exec_id, "generation": generation,
    }
    try:
        accepted = await runner_ops.report_step_done(
            redis, db, worker_id, claim, req.duration, req.started_at,
        )
    except Exception:
        await redis.reset_task_terminal(worker_id, job_id, step, req.exec_id, "done")
        raise
    if not accepted:
        return {"ok": False, "stale": True}
    if req.commit_token is not None:
        await redis.finish_step_commit(job_id, step, req.commit_token)
    return {"ok": True, "duplicate": False}


@router.post("/jobs/{job_id}/steps/{step}/fail")
async def fail_step(
    job_id: str,
    step: str,
    req: RunnerFailRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    first = await _begin_terminal(
        redis, worker_id, job_id, step, req.exec_id, "failed", req.pool,
    )
    if not first:
        return {"ok": True, "duplicate": True}
    generation = await redis.get_step_generation(job_id, step)
    claim = {
        "job_id": job_id, "step": step, "pool": req.pool,
        "exec_id": req.exec_id, "generation": generation,
    }
    try:
        accepted = await runner_ops.report_step_failed(
            redis, db, worker_id, claim, req.error, req.error_type,
            req.duration, req.started_at, req.count_stats,
        )
    except Exception:
        await redis.reset_task_terminal(worker_id, job_id, step, req.exec_id, "failed")
        raise
    if not accepted:
        return {"ok": False, "stale": True}
    return {"ok": True, "duplicate": False}


@router.post("/jobs/{job_id}/steps/{step}/release")
async def release_step(
    job_id: str,
    step: str,
    req: RunnerReleaseRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    validate_path_segment(req.exec_id, "exec_id")
    validate_path_segment(req.pool, "pool")
    valid = await redis.validate_task_lease(
        worker_id, job_id, step, req.exec_id,
        require_active=False, expected_pool=req.pool,
    )
    if not valid:
        if await redis.validate_released_task_lease(
            worker_id, job_id, step, req.exec_id, req.pool,
        ):
            return {"ok": True, "duplicate": True}
        raise HTTPException(status_code=403, detail="task lease is stale or out of scope")
    claim = {"job_id": job_id, "step": step, "pool": req.pool, "exec_id": req.exec_id}
    await runner_ops.release_step(redis, db, worker_id, claim)
    return {"ok": True}


@router.post("/ai-tasks/{task_id}/release")
async def release_ai_task(
    task_id: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """释放当前 AI claim 的池槽;终态和过期 claim 也可按原身份幂等释放。"""
    claim = await _require_ai_claim(
        redis, worker_id, task_id, lease, require_unexpired=False,
    )
    await runner_ops.release_step(redis, db, worker_id, {
        "kind": "ai", "task_id": task_id, "step": lease.step,
        "pool": "ai", "exec_id": lease.exec_id,
        "claim_id": claim.get("claim_id"),
    })
    return {"ok": True}


def _ai_claim_kwargs(claim: dict, worker_id: str) -> dict:
    """只从服务端 claim 取 CAS 身份,不采信 Worker body 自报。"""
    return {
        "task_id": claim["task_id"],
        "batch_id": claim.get("batch_id", ""),
        "attempt": int(claim.get("attempt", 0)),
        "revision": int(claim.get("revision", 0)),
        "worker_id": worker_id,
        "claim_id": claim["claim_id"],
    }


@router.post("/ai-tasks/{task_id}/executing")
async def mark_ai_task_executing(
    task_id: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    claim = await _require_ai_claim(
        redis, worker_id, task_id, lease, states={"claimed"},
    )
    changed = await redis.mark_ai_task_executing(
        **_ai_claim_kwargs(claim, worker_id), now_epoch=time.time(),
    )
    if not changed:
        raise HTTPException(status_code=403, detail="AI task claim transition rejected")
    return {"ok": True}


@router.post("/ai-tasks/{task_id}/renew")
async def renew_ai_task_claim(
    task_id: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    claim = await _require_ai_claim(
        redis, worker_id, task_id, lease, states={"claimed", "executing"},
    )
    changed = await redis.renew_ai_task_claim(
        **_ai_claim_kwargs(claim, worker_id),
        state=claim["state"], now_epoch=time.time(),
    )
    if not changed:
        raise HTTPException(status_code=403, detail="AI task claim renewal rejected")
    return {"ok": True}


@router.post("/ai-tasks/{task_id}/result")
async def set_ai_task_result(
    task_id: str,
    req: RunnerAIResultRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    await _require_ai_claim(
        redis, worker_id, task_id, lease, states={"executing"},
    )
    # 服务端锚点必须先存在。Worker 只能提交结果副本,不能凭结果重建锚点。
    original = await redis.get_ai_task_original_payload(task_id)
    if original is None:
        raise HTTPException(status_code=409, detail="AI task provenance anchor missing")
    result = dict(req.result)
    if lease.step == "synthesis":
        audit_context = original.get("audit_context")
        original_manifest = (
            audit_context.get("ask_source_manifest")
            if type(audit_context) is dict else None
        )
        result["citation_validation"] = validate_bound_ask_citations(
            task_id, str(result.get("content") or ""),
            result.get("source_manifest"), original_manifest,
        )
    elif lease.step == "digest":
        from api.services.radar import validate_digest_citations

        audit_context = original.get("audit_context")
        original_manifest = (
            audit_context.get("digest_source_manifest")
            if type(audit_context) is dict else None
        )
        for field in (
            "audit_context", "digest_source_manifest", "manifest_sha256",
            "source_manifest",
        ):
            result.pop(field, None)
        result["source_manifest"] = (
            original_manifest if type(original_manifest) is dict else None
        )
        result["citation_validation"] = validate_digest_citations(
            task_id, str(result.get("content") or ""), original_manifest,
        )
    await redis.set_ai_result(task_id, result)
    return {"ok": True}


@router.post("/ai-tasks/{task_id}/log")
async def record_ai_task_log(
    task_id: str,
    req: RunnerAILogRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    await _require_ai_claim(
        redis, worker_id, task_id, lease, states={"executing"},
    )
    record = dict(req.log)
    record.update({
        "task_id": task_id, "exec_id": lease.exec_id, "step_name": lease.step,
    })
    raw_record = record.get("record")
    if type(raw_record) is dict:
        raw_record = dict(raw_record)
        raw_record.update({
            "task_id": task_id, "exec_id": lease.exec_id, "step": lease.step,
        })
        record["record"] = raw_record
    written = await asyncio.to_thread(db.record_ai_task_log, record)
    if not written:
        raise HTTPException(status_code=503, detail="AI task audit persistence failed")
    return {"ok": True}


@router.post("/ai-tasks/{task_id}/finish")
async def finish_ai_task_claim(
    task_id: str,
    req: RunnerAIFinishRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    if req.outcome not in {"succeeded", "failed"}:
        raise HTTPException(status_code=422, detail="invalid AI task outcome")
    claim = await _require_ai_claim(
        redis, worker_id, task_id, lease, states={"executing"},
    )
    changed = await redis.finish_ai_task_claim(
        **_ai_claim_kwargs(claim, worker_id), outcome=req.outcome,
    )
    if not changed:
        raise HTTPException(status_code=403, detail="AI task claim finish rejected")
    await redis.publish(f"events:{task_id}", {
        "event": "ai_task_done" if req.outcome == "succeeded" else "ai_task_failed",
        "task_id": task_id, "step": lease.step,
    })
    return {"ok": True}


@router.post("/jobs/{job_id}/steps/{step}/progress")
async def step_progress(
    job_id: str,
    step: str,
    req: RunnerProgressRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    """运行中进度/日志:发到 events:{job_id},供前端 WS 准实时拉取(gateway on_progress)。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    if lease.job_id != job_id or lease.step != step:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, step, lease.exec_id, renew=True,
    )
    # 固定字段后置:payload 若含 "event" 键不能覆盖 step_progress。
    await redis.publish(f"events:{job_id}", {**req.payload, "event": "step_progress"})
    return {"ok": True}


@router.post("/jobs/{job_id}/steps/{step}/alive")
async def step_alive(
    job_id: str,
    step: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    """步进度心跳:刷新 redis 步进度时间戳。worker on_tick 每 10s 调,仅子进程存活时;
    供 scheduler.check_stuck 对产物不落调度器盘的远程 job 判进度停滞。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    if lease.job_id != job_id or lease.step != step:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, step, lease.exec_id, renew=True,
    )
    await redis.set_step_progress_at(job_id, step)
    return {"ok": True}


@router.get("/credentials/{key}")
async def get_dispatch_credential(
    key: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
    db: Database = Depends(get_db),
):
    """下载凭证领取:仅当前 01_download 租约可访问白名单 key,缓存 miss 回源 DB.
    value=null 表示中心未配置该凭证(worker 匿名降级,不视为错误)。
    每次领取记审计事件 credential_issued,补上文件共享时代缺失的凭证审计。"""
    from shared.credentials import DISPATCH_KEYS, resolve_from_db

    await _require_task_lease(
        redis, worker_id, lease.job_id, lease.step, lease.exec_id, renew=True,
    )
    _, template_step = parse_execution_step(lease.step)
    if template_step != "01_download":
        raise HTTPException(status_code=403, detail="credential access requires download lease")

    if key not in DISPATCH_KEYS:
        raise HTTPException(404, f"unknown credential key: {key}")
    value = await redis.get_dispatch_credential(key)
    if value is None:
        value = await asyncio.to_thread(resolve_from_db, db, key)
        if value:
            await redis.set_dispatch_credential(key, value)
    logger.info("credential_issued", worker_id=worker_id, key=key, present=bool(value))
    return {"key": key, "value": value}


@router.post("/usage")
async def record_usage(
    request: Request,
    req: RunnerUsageRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """在当前任务租约内记录 AI 调用用量(exec_id UNIQUE,重复上报幂等)."""
    if req.job_id is None:
        if req.step != lease.step or req.exec_id != lease.exec_id:
            raise HTTPException(status_code=403, detail="usage AI task lease mismatch")
        await _require_ai_claim(
            redis, worker_id, lease.job_id, lease, states={"executing"},
        )
    else:
        if req.job_id != lease.job_id or req.step != lease.step:
            raise HTTPException(status_code=403, detail="usage task lease mismatch")
        await _require_task_lease(
            redis, worker_id, lease.job_id, lease.step, lease.exec_id,
            renew=True,
        )
    usage = AIUsage(
        exec_id=req.exec_id,
        provider=req.provider,
        model=req.model,
        job_id=req.job_id,
        step=req.step,
        worker_id=worker_id,   # 以鉴权 token 认定的 worker 为准(权威),忽略 body 自报
        input_tokens=req.input_tokens,
        output_tokens=req.output_tokens,
        cache_creation_input_tokens=req.cache_creation_input_tokens,
        cache_read_input_tokens=req.cache_read_input_tokens,
        cost_usd=req.cost_usd,
        duration_sec=req.duration_sec,
        num_turns=req.num_turns,
        cached=req.cached,
    )
    # 用 LiteLLM 价表填权威成本. claude-cli CLI 用 CLI total_cost_usd,空表或未命中则保留上报值.
    if req.provider != "claude-cli":
        pricing = getattr(request.app.state, "pricing", None)
        if pricing is not None:
            c = pricing.cost(req.provider, req.model, req.input_tokens, req.output_tokens,
                             req.cache_creation_input_tokens, req.cache_read_input_tokens)
            if c is not None:
                usage.cost_usd = round(c, 6)
    await asyncio.to_thread(db.record_ai_usage, usage)
    return {"ok": True}


# 产物代理:worker<->API<->storage,minio 永不暴露给 worker。


def _validate_rel(rel: str) -> None:
    # 防目录穿越:禁 ".."、绝对路径、空字节(与 artifact 端点的 job_id 校验同风格)。
    normalized = rel.replace("\\", "/")
    if (".." in normalized or normalized.startswith("/") or "\x00" in normalized
            or normalized.startswith(".flori-upload/")):
        raise HTTPException(400, "invalid artifact path")


def _artifact_range(value: str | None, size: int) -> tuple[int, int, int]:
    """解析单段 bytes Range,返回 start,length,HTTP status."""
    if not value:
        return 0, size, 200
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(416, "invalid range", headers={"Content-Range": f"bytes */{size}"})
    spec = value[6:]
    left, sep, right = spec.partition("-")
    if not sep:
        raise HTTPException(416, "invalid range", headers={"Content-Range": f"bytes */{size}"})
    try:
        if not left:
            suffix = int(right)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(left)
            end = int(right) if right else size - 1
    except ValueError:
        raise HTTPException(416, "invalid range", headers={"Content-Range": f"bytes */{size}"})
    if size <= 0 or start < 0 or start >= size or end < start:
        raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    return start, end - start + 1, 206


@router.get("/jobs/{job_id}/artifacts")
async def list_artifacts(
    job_id: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """列出当前任务可拉取的产物;敏感凭证和内部暂存文件永不下发."""
    validate_path_segment(job_id, "job_id")
    if lease.job_id != job_id:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
    )
    files = await storage.list_files(job_id)
    return {
        "files": [
            f for f in files
            if not is_credential_file(f)
            and execution_artifact_allowed(lease.step, f, write=False)
        ],
    }


@router.get("/jobs/{job_id}/artifacts/{rel:path}")
async def get_artifact(
    job_id: str,
    rel: str,
    request: Request,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """按当前任务租约流式读取产物,支持单段 Range;敏感凭证一律返回 404."""
    validate_path_segment(job_id, "job_id")
    _validate_rel(rel)
    if is_credential_file(rel):
        raise HTTPException(404, "artifact not found")
    if lease.job_id != job_id:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
    )
    if not execution_artifact_allowed(lease.step, rel, write=False):
        raise HTTPException(403, "artifact is outside task scope")
    size = await storage.file_size(job_id, rel)
    if size is None:
        raise HTTPException(404, "artifact not found")
    start, length, status_code = _artifact_range(request.headers.get("range"), size)
    stream = await storage.open_stream(
        job_id, rel, start=start, length=length, chunk_size=_ARTIFACT_CHUNK_SIZE,
    )
    if stream is None:
        raise HTTPException(404, "artifact not found")

    async def _counted() -> AsyncIterator[bytes]:
        sent = 0
        checked_at = time.monotonic()
        try:
            async for chunk in stream:
                if time.monotonic() - checked_at >= 30:
                    await _require_task_lease(
                        redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
                    )
                    checked_at = time.monotonic()
                sent += len(chunk)
                yield chunk
        finally:
            await redis.incr_traffic("pull", worker_id, sent)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
    return StreamingResponse(
        _counted(), status_code=status_code, headers=headers,
        media_type="application/octet-stream",
    )


@router.put("/jobs/{job_id}/artifacts/{rel:path}")
async def put_artifact(
    job_id: str,
    rel: str,
    request: Request,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """分块接收产物并校验大小/摘要,成功后原子发布;失败只清理暂存内容."""
    validate_path_segment(job_id, "job_id")
    _validate_rel(rel)
    if is_credential_file(rel):
        # 与 get_artifact 对称:禁止经网关回传写入凭证侧载文件(.credentials.json),
        # 防任意已注册 worker 植入一个同机下载步随后会读的凭证文件。
        raise HTTPException(403, "writing credential files is not allowed")
    if is_internal_namespace_path(rel):
        # .flori 内部命名空间(manifest/staging)只能经 commit 协议写入,防伪造 final manifest。
        raise HTTPException(403, "writing internal namespace is not allowed")
    if lease.job_id != job_id:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
    )
    if not execution_artifact_allowed(lease.step, rel, write=True):
        raise HTTPException(403, "artifact is outside task scope")
    length_header = request.headers.get("content-length")
    try:
        expected_size = int(length_header) if length_header is not None else None
    except ValueError:
        raise HTTPException(400, "invalid content length")
    if expected_size is not None and (expected_size < 0 or expected_size > _ARTIFACT_MAX_BYTES):
        raise HTTPException(413, "artifact too large")
    expected_sha256 = request.headers.get("x-content-sha256")
    if expected_sha256 and (
        len(expected_sha256) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in expected_sha256)
    ):
        raise HTTPException(400, "invalid artifact checksum")

    async def _checked_upload() -> AsyncIterator[bytes]:
        checked_at = time.monotonic()
        async for chunk in request.stream():
            if time.monotonic() - checked_at >= 30:
                await _require_task_lease(
                    redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
                )
                checked_at = time.monotonic()
            yield chunk

    try:
        result = await storage.write_stream(
            job_id,
            rel,
            _checked_upload(),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            max_bytes=_ARTIFACT_MAX_BYTES,
        )
    except ArtifactTooLarge:
        raise HTTPException(413, "artifact too large")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    await redis.incr_traffic("push", worker_id, result["size"])
    return {"ok": True, **result}


# 步骤产物提交协议(设计稿 §2.6):commit fence 签发/校验与中心侧 staging/promote。
# Gateway worker 不直连 Redis/MinIO,同一 Lua 语义经这些端点执行。


class RunnerCommitBeginRequest(BaseModel):
    candidate_digest: str


class RunnerCommitConfirmRequest(BaseModel):
    token: dict
    phase: str = ""


class RunnerStagingCopyRequest(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str


class RunnerStepCommitRequest(BaseModel):
    token: dict
    outputs: list[dict] = Field(default_factory=list)
    manifest: dict
    manifest_rel: str = ""
    stale_paths: list[str] = Field(default_factory=list)


@router.post("/jobs/{job_id}/steps/{step}/commit/begin")
async def begin_step_commit(
    job_id: str,
    step: str,
    req: RunnerCommitBeginRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    """原子校验实时 job generation/step exec/running/租约后签发一次性 commit token;
    围栏拒绝(陈旧执行/在途 commit)返回 409。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    if lease.job_id != job_id or lease.step != step:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(redis, worker_id, job_id, step, lease.exec_id, renew=True)
    generation = await redis.get_step_generation(job_id, step)
    if generation is None:
        raise HTTPException(status_code=409, detail="step generation unknown")
    token, reason = await redis.begin_step_commit(
        job_id=job_id, step=step, exec_id=lease.exec_id, generation=generation,
        candidate_digest=req.candidate_digest, worker_id=worker_id,
    )
    if token is None:
        raise HTTPException(status_code=409, detail=f"commit fence rejected: {reason}")
    return {"token": token}


@router.post("/jobs/{job_id}/steps/{step}/commit/confirm")
async def confirm_step_commit(
    job_id: str,
    step: str,
    req: RunnerCommitConfirmRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    """promote 前后逐次校验 commit token;phase 非空时原子推进围栏阶段。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    if lease.job_id != job_id or lease.step != step:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, step, lease.exec_id, renew=True,
        require_active=False,
    )
    # phase 白名单:围栏只有一个合法推进(manifest_published),其余值拒绝(审查落地项)。
    if req.phase not in ("", "manifest_published"):
        raise HTTPException(422, "invalid commit phase")
    ok = await redis.validate_step_commit(job_id, step, req.token, phase=req.phase)
    return {"ok": ok}


@router.post("/jobs/{job_id}/staging/copy")
async def stage_from_canonical(
    job_id: str,
    req: RunnerStagingCopyRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """canonical 已有同尺寸对象时服务端复制进执行 staging(免二次过慢链路);
    read-back 在 promote 后对 canonical 全量重验,复制来源不影响完整性结论。"""
    validate_path_segment(job_id, "job_id")
    _validate_rel(req.path)
    if lease.job_id != job_id:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
    )
    if not execution_artifact_allowed(lease.step, req.path, write=True):
        raise HTTPException(403, "artifact is outside task scope")
    if is_credential_file(req.path) or is_internal_namespace_path(req.path):
        raise HTTPException(403, "staging this path is not allowed")
    staged = await storage.stage_from_canonical(
        job_id, lease.exec_id, req.path, size_bytes=req.size_bytes,
    )
    return {"staged": bool(staged)}


@router.put("/jobs/{job_id}/staging/{rel:path}")
async def put_staging_artifact(
    job_id: str,
    rel: str,
    request: Request,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """candidate 输出直传执行 staging namespace(canonical 复制不可用时的兜底通道)。"""
    validate_path_segment(job_id, "job_id")
    _validate_rel(rel)
    if is_credential_file(rel) or is_internal_namespace_path(rel):
        raise HTTPException(403, "staging this path is not allowed")
    if lease.job_id != job_id:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
    )
    if not execution_artifact_allowed(lease.step, rel, write=True):
        raise HTTPException(403, "artifact is outside task scope")
    length_header = request.headers.get("content-length")
    try:
        expected_size = int(length_header) if length_header is not None else None
    except ValueError:
        raise HTTPException(400, "invalid content length")
    if expected_size is not None and (expected_size < 0 or expected_size > _ARTIFACT_MAX_BYTES):
        raise HTTPException(413, "artifact too large")
    expected_sha256 = request.headers.get("x-content-sha256")

    async def _checked_upload() -> AsyncIterator[bytes]:
        checked_at = time.monotonic()
        async for chunk in request.stream():
            if time.monotonic() - checked_at >= 30:
                await _require_task_lease(
                    redis, worker_id, job_id, lease.step, lease.exec_id, renew=True,
                )
                checked_at = time.monotonic()
            yield chunk

    try:
        result = await storage.write_execution_staging_stream(
            job_id, lease.exec_id, rel, _checked_upload(),
            expected_size=expected_size, expected_sha256=expected_sha256,
            max_bytes=_ARTIFACT_MAX_BYTES,
        )
    except ArtifactTooLarge:
        raise HTTPException(413, "artifact too large")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    await redis.incr_traffic("push", worker_id, result["size"])
    return {"ok": True, **result}


@router.post("/jobs/{job_id}/steps/{step}/commit")
async def commit_step_outputs(
    job_id: str,
    step: str,
    req: RunnerStepCommitRequest,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """中心侧执行九步协议 6/7 步:token 保护下 staging→canonical promote、
    read-back、按旧 manifest 精确删旧输出、manifest 最后原子发布。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    if lease.job_id != job_id or lease.step != step:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(redis, worker_id, job_id, step, lease.exec_id, renew=True)
    if req.token.get("exec_id") != lease.exec_id:
        raise HTTPException(status_code=409, detail="commit token exec mismatch")
    if not await redis.validate_step_commit(job_id, step, req.token):
        raise HTTPException(status_code=409, detail="commit fence rejected the token")
    for entry in req.outputs:
        rel = entry.get("path")
        if type(rel) is not str or not rel:
            raise HTTPException(422, "invalid output entry path")
        _validate_rel(rel)
        if (
            is_credential_file(rel)
            or is_internal_namespace_path(rel)
            or not execution_artifact_allowed(lease.step, rel, write=True)
        ):
            raise HTTPException(403, f"output outside task scope: {rel}")
        if type(entry.get("size_bytes")) is not int or entry["size_bytes"] < 0:
            raise HTTPException(422, f"invalid output size: {rel}")
        if type(entry.get("sha256")) is not str:
            raise HTTPException(422, f"invalid output sha256: {rel}")
    # stale_paths 与 outputs 同一守卫环(审查 P1):跨步/跨 Part/内部命名空间/凭证
    # 的删除请求一律 4xx,拒绝即零删除(storage 层另有纵深防御)。
    for rel in req.stale_paths:
        if type(rel) is not str or not rel:
            raise HTTPException(422, "invalid stale path entry")
        _validate_rel(rel)
        if (
            is_credential_file(rel)
            or is_internal_namespace_path(rel)
            or not execution_artifact_allowed(lease.step, rel, write=True)
        ):
            raise HTTPException(403, f"stale path outside task scope: {rel}")
    # manifest 路径由服务端按执行键权威计算,不采信客户端 manifest_rel。
    scope_key, template_step = parse_execution_step(step)
    try:
        manifest_rel = manifest_relative_path(scope_key, template_step)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    record = build_commit_record(
        job_id=job_id, execution_step=step, exec_id=lease.exec_id,
        token=req.token, manifest_digest=str(req.token.get("candidate_digest")),
        output_job_rels=[entry["path"] for entry in req.outputs],
    )

    async def _verify(phase: str = "") -> bool:
        return await redis.validate_step_commit(job_id, step, req.token, phase=phase)

    try:
        await storage.commit_step_outputs(
            job_id, step, lease.exec_id,
            outputs=req.outputs, manifest=req.manifest, manifest_rel=manifest_rel,
            stale_paths=req.stale_paths, token=req.token, commit_record=record,
            verify_token=_verify,
        )
    except StepCommitFenceRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except StepCommitIntegrityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True}


@router.delete("/jobs/{job_id}/staging")
async def cleanup_execution_staging(
    job_id: str,
    lease: RunnerTaskLeaseHeaders = Depends(_task_lease_headers),
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
    redis: RedisClient = Depends(get_redis),
):
    """清理当前执行的 staging namespace(§2.6-9);done 回执后调用,故不要求 running。"""
    validate_path_segment(job_id, "job_id")
    if lease.job_id != job_id:
        raise HTTPException(status_code=403, detail="task lease path mismatch")
    await _require_task_lease(
        redis, worker_id, job_id, lease.step, lease.exec_id,
        require_active=False,
    )
    await storage.cleanup_execution_staging(job_id, lease.exec_id)
    return {"ok": True}
