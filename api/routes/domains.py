"""领域路由（领域是派生视图，非实体）。
- GET /api/domains            领域总览(卡片网格)
- GET /api/domains/{d}        领域工作台聚合(集合/最近内容/术语/主题)
- GET /api/domains/{d}/terms/{term}    术语详情
- GET /api/domains/{d}/topics/{topic}  主题页(域内带该标签的内容)
"""

from __future__ import annotations

import asyncio
import json

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query

from shared.config import AppConfig
from shared.db import Database
from api.deps import get_config, get_db, verify_token
from api.schemas import DomainCreateRequest

router = APIRouter(prefix="/api/domains", tags=["domains"], dependencies=[Depends(verify_token)])

# 知识库展示元数据持久化在 prompts/profiles/{domain}.yaml(领域无独立表,profile 即其元数据)。
_META_KEYS = ("display_name", "icon", "color", "description", "role")


def _validate(domain: str) -> None:
    if not domain or ".." in domain or "/" in domain or "\x00" in domain:
        raise HTTPException(400, "invalid domain")


def _profile_meta(config: AppConfig) -> dict[str, dict]:
    """读所有 profiles/*.yaml 的展示元数据(icon/color/display_name/description/role),按 domain。"""
    pdir = config.prompts_dir / "profiles"
    out: dict[str, dict] = {}
    if pdir.exists():
        for f in sorted(pdir.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
            out[f.stem] = {k: data[k] for k in _META_KEYS if data.get(k) is not None}
    return out


async def _overview_map(db: Database, config: AppConfig) -> dict[str, dict]:
    """领域总览 {domain: stats+meta}:DB 派生(jobs∪collections∪glossary) ∪ 仅有 profile 的领域(零计数),
    并各自合并 profile 展示元数据。这样新建的空知识库(只有 profile)也会出现。"""
    rows = await asyncio.to_thread(db.list_domains)
    by = {d["domain"]: d for d in rows}
    meta = _profile_meta(config)
    for dom in meta:
        by.setdefault(dom, {
            "domain": dom, "collection_count": 0, "job_count": 0,
            "concept_count": 0, "subscription_count": 0, "last_active_at": None,
        })
    for dom, d in by.items():
        d.update(meta.get(dom, {}))
    return by


def _job_brief(j) -> dict:
    return {
        "job_id": j.id, "content_type": j.content_type, "status": j.status.value,
        "created_at": j.created_at.isoformat(), "title": j.title,
        "progress_pct": j.progress_pct, "source": j.source, "domain": j.domain,
        "collection_id": j.collection_id,
    }


@router.get("")
async def list_domains(
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    by = await _overview_map(db, config)
    return {"domains": [by[k] for k in sorted(by)]}


@router.post("", status_code=201)
async def create_domain(
    req: DomainCreateRequest,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """新建知识库:把展示元数据写进 profiles/{domain}.yaml(领域随即出现在总览,即使暂无内容)。"""
    domain = (req.domain or "").strip()
    _validate(domain)
    if domain == "general":
        raise HTTPException(400, "general 是默认领域，无需新建")
    pdir = config.prompts_dir / "profiles"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{domain}.yaml"
    if path.exists():
        raise HTTPException(409, "domain already exists")
    data: dict = {"domain": domain}
    for k in _META_KEYS:
        v = getattr(req, k, None)
        if v is not None:
            data[k] = v
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8"
    )
    by = await _overview_map(db, config)
    return by[domain]


@router.get("/{domain}")
async def domain_workspace(
    domain: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """领域工作台：情景层(集合+最近内容) + 语义层(术语+主题)。"""
    _validate(domain)
    overview = await _overview_map(db, config)
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


@router.get("/{domain}/concept-timeline")
async def concept_timeline(
    domain: str,
    granularity: str = Query("month", pattern="^(day|week|month)$"),
    db: Database = Depends(get_db),
):
    """概念时间线：各概念 occurrences 经 job 创建时间分桶计数(day/week/month)。空领域返回空序列。"""
    _validate(domain)
    return await asyncio.to_thread(db.concept_timeline, domain, granularity)


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
