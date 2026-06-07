"""Worker-gateway 路由：注册 / 心跳 / 下线（GitLab-runner 式瘦客户端控制面）。"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from shared.db import Database
from shared.models import Worker, generate_worker_id
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, verify_registration_token, verify_worker_token

# 注册接口自带门禁(registration token)，心跳/下线走 per-worker token，故不挂全局 verify_token。
router = APIRouter(prefix="/api/runner", tags=["runner"])

_HEARTBEAT_SEC = 10
_WORKER_TTL = 30


class RunnerRegisterRequest(BaseModel):
    worker_id: str | None = None
    type: str
    pools: list[str]
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)
    hostname: str | None = None


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
):
    """接入门禁通过后，服务端分配 worker_id、签发 per-worker token，
    并单写 Redis + DB（worker 不再双写），返回 token 仅此一次。"""
    await verify_registration_token(_bearer(request), redis)

    worker_id = req.worker_id or generate_worker_id(req.type)
    worker_token = "mnwt-" + secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(worker_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    await asyncio.to_thread(
        db.upsert_worker_token,
        token_hash=token_hash,
        worker_id=worker_id,
        pools=req.pools,
        tags=req.tags,
        created_at=now,
        revoked=False,
    )

    # 单写者：服务端同时写 Redis liveness 与 DB 行，info 形态与 RedisTransport.register 对齐。
    info = {
        "type": req.type,
        "pools": ",".join(req.pools),
        "tags": ",".join(sorted(req.tags)),
        "reject_tags": ",".join(sorted(req.reject_tags)),
        "hostname": req.hostname or "",
        "status": "idle",
        "started_at": now.isoformat(),
        "last_heartbeat": now.isoformat(),
    }
    await redis.register_worker(worker_id, info, ttl=_WORKER_TTL)
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
            started_at=now,
            first_seen=now,
            last_heartbeat=now,
        ),
    )
    return {
        "worker_id": worker_id,
        "worker_token": worker_token,
        "heartbeat_sec": _HEARTBEAT_SEC,
    }


@router.post("/heartbeat")
async def heartbeat(
    req: RunnerHeartbeatRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """刷新 Redis TTL + DB last_heartbeat；借返回值回发 drain 控制位。"""
    if req.worker_id != worker_id:
        raise HTTPException(status_code=403, detail="token/worker_id mismatch")
    await redis.heartbeat(worker_id, ttl=_WORKER_TTL)
    await asyncio.to_thread(
        db.update_worker_heartbeat,
        worker_id,
        status=req.status,
        current_job=req.current_job,
        current_step=req.current_step,
    )
    info = await redis.get_worker_info(worker_id) or {}
    return {"draining": info.get("status") == "draining"}


@router.post("/offline")
async def offline(
    req: RunnerOfflineRequest,
    worker_id: str = Depends(verify_worker_token),
    db: Database = Depends(get_db),
):
    """worker 主动下线：仅置 status=offline，不触碰 last_heartbeat。"""
    await asyncio.to_thread(db.set_worker_status, worker_id, "offline")
    return {"ok": True}
