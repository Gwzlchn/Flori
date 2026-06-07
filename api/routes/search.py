"""全文检索路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from shared.db import Database
from api.deps import get_db, verify_token
from api.schemas import SearchResponse, SearchResultItem

router = APIRouter(
    prefix="/api/search", tags=["search"],
    dependencies=[Depends(verify_token)],
)


@router.get("", response_model=SearchResponse)
async def search_notes(
    q: str = Query("", description="检索词；trigram 至少 3 字符才命中"),
    collection_id: str | None = None,
    domain: str | None = None,
    content_type: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
) -> SearchResponse:
    """笔记全文检索：q 经 db 层转义防注入，空查询直接返回空结果。"""
    total, items = await asyncio.to_thread(
        db.search_notes,
        q, collection_id=collection_id, domain=domain,
        content_type=content_type, limit=limit, offset=offset,
    )
    return SearchResponse(
        total=total,
        items=[SearchResultItem(**it) for it in items],
    )
