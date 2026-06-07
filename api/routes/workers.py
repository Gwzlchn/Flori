"""Worker 管理路由。"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from shared.config import AppConfig
from shared.db import Database
from shared.redis_client import RedisClient
from shared.status import (
    DEFAULT_ONLINE_WINDOW_SEC,
    DEFAULT_STALE_WINDOW_SEC,
    OFFLINE,
    STALE,
    compute_worker_status,
)
from api.deps import get_config, get_db, get_redis, verify_token
from api.schemas import WorkerResponse, WorkerUpdateRequest

router = APIRouter(prefix="/api/workers", tags=["workers"], dependencies=[Depends(verify_token)])


def _windows(config: AppConfig) -> tuple[int, int]:
    """从 pools.yaml 读在线/失联窗口，缺省回退到内置默认（全栈共用一套阈值）。"""
    cfg = config.pools.get("worker_status", {}) if config else {}
    return (
        int(cfg.get("online_window_sec", DEFAULT_ONLINE_WINDOW_SEC)),
        int(cfg.get("stale_window_sec", DEFAULT_STALE_WINDOW_SEC)),
    )


def _iso_utc(value: datetime | str | None) -> str | None:
    """序列化时间戳为带 Z 后缀的 UTC ISO 串，让前端无歧义按 UTC 解析。
    入参可能是 DB 来的 aware datetime，或 Redis 里存的原始 ISO 串(可能 naive)。"""
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_response(w) -> WorkerResponse:
    return WorkerResponse(
        id=w.id, type=w.type, pools=w.pools,
        tags=sorted(w.tags), reject_tags=sorted(w.reject_tags),
        hostname=w.hostname, gpu_name=w.gpu_name, gpu_memory_mb=w.gpu_memory_mb,
        status=w.status,
        current_job=w.current_job, current_step=w.current_step,
        tasks_completed=w.tasks_completed, tasks_failed=w.tasks_failed,
        total_duration_sec=w.total_duration_sec,
        first_seen=_iso_utc(w.first_seen),
        started_at=_iso_utc(w.started_at),
        last_heartbeat=_iso_utc(w.last_heartbeat),
        admin_note=w.admin_note,
    )


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(value: str | None) -> datetime | None:
    """解析 Redis 里存的 ISO 时间串为 aware-UTC，naive 补 UTC。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@router.get("")
