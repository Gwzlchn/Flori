"""集合管理路由。"""

from __future__ import annotations

import asyncio
import secrets
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from shared.db import Database
from shared.models import Collection

from api.deps import get_db, verify_token
from api.schemas import (
    CollectionCreateRequest,
    CollectionResponse,
    CollectionUpdateRequest,
    JobListResponse,
    JobResponse,
)

router = APIRouter(
    prefix="/api/collections", tags=["collections"],
    dependencies=[Depends(verify_token)],
)


def _generate_collection_id() -> str:
    """生成集合 ID: c_{YYYYMMDD}_{6 hex chars}（与 job_id 风格一致）。"""
    d = date.today().strftime("%Y%m%d")
    return f"c_{d}_{secrets.token_hex(3)}"


def _validate_collection_id(collection_id: str) -> None:
    if ".." in collection_id or "/" in collection_id or "\x00" in collection_id:
        raise HTTPException(400, "invalid collection_id")


def _to_response(c: Collection) -> CollectionResponse:
    return CollectionResponse(
        id=c.id, name=c.name, domain=c.domain, description=c.description,
        tags=c.tags, job_count=c.job_count, created_at=c.created_at.isoformat(),
    )


@router.post("", status_code=201, response_model=CollectionResponse)
async def create_collection(
    req: CollectionCreateRequest,
    db: Database = Depends(get_db),
):
    collection = Collection(
        id=_generate_collection_id(),
        name=req.name,
        domain=req.domain,
        description=req.description or "",
        tags=req.tags,
    )
    await asyncio.to_thread(db.create_collection, collection)
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
        req.name, req.description, req.tags,
    )
    updated = await asyncio.to_thread(db.get_collection, collection_id)
    return _to_response(updated)


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
