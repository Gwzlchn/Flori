"""术语表路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from shared.config import AppConfig
from shared.db import Database
from api.deps import get_config, get_db, verify_token
from api.routes.profiles import sync_term_to_profile
from api.schemas import GlossaryTermRequest, GlossaryTermResponse

router = APIRouter(
    prefix="/api/glossary", tags=["glossary"],
    dependencies=[Depends(verify_token)],
)


def _validate_seg(value: str, label: str) -> None:
    if ".." in value or "/" in value or "\x00" in value:
        raise HTTPException(400, f"invalid {label}")


def _to_response(row: dict) -> GlossaryTermResponse:
    """db 返回 dict（created_at 为 datetime|None）映射为响应模型（created_at 为 ISO str）。"""
    created = row.get("created_at")
    return GlossaryTermResponse(
        domain=row["domain"],
        term=row["term"],
        definition=row.get("definition") or "",
        sources=row.get("sources") or [],
        related=row.get("related") or [],
        status=row.get("status") or "accepted",
        source_type=row.get("source_type") or "manual",
        created_at=created.isoformat() if created is not None else "",
    )


@router.get("", response_model=list[GlossaryTermResponse])
async def list_terms(
    domain: str | None = None,
    status: str | None = None,
    db: Database = Depends(get_db),
):
    """列术语，可按 domain / status（suggested 待审 / accepted 已采纳）过滤。"""
    rows = await asyncio.to_thread(db.list_glossary, domain, status)
    return [_to_response(r) for r in rows]


@router.post("", response_model=GlossaryTermResponse, status_code=201)
async def create_term(
    req: GlossaryTermRequest,
    domain: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """手动新增术语：直接落 status='accepted'，并同步进 Profile.terminology。"""
    _validate_seg(domain, "domain")
    term = req.term.strip()
    if not term:
        raise HTTPException(400, "term required")
    definition = req.definition or ""
    await asyncio.to_thread(
        db.upsert_glossary_term, domain, term, definition,
        req.related, "accepted", "manual",
    )
    await asyncio.to_thread(sync_term_to_profile, config, domain, term, definition)
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(row)


@router.get("/{domain}/{term}", response_model=GlossaryTermResponse)
async def get_term(domain: str, term: str, db: Database = Depends(get_db)):
    """术语详情（含 sources 关联的 job 列表）。"""
    _validate_seg(domain, "domain")
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        raise HTTPException(404, "term not found")
    return _to_response(row)


@router.put("/{domain}/{term}", response_model=GlossaryTermResponse)
async def update_term(
    domain: str,
    term: str,
    req: GlossaryTermRequest,
    db: Database = Depends(get_db),
):
    """改 definition / related（不动 status / sources）。"""
    _validate_seg(domain, "domain")
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        raise HTTPException(404, "term not found")
    definition = req.definition if req.definition is not None else row["definition"]
    related = req.related if req.related is not None else row["related"]
    await asyncio.to_thread(
        db.upsert_glossary_term, domain, term, definition,
        related, row["status"], row["source_type"],
    )
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


@router.post("/{domain}/{term}/accept", response_model=GlossaryTermResponse)
async def accept_term(
    domain: str,
    term: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """采纳候选术语：status -> 'accepted' 并同步进 Profile.terminology，让 AI 步骤可用。"""
    _validate_seg(domain, "domain")
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        raise HTTPException(404, "term not found")
    await asyncio.to_thread(db.accept_glossary_term, domain, term)
    await asyncio.to_thread(
        sync_term_to_profile, config, domain, term, row["definition"] or "",
    )
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


@router.delete("/{domain}/{term}", status_code=204)
async def delete_term(domain: str, term: str, db: Database = Depends(get_db)):
    """删一条术语（不动 Profile，避免误删手工维护的条目）。"""
    _validate_seg(domain, "domain")
    await asyncio.to_thread(db.delete_glossary_term, domain, term)
