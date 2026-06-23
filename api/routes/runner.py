"""Worker-gateway 路由：注册 / 心跳 / 下线（GitLab-runner 式瘦客户端控制面）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from shared import runner_ops
from shared.config import AppConfig
from shared.db import Database
from shared.models import AIUsage, Worker, generate_worker_id
from shared.redis_client import RedisClient
from shared.status import DEFAULT_ONLINE_WINDOW_SEC
from shared.storage import StorageBackend, is_credential_file
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

# 注册接口自带门禁(registration token)，心跳/下线走 per-worker token，故不挂全局 verify_token。
router = APIRouter(prefix="/api/runner", tags=["runner"])

# 长轮询:服务端持有窗口须小于 worker httpx 读超时(35s),空轮询间隔避免空转打爆 Redis。
_CLAIM_WINDOW_SEC = 25.0
_CLAIM_POLL_SEC = 0.5


def _worker_ttl(config: AppConfig) -> int:
    """Redis worker liveness key 的 TTL = 配置的 online_window_sec(单一事实源,与对外
    在线判定同一窗口)。此前硬编码 _WORKER_TTL=30 与 pools.yaml online_window_sec=30 是两处
    独立常量,改一处不同步;现统一由 config 驱动,缺省回落 shared.status 的兜底常量。"""
    ws = (config.pools or {}).get("worker_status") or {}
    return int(ws.get("online_window_sec", DEFAULT_ONLINE_WINDOW_SEC))


class RunnerRegisterRequest(BaseModel):
    worker_id: str | None = None
    type: str
    pools: list[str]
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)
    hostname: str | None = None
    concurrency: int = 1
    spec: dict = Field(default_factory=dict)   # 版本/机器配置(worker 自报,redis-only)


class RunnerHeartbeatRequest(BaseModel):
    worker_id: str
    status: str = "idle"
    current_job: str = ""
    current_step: str = ""


class RunnerOfflineRequest(BaseModel):
    worker_id: str


def _bearer(request: Request) -> str:
    """从 Authorization: Bearer 头取出 token（注册接口的接入门禁用）。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@router.post("/register")
async def register(
    req: RunnerRegisterRequest,
    request: Request,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    """接入门禁通过后，服务端分配 worker_id、签发 per-worker token，
    并单写 Redis + DB（worker 不再双写），返回 token 仅此一次。"""
    await verify_registration_token(_bearer(request), redis)

    worker_id = req.worker_id or generate_worker_id(req.type)
    worker_token = "flwt-" + secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(worker_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    # 重注册保留管理员暂停态:worker 重启不应清掉暂停(否则每次重启都要重新暂停)。
    # 从 DB(持久,无 TTL)读已有 admin_status;新 worker 无记录则为空。
    _existing = await asyncio.to_thread(db.get_worker, worker_id)
    admin_status = _existing.admin_status if _existing else ""

    await asyncio.to_thread(
        db.upsert_worker_token,
        token_hash=token_hash,
        worker_id=worker_id,
        pools=req.pools,
        tags=req.tags,
        created_at=now,
        revoked=False,
    )

    # 连接来源:网关 worker 经 Caddy→隧道→api,真实客户端 IP 在 X-Forwarded-For(Caddy 注入);
    # 无该头(本机直连 api)则取 request.client.host。供详情页显示「worker 从哪连过来」。
    _xff = request.headers.get("x-forwarded-for", "")
    remote_addr = _xff.split(",")[0].strip() if _xff else (request.client.host if request.client else "")
    # 单写者：服务端同时写 Redis liveness 与 DB 行，info 形态与 RedisTransport.register 对齐。
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
            started_at=now,
            first_seen=now,
            last_heartbeat=now,
        ),
    )
    return {
        "worker_id": worker_id,
        "worker_token": worker_token,
    }


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
    await redis.heartbeat(worker_id, ttl=_worker_ttl(config))
    await asyncio.to_thread(
        db.update_worker_heartbeat,
        worker_id,
        status=req.status,
        current_job=req.current_job,
        current_step=req.current_step,
    )
    # 心跳只刷存活;暂停态由服务端 claim_step 据 admin_status 兜底(不经心跳回发控制位)。
    return {"ok": True}


@router.post("/offline")
async def offline(
    req: RunnerOfflineRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
):
    """worker 主动下线：仅置 status=offline，不触碰 last_heartbeat。"""
    await asyncio.to_thread(db.set_worker_status, worker_id, "offline")
    return {"ok": True}


# ── 认领 / 上报:服务端执行编排,gateway 模式 worker 无需直连 redis ──


async def _enrich_claim(redis: RedisClient, claim: dict) -> dict:
    """把 pipeline/domain/style_tags 塞进 claim,让 gateway worker 无需回读 redis
    (parse 逻辑与旧 worker.execute 逐字等价:json-or-list,失败兜空)。"""
    job_id = claim["job_id"]
    pipeline = await redis.get_job_pipeline(job_id)
    job_info = await redis.get_job_info(job_id)
    domain = job_info.get("domain", "general")
    style_tags = runner_ops.parse_style_tags(job_info.get("style_tags", "[]"))
    return {**claim, "pipeline": pipeline, "domain": domain, "style_tags": style_tags}


