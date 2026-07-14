"""全文检索路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from shared.db import Database
from shared.storage import StorageBackend
from api.deps import get_db, get_storage, verify_token
from api.schemas import SearchResponse, SearchResultItem
from api.services.evidence import attach_canonical_evidence
from api.wire_schemas import API_ERROR_RESPONSES

router = APIRouter(
    prefix="/api/search", tags=["search"],
    dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


@router.get("", response_model=SearchResponse)
async def search_notes(
    q: str = Query("", description="检索词;2 字 CJK 可精确子串命中,3+ 字符走 trigram"),
    collection_id: str | None = None,
    domain: str | None = None,
    content_type: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=2_147_483_647),  # int32 max,远低于 SQLite int64 溢出点;挡住超大 offset 触发的 500
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
) -> SearchResponse:
    """笔记全文检索:q 经 db 层转义防注入,空查询直接返回空结果。"""
    total, items = await asyncio.to_thread(
        db.search_notes,
        q, collection_id=collection_id, domain=domain,
        content_type=content_type, limit=limit, offset=offset,
    )
    await attach_canonical_evidence(db, storage, items)
    return SearchResponse(
        total=total,
        items=[SearchResultItem(**it) for it in items],
    )