async def list_workers(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    online_window, stale_window = _windows(config)
    workers = await asyncio.to_thread(db.list_workers, online_window, stale_window)
    by_id: dict[str, WorkerResponse] = {w.id: _to_response(w) for w in workers}
    # 合并 Redis 里注册的远程 worker：本地 SQLite 没有它们(状态写在 Redis)，
    # 没这一步分布式 worker 在 /api/workers 里是隐身的。Redis key 带 TTL，
    # 失活的远程 worker 会自动消失。状态同样按 last_heartbeat 走后端权威判定，
    # 不信任 Redis 里 worker 自报的 status 字段。
    now = datetime.now(timezone.utc)
    for wid in await redis.list_worker_ids():
        if wid in by_id:
            continue
        info = await redis.get_worker_info(wid)
        if not info:
            continue
        status = compute_worker_status(
            last_heartbeat=_parse_iso(info.get("last_heartbeat")),
            current_job=info.get("current_job") or None,
            admin_status=info.get("status"),
            now=now,
            online_window_sec=online_window,
            stale_window_sec=stale_window,
        )
        by_id[wid] = WorkerResponse(
            id=wid,
            type=info.get("type", ""),
            pools=[p for p in info.get("pools", "").split(",") if p],
            tags=[t for t in info.get("tags", "").split(",") if t],
            reject_tags=[t for t in info.get("reject_tags", "").split(",") if t],
            hostname=info.get("hostname"),
            gpu_name=info.get("gpu_name") or None,
            gpu_memory_mb=_int(info.get("gpu_memory_mb")) or None,
            status=status,
            current_job=info.get("current_job") or None,
            current_step=info.get("current_step") or None,
            tasks_completed=_int(info.get("tasks_completed")),
            tasks_failed=_int(info.get("tasks_failed")),
            total_duration_sec=_float(info.get("total_duration_sec")),
            first_seen=_iso_utc(info.get("started_at") or info.get("last_heartbeat")) or "",
            started_at=_iso_utc(info.get("started_at")),
            last_heartbeat=_iso_utc(info.get("last_heartbeat")),
            admin_note=None,
        )
    return list(by_id.values())


@router.post("/registration-token")
async def mint_registration_token(redis: RedisClient = Depends(get_redis)):
    """铸/重置一次性接入 token（homelab 可复用 + 可重置，重铸即作废旧的）。"""
    token = "mnw-" + secrets.token_urlsafe(18)
    await redis.set_registration_token(token)
    return {"token": token}


@router.get("/{worker_id}")
async def get_worker(
    worker_id: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    online_window, stale_window = _windows(config)
    w = await asyncio.to_thread(db.get_worker, worker_id, online_window, stale_window)
    if not w:
        raise HTTPException(404, "worker not found")
    return _to_response(w)


@router.get("/{worker_id}/jobs")
async def list_worker_jobs(
    worker_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
):
    """该 worker 的任务历史（对应 runner 的 jobs 列表）。"""
    steps = await asyncio.to_thread(db.list_worker_jobs, worker_id, limit)
    return [
        {
            "job_id": s.job_id,
            "step": s.name,
            "status": s.status.value if hasattr(s.status, "value") else s.status,
            "started_at": _iso_utc(s.started_at),
            "finished_at": _iso_utc(s.finished_at),
            "duration_sec": s.duration_sec,
            "error": s.error,
        }
        for s in steps
    ]


@router.put("/{worker_id}")
async def update_worker(
    worker_id: str,
    req: WorkerUpdateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    w = await asyncio.to_thread(db.get_worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    if req.status is not None:
        w.status = req.status
    if req.admin_note is not None:
        w.admin_note = req.admin_note
    if req.tags is not None:
        w.tags = set(req.tags)
    if req.reject_tags is not None:
        w.reject_tags = set(req.reject_tags)
    await asyncio.to_thread(db.upsert_worker, w)
    # drain 真生效：worker 读 Redis 的 status 判 draining，只写 SQLite 不顶用，
    # 必须把 status 同步进 Redis 字段。tags 同理透传给在跑的 worker 认领逻辑。
    if req.status is not None:
        await redis.set_worker_field(worker_id, "status", req.status)
    if req.tags is not None:
        await redis.set_worker_field(worker_id, "tags", ",".join(sorted(req.tags)))
    if req.reject_tags is not None:
        await redis.set_worker_field(
            worker_id, "reject_tags", ",".join(sorted(req.reject_tags))
        )
    return {"id": worker_id, "status": "updated"}


@router.delete("/{worker_id}", status_code=204)
async def delete_worker(
    worker_id: str,
    force: bool = Query(False),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    config: AppConfig = Depends(get_config),
):
    online_window, stale_window = _windows(config)
    w = await asyncio.to_thread(db.get_worker, worker_id, online_window, stale_window)
    redis_alive = await redis.worker_exists(worker_id)
    if not w and not redis_alive:
        raise HTTPException(404, "worker not found")

    # 仅离线/失联可删，活着的需 force；否则下次扫描又冒出来（远程 worker 尤甚）。
    # 状态统一按心跳衍生：DB 有行用 DB 行，否则用 Redis hash 现算。
    if w is not None:
        status = w.status
    else:
        info = await redis.get_worker_info(worker_id) or {}
        status = compute_worker_status(
            last_heartbeat=_parse_iso(info.get("last_heartbeat")),
            current_job=info.get("current_job") or None,
            admin_status=info.get("status"),
            online_window_sec=online_window,
            stale_window_sec=stale_window,
        )
    if status not in (OFFLINE, STALE) and not force:
        raise HTTPException(409, "worker is online; pass force=true to remove")

    if w is not None:
        await asyncio.to_thread(db.delete_worker, worker_id)
    # 远程 worker 只在 Redis 里活着，必须连 Redis key 一起清，否则会复活。
    await redis.delete_worker(worker_id)
