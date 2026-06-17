"""领域路由（领域是派生视图，非实体）。
- GET /api/domains            领域总览(卡片网格)
- GET /api/domains/{d}        领域工作台聚合(集合/最近内容/术语/主题)
- GET /api/domains/{d}/terms/{term}    术语详情
- GET /api/domains/{d}/topics/{topic}  主题页(域内带该标签的内容)
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query

from shared.db import Database
from api.deps import get_db, verify_token

router = APIRouter(prefix="/api/domains", tags=["domains"], dependencies=[Depends(verify_token)])


def _validate(domain: str) -> None:
    if not domain or ".." in domain or "/" in domain or "\x00" in domain:
        raise HTTPException(400, "invalid domain")


def _job_brief(j) -> dict:
    return {
        "job_id": j.id, "content_type": j.content_type, "status": j.status.value,
        "created_at": j.created_at.isoformat(), "title": j.title,
        "progress_pct": j.progress_pct, "source": j.source, "domain": j.domain,
        "collection_id": j.collection_id,
    }


@router.get("")
async def list_domains(db: Database = Depends(get_db)):
    return {"domains": await asyncio.to_thread(db.list_domains)}


@router.get("/{domain}")
async def domain_workspace(
    domain: str,
    db: Database = Depends(get_db),
):
    """领域工作台：情景层(集合+最近内容) + 语义层(术语+主题)。"""
    _validate(domain)
    overview = {d["domain"]: d for d in await asyncio.to_thread(db.list_domains)}
    if domain not in overview:
        raise HTTPException(404, "domain not found")
    collections = await asyncio.to_thread(db.list_collections, domain)
    _, recent = await asyncio.to_thread(db.list_jobs, None, None, 12, 0, domain)
    top_terms = await asyncio.to_thread(db.domain_top_terms, domain, 30)
    topics = await asyncio.to_thread(db.domain_topics, domain)
    suggested = await asyncio.to_thread(db.list_glossary, domain, "suggested")
    return {
        "domain": domain,
        "stats": overview[domain],
        "collections": [
            {
                "id": c.id, "name": c.name, "job_count": c.job_count,
                "is_subscription": c.is_subscription,
                "source_id": c.source_id, "sync_enabled": c.sync_enabled,
            }
            for c in collections
        ],
        "recent_jobs": [_job_brief(j) for j in recent],
        "top_concepts": top_terms,
        "topics": topics,
        "suggested_count": len(suggested),
    }


@router.get("/{domain}/topic-concepts")
async def topic_concepts(
    domain: str,
    db: Database = Depends(get_db),
):
    """域内被标为主题的概念列表（is_topic=1），按出现数降序；空则 []。"""
    _validate(domain)
    return await asyncio.to_thread(db.list_topic_concepts, domain)


@router.get("/{domain}/terms/{term}")
async def term_detail(
    domain: str, term: str,
    db: Database = Depends(get_db),
):
    """术语详情：定义 + 关联 + 出现处(sources，T2 升级为 occurrences)。"""
    _validate(domain)
    t = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if not t:
        raise HTTPException(404, "term not found")
    return t


@router.get("/{domain}/topics/{topic}")
async def topic_page(
    domain: str, topic: str,
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
):
    """主题页：域内带该标签(style_tags)的内容(跨集合/跨来源聚合)。"""
    _validate(domain)
    _, jobs = await asyncio.to_thread(db.list_jobs, None, None, 500, 0, domain)
    matched = []
    for j in jobs:
        try:
            tags = j.style_tags if isinstance(j.style_tags, list) else json.loads(j.style_tags or "[]")
        except (ValueError, TypeError):
            tags = []
        if topic in (tags or []):
            matched.append(_job_brief(j))
        if len(matched) >= limit:
            break
    return {"domain": domain, "topic": topic, "jobs": matched, "total": len(matched)}
