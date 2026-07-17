"""集合管理路由。订阅是集合的属性(无独立订阅实体/页面):
source_type/source_id 非空 = 订阅集合,自动从该来源追更新内容入本集合。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from shared.audit import audit
from shared.db import Database
from shared.ids import lineage_key
from shared.models import Collection, collection_id_for_subscription, generate_collection_id
from shared.redis_client import RedisClient
from shared.storage import StorageBackend
from shared.subscriptions.base import source_label  # 派生来源短标签(_to_response 用,模块级)

from api.deps import get_db, get_redis, get_storage, validate_path_segment, verify_token
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


def _to_response(
    c: Collection, status_counts: dict[str, int] | None = None
) -> CollectionResponse:
    sub = None
    if c.is_subscription:
        sub = CollectionSubscriptionInfo(
            source_type=c.source_type, source_id=c.source_id,
            source_label=source_label(c.source_type),   # 派生短标签,前端组合显示(name + 徽标)
            enabled=c.sync_enabled,
            last_synced_at=c.last_synced_at.isoformat() if c.last_synced_at else None,
            last_sync_status=c.last_sync_status,         # ok|error|syncing|None(从未同步)
            last_sync_error=c.last_sync_error,           # status=error 时的错误摘要
        )
    return CollectionResponse(
        id=c.id, name=c.name, domain=c.domain, description=c.description,
        tags=c.tags, job_count=c.job_count, created_at=c.created_at.isoformat(),
        subscription=sub, status_counts=status_counts,   # 仅详情端点填,列表端点为 None
    )


async def sync_collection(
    coll: Collection, db: Database, redis: RedisClient, storage: StorageBackend,
) -> dict:
    """枚举订阅集合的来源,跟已入库内容去重,并为新内容自动建 job。
    经 enumerate_source 按 source_type 分派到注册的 source-adapter(B站 UP/收藏夹/
    YouTube/RSS/本地目录…),与具体来源解耦。返回 {total, new, skipped}。仅订阅集合可调。"""
    if not coll.is_subscription:
        raise ValueError("not a subscription collection")
    # 同步状态机:开始置 syncing;成功由 mark_collection_synced 置 ok;
    # 异常置 error+存摘要后向上抛,故障隔离不掩盖错误.
    await asyncio.to_thread(db.set_sync_status, coll.id, "syncing")
    try:
        return await _sync_collection_body(coll, db, redis, storage)
    except Exception as e:
        await asyncio.to_thread(db.set_sync_status, coll.id, "error", str(e))
        raise


async def _sync_collection_body(
    coll: Collection, db: Database, redis: RedisClient, storage: StorageBackend,
) -> dict:
    from shared.subscriptions import SourceContext, enumerate_source
    from api.routes.jobs import create_job_core

    cookies = await asyncio.to_thread(db.get_credential, "bili_cookies")
    ctx = SourceContext(bili_cookies=cookies, db=db)
    source_title, items = await enumerate_source(coll.source_type, coll.source_id, ctx)
    # 来源适配器应自行去重,同步边界仍按稳定 item_id 保留首项兜底。分页重叠或
    # 上游脏数据不能在同一轮创建重复 lineage。
    unique_items = []
    seen_item_ids: set[str] = set()
    for item in items:
        if item.item_id in seen_item_ids:
            continue
        seen_item_ids.add(item.item_id)
        unique_items.append(item)
    items = unique_items

    # 首次同步拿到 source_title 后回填集合名:仅当当前名为占位(空/等于
    # source_id/等于集合 id)时改,避免覆盖用户手填名。回填后用于响应与后续展示。
    if source_title:
        desired = source_title  # 存纯真实名;来源标签在响应里派生(source_label),不拼进 name
        if _is_placeholder_name(coll.name, coll) and coll.name != desired:
            await asyncio.to_thread(db.update_collection, coll.id, name=desired)
            coll.name = desired

    ingested = await asyncio.to_thread(db.ingested_item_ids, coll.id)
    created = 0
    reused = 0
    failed = 0
    _, collection_jobs = await asyncio.to_thread(
        db.list_jobs, collection_id=coll.id, limit=100000,
    )
    by_item_id = {
        job.meta.get("source_item_id"): job
        for job in collection_jobs
        if isinstance(job.meta, dict) and job.meta.get("source_item_id")
    }
    visible_item_ids = {item.item_id for item in items}
    for item_id, existing in by_item_id.items():
        present = item_id in visible_item_ids
        if existing.meta.get("source_present") != present:
            meta = dict(existing.meta)
            meta["source_present"] = present
            await asyncio.to_thread(db.update_job, existing.id, meta=meta)
            existing.meta = meta
    # book(章序):全部章 defer 建好(不触发调度),由 scheduler 在前章终态时按序 submit;
    # 章 job 强制 smart_note=True(article 链默认 off,书章要笔记)。
    is_book = coll.source_type == "book_toc"
    for position, it in enumerate(items):
        existing = by_item_id.get(it.item_id)
        if it.item_id in ingested:
            if existing and (
                existing.meta.get("source_position") != position
                or existing.meta.get("source_present") is not True
            ):
                meta = dict(existing.meta)
                meta["source_position"] = position
                meta["source_present"] = True
                await asyncio.to_thread(db.update_job, existing.id, meta=meta)
            continue
        try:
            current = await asyncio.to_thread(
                db.get_current_job_by_lineage,
                lineage_key(it.url, it.content_type),
            )
            if current is not None:
                if current.collection_id not in (None, coll.id):
                    raise ValueError("lineage already belongs to another collection")
                await asyncio.to_thread(
                    db.move_job_to_collection,
                    current.id,
                    coll.id,
                    source_item_id=it.item_id,
                    source_position=position,
                )
                await asyncio.to_thread(db.mark_ingested, coll.id, it.item_id)
                created += 1
                reused += 1
                continue
            await create_job_core(
                db, redis, storage,
                url=it.url, content_type=it.content_type, domain=coll.domain,
                collection_id=coll.id, title=it.title or None,
                item_id=it.item_id, actor="subscription",
                source_position=position,
                smart_note=True if is_book else None,
                document_kind=it.document_kind,
                defer_submit=is_book,
            )
            await asyncio.to_thread(db.mark_ingested, coll.id, it.item_id)
        except Exception as e:
            # 故障隔离:单条建 job 失败(坏 url / I/O 抖动)不阻断整轮同步;不 mark_ingested,
            # 下轮自动重试。否则一条坏数据会卡住其后所有条目本轮入库(违反"单任务失败不影响其他")。
            logger.warning("collection_sync_item_failed", coll=coll.id,
                           item_id=it.item_id, url=it.url, error=str(e)[:200])
            failed += 1
            continue
        created += 1
        await asyncio.sleep(0.2)  # 轻微间隔,别瞬时灌爆队列/触发风控
    if is_book and created:
        # 兜底起链:无章在跑时投最早待投章;有章在跑返回 None 不动.
        from shared.book_chain import next_chapter_job
        nxt = await next_chapter_job(db, redis, coll.id)
        if nxt:
            job = await asyncio.to_thread(db.get_job, nxt)
            await redis.append_lifecycle_event("job_command", {
                "action": "new_job", "job_id": nxt, "pipeline": job.pipeline if job else "document",
            })
            logger.info("book_chain_kickoff", coll=coll.id, job_id=nxt)
    await asyncio.to_thread(db.mark_collection_synced, coll.id, datetime.now(timezone.utc))
    logger.info("collection_synced", coll=coll.id, total=len(items),
                new=created, reused=reused, failed=failed)
    return {
        "total": len(items),
        "new": created,
        "reused": reused,
        "skipped": len(items) - created - failed,
        "failed": failed,
    }


def _is_placeholder_name(name: str | None, coll: Collection) -> bool:
    """判断集合名是否为可被首次同步覆盖的占位名(空 / 等于来源 id / 等于集合 id)。
    用户显式填的真实名不在此列,不会被回填覆盖。"""
    n = (name or "").strip()
    return (not n) or n == coll.source_id or n == coll.id


@router.post("", status_code=201, response_model=CollectionResponse)
async def create_collection(
    req: CollectionCreateRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    if bool(req.source_type) != bool(req.source_id):
        raise HTTPException(422, "source_type and source_id must be provided together")
    is_sub = bool(req.source_type and req.source_id)
    if is_sub:
        from shared.subscriptions import SOURCE_ADAPTERS, normalize_source_id
        if req.source_type not in SOURCE_ADAPTERS:
            raise HTTPException(422, f"unsupported_source_type: {req.source_type}")
        try:
            source_id = normalize_source_id(req.source_type, req.source_id)
        except ValueError as exc:
            raise HTTPException(422, f"invalid_source_id: {exc}") from exc
        # 订阅集合:domain 必须显式且非 general(否则术语沉错领域);来源全局唯一。
        if not req.domain or req.domain == "general":
            raise HTTPException(400, "订阅集合必须选择真实领域(不能为 general)")
        if await asyncio.to_thread(db.find_collection_by_source, req.source_type, source_id):
            raise HTTPException(400, "该来源已订阅")
        cid = collection_id_for_subscription(req.source_type, source_id)
    else:
        # 手动集合必须有名(订阅集合可留空,首次同步自动命名)。
        if not (req.name or "").strip():
            raise HTTPException(400, "集合名不能为空")
        cid = generate_collection_id()

    # 订阅集合名留空 = 要求自动命名:先以 source_id 占位(NOT NULL)。首次同步拿到
    # source_title 后由 sync_collection 回填,占位判定见 _is_placeholder_name。
    name = (req.name or "").strip()
    if is_sub and not name:
        name = source_id

    collection = Collection(
        id=cid, name=name, domain=req.domain,
        description=req.description or "", tags=req.tags,
        source_type=req.source_type if is_sub else None,
        source_id=source_id if is_sub else None,
    )
    await asyncio.to_thread(db.create_collection, collection)

    if is_sub and req.sync_now:
        try:
            await sync_collection(collection, db, redis, storage)
        except Exception as e:  # 首次同步失败不阻塞集合创建
            logger.warning("initial_sync_failed", coll=cid, error=str(e)[:200])
        collection = await asyncio.to_thread(db.get_collection, cid)
    audit("collection", cid, "create", actor="api",
          detail={"name": name, "domain": req.domain, "subscription": is_sub})
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
    validate_path_segment(collection_id, "collection_id")
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    # 详情页带 status_counts(本集合各状态 job 计数):充实"集合信息"卡 + 驱动"重试本集合失败"
    counts = await asyncio.to_thread(db.count_jobs_by_status, collection_id)
    status_counts = dict(counts)
    for k in ("done", "processing", "failed", "pending"):
        status_counts.setdefault(k, 0)
    return _to_response(c, status_counts)


@router.put("/{collection_id}", response_model=CollectionResponse)
async def update_collection(
    collection_id: str,
    req: CollectionUpdateRequest,
    db: Database = Depends(get_db),
):
    validate_path_segment(collection_id, "collection_id")
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    # sync_enabled 仅订阅集合有意义;手动集合传该字段是无效写入。
    if req.sync_enabled is not None and not c.is_subscription:
        raise HTTPException(400, "非订阅集合没有自动追更开关")
    await asyncio.to_thread(
        db.update_collection, collection_id,
        req.name, req.description, req.tags, req.sync_enabled,
    )
    audit("collection", collection_id, "update", actor="api")
    return _to_response(await asyncio.to_thread(db.get_collection, collection_id))


@router.post("/{collection_id}/sync")
async def trigger_sync(
    collection_id: str,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    """立即同步订阅集合(拉来源新内容入库)。"""
    validate_path_segment(collection_id, "collection_id")
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
    purge: bool = Query(False),   # false=仅解绑(保留 job/笔记);true=连名下 job 一起删(前端需二次确认)
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
):
    """删集合两模式:默认解绑(名下 job 的 collection_id 置 NULL、保留内容);
    purge=true 连名下 job 一起删(走与单 job 删同款的精准清理:队列/编排/产物,再 DB 批量级联)。
    两种都清该集合的 ingested_items(便于重订阅重新入库)。"""
    validate_path_segment(collection_id, "collection_id")
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    purged = 0
    if purge:
        # 名下每个 job 先做非 DB 清理:队列残留 + 编排 hash + active + 在途重试 + 产物。
        # 再由 db.delete_collection(purge=True) 单事务批量删 DB(jobs+FTS+ai_usage+occurrences+ingested+集合)。
        _, jobs = await asyncio.to_thread(db.list_jobs, collection_id=collection_id, limit=100000)
        for j in jobs:
            await redis.remove_job_tasks(j.id)
            await redis.cleanup_job(j.id)
            await redis.remove_active_job(j.id)
            await redis.append_lifecycle_event("job_command", {"action": "delete", "job_id": j.id})
            await storage.delete(j.id)
            audit("job", j.id, "delete", actor="collection_purge",
                  detail={"collection_id": collection_id})
        purged = len(jobs)
    await asyncio.to_thread(db.delete_collection, collection_id, purge)
    audit("collection", collection_id, "delete", actor="api",
          detail={"purge": purge, "jobs_purged": purged})


@router.get("/{collection_id}/jobs", response_model=JobListResponse)
async def list_collection_jobs(
    collection_id: str,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0, le=2_147_483_647),  # int32 max,远低于 SQLite int64 溢出点;挡住超大 offset.
    db: Database = Depends(get_db),
):
    """集合名下的 job 列表(分页,复用 db.list_jobs 的 collection_id 过滤)。"""
    validate_path_segment(collection_id, "collection_id")
    c = await asyncio.to_thread(db.get_collection, collection_id)
    if not c:
        raise HTTPException(404, "collection not found")
    total, jobs = await asyncio.to_thread(
        db.list_jobs, None, collection_id, limit, offset,
        source_order=c.is_subscription,
    )
    return JobListResponse(
        total=total,
        items=[
            JobResponse(
                job_id=j.id, content_type=j.content_type,
                document_kind=j.document_kind or None, pipeline=j.pipeline,
                status=j.status.value,
                created_at=j.created_at.isoformat(), title=j.title,
                progress_pct=j.progress_pct, source=j.source, domain=j.domain,
                collection_id=j.collection_id,
            )
            for j in jobs
        ],
    )