def _clamp_pool_limits(
    config: AppConfig, allowed: list[str], client_limits: dict,
) -> dict[str, int]:
    """以服务端 pools.yaml 为权威夹取每池并发上限:绝不信任 worker 自报的 pool_limits
    ——否则错误/恶意 worker 报一个超大 limit 即可突破全局每池并发(如 ai=2 被打成 ai=999)。
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
    """长轮询认领一步:窗口内反复 claim_step,认到就 enrich 后返回,否则 {"claim": null}。"""
    # per-token 授权:把请求池裁剪到 token 注册时授权的池子(空授权列表=不限,兼容旧 token)。
    token = getattr(request.state, "worker_token", None)
    authorized_pools = (token or {}).get("pools") or []
    if authorized_pools:
        allowed = [p for p in req.pools if p in set(authorized_pools)]
    else:
        allowed = list(req.pools)
    # 剔除 pools.yaml 未声明的池:缺失池在 _clamp/claim 都回落哨兵 999(fail-open),视为无效
    # 不认领,使配置缺失/漂移 fail-safe 而非 fail-open(审计:_clamp_pool_limits fail-open)。
    _server_pools = (config.pools or {}).get("pools", {}) or {}
    allowed = [p for p in allowed if p in _server_pools]
    # 越权/无效池被裁空 → 无可认领,返回 null(非错误:worker 请求范围外的池自然认不到)。
    if not allowed:
        return {"claim": None}

    # 服务端权威夹取并发上限(不信任客户端自报),堵全局并发被突破。
    effective_limits = _clamp_pool_limits(config, allowed, req.pool_limits)

    deadline = time.monotonic() + _CLAIM_WINDOW_SEC
    while True:
        claim = await runner_ops.claim_step(
            redis, db, worker_id, allowed, effective_limits,
            set(req.tags), set(req.reject_tags),
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
    claim = {"job_id": job_id, "step": step, "pool": req.pool, "exec_id": req.exec_id}
    await runner_ops.report_step_done(
        redis, db, worker_id, claim, req.duration, req.started_at,
    )
    return {"ok": True}


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
    claim = {"job_id": job_id, "step": step, "pool": req.pool, "exec_id": req.exec_id}
    await runner_ops.report_step_failed(
        redis, db, worker_id, claim, req.error, req.error_type,
        req.duration, req.started_at, req.count_stats,
    )
    return {"ok": True}


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
    claim = {"job_id": job_id, "step": step, "pool": req.pool, "exec_id": req.exec_id}
    await runner_ops.release_step(redis, db, worker_id, claim)
    return {"ok": True}


@router.post("/jobs/{job_id}/steps/{step}/progress")
async def step_progress(
    job_id: str,
    step: str,
    req: RunnerProgressRequest,
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    """运行中进度/日志:发到 events:{job_id},供前端 WS 准实时拉取(gateway on_progress)。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    # 固定字段后置:payload 若含 "event" 键不能覆盖 step_progress(审计 I-L8)。
    await redis.publish(f"events:{job_id}", {**req.payload, "event": "step_progress"})
    return {"ok": True}


@router.post("/jobs/{job_id}/steps/{step}/alive")
async def step_alive(
    job_id: str,
    step: str,
    worker_id: str = Depends(verify_worker_token),
    redis: RedisClient = Depends(get_redis),
):
    """步进度心跳:刷新 redis 步进度时间戳(worker on_tick 每 10s 调,仅子进程存活时),
    供 scheduler.check_stuck 对远程 job(产物不落调度器盘)判进度停滞。"""
    validate_path_segment(job_id, "job_id")
    validate_path_segment(step, "step")
    await redis.set_step_progress_at(job_id, step)
    return {"ok": True}


@router.post("/usage")
async def record_usage(
    req: RunnerUsageRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
):
    """记录一次 AI 调用用量(exec_id UNIQUE 去重,重复返回 ok 不报错)。"""
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
    await asyncio.to_thread(db.record_ai_usage, usage)
    return {"ok": True}


# ── 产物代理:worker<->API<->storage,minio 永不暴露给 worker ──


def _validate_rel(rel: str) -> None:
    # 防目录穿越:禁 ".."、绝对路径、空字节(与 artifact 端点的 job_id 校验同风格)。
    if ".." in rel or rel.startswith("/") or "\x00" in rel:
        raise HTTPException(400, "invalid artifact path")


@router.get("/jobs/{job_id}/artifacts")
async def list_artifacts(
    job_id: str,
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
):
    """产物清单:GatewayStorage.pull 据此逐个拉取。敏感凭证侧载文件不下发给远端 worker。"""
    validate_path_segment(job_id, "job_id")
    files = await storage.list_files(job_id)
    return {"files": [f for f in files if not is_credential_file(f)]}


@router.get("/jobs/{job_id}/artifacts/{rel:path}")
async def get_artifact(
    job_id: str,
    rel: str,
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
):
    """取单个产物字节;不存在返回 404(GatewayStorage.read_file 据此返回 None)。
    敏感凭证侧载文件对远端 worker 一律 404(只供同机 LocalStorage 本地读)。"""
    validate_path_segment(job_id, "job_id")
    _validate_rel(rel)
    if is_credential_file(rel):
        raise HTTPException(404, "artifact not found")
    data = await storage.read_file(job_id, rel)
    if data is None:
        raise HTTPException(404, "artifact not found")
    return Response(content=data, media_type="application/octet-stream")


@router.put("/jobs/{job_id}/artifacts/{rel:path}")
async def put_artifact(
    job_id: str,
    rel: str,
    request: Request,
    worker_id: str = Depends(verify_worker_token),
    storage: StorageBackend = Depends(get_storage),
):
    """回传单个产物:原始 body 直接写入 storage(worker push 的中转出口)。"""
    validate_path_segment(job_id, "job_id")
    _validate_rel(rel)
    if is_credential_file(rel):
        # 与 get_artifact 对称:禁止经网关回传写入凭证侧载文件(.credentials.json),
        # 防任意已注册 worker plant 一个「同机下载步随后会读」的凭证文件。
        raise HTTPException(403, "writing credential files is not allowed")
    data = await request.body()
    await storage.write_file(job_id, rel, data)
    return {"ok": True}
