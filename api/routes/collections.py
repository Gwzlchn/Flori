"""集合管理路由。订阅是集合的属性(无独立订阅实体/页面)：
source_type/source_id 非空 = 订阅集合，自动从该来源追更新内容入本集合。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from shared.db import Database
from shared.models import Collection, collection_id_for_subscription, generate_collection_id
from shared.redis_client import RedisClient
from shared.storage import StorageBackend

from api.deps import get_db, get_redis, get_storage, verify_token
from api.schemas import (
    CollectionCreateRequest,
    CollectionResponse,
    CollectionSubscriptionInfo,
    CollectionUpdateRequest,
    JobListResponse,
    JobResponse,
)

logger = structlog.get_logger(component="collections")
router = APIRouter(
    prefix="/api/collections", tags=["collections"],
    dependencies=[Depends(verify_token)],
)


def _validate_collection_id(collection_id: str) -> None:
    if ".." in collection_id or "/" in collection_id or "\x00" in collection_id:
        raise HTTPException(400, "invalid collection_id")


def _to_response(c: Collection) -> CollectionResponse:
    sub = None
    if c.is_subscription:
        sub = CollectionSubscriptionInfo(
            source_type=c.source_type, source_id=c.source_id,
            enabled=c.sync_enabled,
            last_synced_at=c.last_synced_at.isoformat() if c.last_synced_at else None,
        )
    return CollectionResponse(
        id=c.id, name=c.name, domain=c.domain, description=c.description,
        tags=c.tags, job_count=c.job_count, created_at=c.created_at.isoformat(),
        subscription=sub,
    )


async def sync_collection(
    coll: Collection, db: Database, redis: RedisClient, storage: StorageBackend,
) -> dict:
    """枚举订阅集合的来源(UP) → 跟已入库去重 → 新内容自动建 job(归入本集合)。
    返回 {total, new, skipped}。仅订阅集合可调。"""
    if not coll.is_subscription:
        raise ValueError("not a subscription collection")
    if coll.source_type != "bilibili_up":
        raise ValueError(f"unsupported source_type: {coll.source_type}")
    from shared.bili_space import enumerate_up
    from api.routes.jobs import create_job_core

    cookies = await asyncio.to_thread(db.get_credential, "bili_cookies")
    videos = await enumerate_up(coll.source_id, cookies)
    ingested = await asyncio.to_thread(db.ingested_bvids)
    new = [v for v in videos if v["bvid"] not in ingested]
    for v in new:
        await create_job_core(
            db, redis, storage,
            url=f"https://www.bilibili.com/video/{v['bvid']}",
            content_type="video", domain=coll.domain,
            collection_id=coll.id, title=(v.get("title") or "").strip() or None,
        )
        await asyncio.sleep(0.2)  # 轻微间隔,别瞬时灌爆队列/触发风控
    await asyncio.to_thread(db.mark_collection_synced, coll.id, datetime.now(timezone.utc))
    logger.info("collection_synced", coll=coll.id, total=len(videos), new=len(new))
    return {"total": len(videos), "new": len(new), "skipped": len(videos) - len(new)}


@router.post("", status_code=201, response_model=CollectionResponse)
async def create_collection(
    req: CollectionCreateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    is_sub = bool(req.source_type and req.source_id)
    if is_sub:
        # 订阅集合：domain 必须显式且非 general(否则术语沉错领域)；来源全局唯一。
        if not req.domain or req.domain == "general":
            raise HTTPException(400, "订阅集合必须选择真实领域(不能为 general)")
        if await asyncio.to_thread(db.find_collection_by_source, req.source_type, req.source_id):
            raise HTTPException(400, "该来源已订阅")
        cid = collection_id_for_subscription(req.source_type, req.source_id)
    else:
        cid = generate_collection_id()

    collection = Collection(
        id=cid, name=req.name, domain=req.domain,
        description=req.description or "", tags=req.tags,
        source_type=req.source_type if is_sub else None,
        source_id=req.source_id if is_sub else None,
    )
    await asyncio.to_thread(db.create_collection, collection)

    if is_sub and req.sync_now:
        try:
            await sync_collection(collection, db, redis, storage)
        except Exception as e:  # 首次同步失败不阻塞集合创建
            logger.warning("initial_sync_failed", coll=cid, error=str(e)[:200])
        collection = await asyncio.to_thread(db.get_collection, cid)
    return _to_response(collection)


@router.get("", response_model=list[CollectionResponse])
async def list_collections(
    domain: str | None = None,
    db: Database = Depends(get_db),
):
    collections = await asyncio.to_thread(db.list_collections, domain)
    return [_to_response(c) for c in collections]


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: str,
    db: Database = Depends(get_db),
):
    _validate_collection_id(collection_id)
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    return _to_response(c)


@router.put("/{collection_id}", response_model=CollectionResponse)
async def update_collection(
    collection_id: str,
    req: CollectionUpdateRequest,
    db: Database = Depends(get_db),
):
    _validate_collection_id(collection_id)
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    await asyncio.to_thread(
        db.update_collection, collection_id,
        req.name, req.description, req.tags, req.sync_enabled,
    )
    return _to_response(await asyncio.to_thread(db.get_collection, collection_id))


@router.post("/{collection_id}/sync")
async def trigger_sync(
    collection_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    """立即同步订阅集合(拉来源新内容入库)。"""
    _validate_collection_id(collection_id)
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    if not c.is_subscription:
        raise HTTPException(400, "非订阅集合，无可同步来源")
    try:
        return await sync_collection(c, db, redis, storage)
    except Exception as e:
        raise HTTPException(502, f"同步失败: {str(e)[:200]}")


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: str,
    db: Database = Depends(get_db),
):
    """删集合=解绑：名下 job 的 collection_id 置 NULL（保留 job），再删集合行。"""
    _validate_collection_id(collection_id)
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    await asyncio.to_thread(db.delete_collection, collection_id)


@router.get("/{collection_id}/jobs", response_model=JobListResponse)
async def list_collection_jobs(
    collection_id: str,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
):
    """集合名下的 job 列表（分页，复用 db.list_jobs 的 collection_id 过滤）。"""
    _validate_collection_id(collection_id)
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    total, jobs = await asyncio.to_thread(
        db.list_jobs, None, collection_id, limit, offset,
    )
    return JobListResponse(
        total=total,
        items=[
            JobResponse(
                job_id=j.id, content_type=j.content_type, status=j.status.value,
                created_at=j.created_at.isoformat(), title=j.title,
                progress_pct=j.progress_pct, source=j.source, domain=j.domain,
            )
            for j in jobs
        ],
    )
