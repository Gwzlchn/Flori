"""Worker 管理路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from shared.db import Database
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, verify_token
from api.schemas import WorkerResponse, WorkerUpdateRequest

router = APIRouter(prefix="/api/workers", tags=["workers"], dependencies=[Depends(verify_token)])


@router.get("")
async def list_workers(
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    workers = await asyncio.to_thread(db.list_workers)
    by_id: dict[str, WorkerResponse] = {
        w.id: WorkerResponse(
            id=w.id, type=w.type, pools=w.pools,
            hostname=w.hostname, status=w.status,
            current_job=w.current_job, current_step=w.current_step,
            tasks_completed=w.tasks_completed, tasks_failed=w.tasks_failed,
            total_duration_sec=w.total_duration_sec,
            first_seen=w.first_seen.isoformat(),
            started_at=w.started_at.isoformat() if w.started_at else None,
            last_heartbeat=w.last_heartbeat.isoformat() if w.last_heartbeat else None,
            admin_note=w.admin_note,
        )
        for w in workers
    }
    # 合并 Redis 里注册的远程 worker：本地 SQLite 没有它们(状态写在 Redis)，
    # 没这一步分布式 worker 在 /api/workers 里是隐身的。Redis key 带 TTL，
    # 失活的远程 worker 会自动消失。
    for wid in await redis.list_worker_ids():
        if wid in by_id:
            continue
        info = await redis.get_worker_info(wid)
        if not info:
            continue
        by_id[wid] = WorkerResponse(
            id=wid,
            type=info.get("type", ""),
            pools=[p for p in info.get("pools", "").split(",") if p],
            hostname=info.get("hostname"),
            status=info.get("status", "idle"),
            current_job=info.get("current_job") or None,
            current_step=info.get("current_step") or None,
            tasks_completed=0, tasks_failed=0, total_duration_sec=0.0,
            first_seen=info.get("started_at") or info.get("last_heartbeat", ""),
            started_at=info.get("started_at"),
            last_heartbeat=info.get("last_heartbeat"),
            admin_note=None,
        )
    return list(by_id.values())


@router.get("/{worker_id}")
async def get_worker(worker_id: str, db: Database = Depends(get_db)):
    w = await asyncio.to_thread(db.get_worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    return WorkerResponse(
        id=w.id, type=w.type, pools=w.pools,
        hostname=w.hostname, status=w.status,
        current_job=w.current_job, current_step=w.current_step,
        tasks_completed=w.tasks_completed, tasks_failed=w.tasks_failed,
        total_duration_sec=w.total_duration_sec,
        first_seen=w.first_seen.isoformat(),
        started_at=w.started_at.isoformat() if w.started_at else None,
        last_heartbeat=w.last_heartbeat.isoformat() if w.last_heartbeat else None,
        admin_note=w.admin_note,
    )


@router.put("/{worker_id}")
async def update_worker(worker_id: str, req: WorkerUpdateRequest, db: Database = Depends(get_db)):
    w = await asyncio.to_thread(db.get_worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    if req.status is not None:
        w.status = req.status
    if req.admin_note is not None:
        w.admin_note = req.admin_note
    await asyncio.to_thread(db.upsert_worker, w)
    return {"id": worker_id, "status": "updated"}


@router.delete("/{worker_id}", status_code=204)
async def delete_worker(worker_id: str, db: Database = Depends(get_db)):
    w = await asyncio.to_thread(db.get_worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    await asyncio.to_thread(db.delete_worker, worker_id)
