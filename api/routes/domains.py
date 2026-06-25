"""领域路由（领域是派生视图，非实体）。
- GET  /api/domains            领域总览(卡片网格)
- POST /api/domains            新建领域
- GET  /api/domains/{d}        领域工作台聚合(集合/最近内容/术语/主题)
- GET  /api/domains/{d}/topic-concepts   领域主题概念
- GET  /api/domains/{d}/concept-timeline 领域概念时间线
- GET  /api/domains/{d}/terms/{term}     术语详情
- GET  /api/domains/{d}/topics/{topic}   主题页(域内带该标签的内容)
"""

from __future__ import annotations

import asyncio
import json

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query

from shared.config import AppConfig
from shared.db import Database
from api.deps import get_config, get_db, validate_path_segment, verify_token
from api.schemas import DomainCreateRequest, DomainRenameRequest, GlossaryTermResponse

router = APIRouter(prefix="/api/domains", tags=["domains"], dependencies=[Depends(verify_token)])

# 知识库展示元数据持久化在 prompts/profiles/{domain}.yaml(领域无独立表,profile 即其元数据)。
_META_KEYS = ("display_name", "icon", "color", "description", "role")


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
    if not domain:
        raise HTTPException(400, "invalid domain")
    validate_path_segment(domain, "domain")
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


# 进程级串行化 rename:防撞检查与文件/DB 迁移之间本无原子性,两个并发 rename 可交错通过检查后
# 各自改文件/库(TOCTOU)。单用户工具极低频,一把进程内锁串行化即足够稳妥。
_rename_lock = asyncio.Lock()


@router.post("/{domain}/rename")
async def rename_domain_key(
    domain: str,
    req: DomainRenameRequest,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """改知识库英文标识(domain key,二期 issue1-b):领域是派生键(无独立表),散在 jobs/collections/glossary
    + notes_fts5 冗余列 + profiles/{domain}.yaml。这里:先迁 profile 文件(可回滚)→ 再事务迁移 DB 引用,
    DB 失败则回滚文件。new 须合法、不与现有领域冲突;general 不可改。"""
    validate_path_segment(domain, "domain")
    new = (req.new_domain or "").strip()
    if not new:
        raise HTTPException(400, "invalid new_domain")
    validate_path_segment(new, "new_domain")
    if new == domain:
        raise HTTPException(400, "新旧标识相同")
    if domain == "general" or new == "general":
        raise HTTPException(400, "general 是默认领域,不可改名")
    # 防撞 + 文件/DB 迁移全程持锁,避免并发 rename 在检查与执行之间交错(TOCTOU)。
    async with _rename_lock:
        # 防撞:new 不能已被使用(库里有行 或 profile 文件已存在)
        pdir = config.prompts_dir / "profiles"
        old_path = pdir / f"{domain}.yaml"
        new_path = pdir / f"{new}.yaml"
        if new_path.exists() or await asyncio.to_thread(db.domain_exists, new):
            raise HTTPException(409, f"目标标识 '{new}' 已存在")
        # 1) 迁 profile 文件(若有):old.yaml -> new.yaml + 改内部 domain 字段(记录以便回滚)
        moved_file = False
        if old_path.exists():
            data = yaml.safe_load(old_path.read_text(encoding="utf-8")) or {}
            data["domain"] = new
            pdir.mkdir(parents=True, exist_ok=True)
            new_path.write_text(
                yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8"
            )
            old_path.unlink()
            moved_file = True
        # 2) 事务迁移 DB 引用;失败则回滚文件,保证一致
        try:
            moved = await asyncio.to_thread(db.rename_domain, domain, new)
        except Exception:
            if moved_file:
                data["domain"] = domain
                old_path.write_text(
                    yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8"
                )
                new_path.unlink(missing_ok=True)
            raise
    by = await _overview_map(db, config)
    return {"old": domain, "new": new, "moved": moved, "domain": by.get(new)}


# 注:知识库展示元数据(display_name/icon/color)的修改复用已有 PUT /api/profiles/{domain}
# (api/routes/profiles.py,ProfileUpdateRequest 已含这三字段且为部分合并、保留 terminology)——
# 不在此另开 PATCH/PUT 端点,避免同一份 yaml meta 持久化逻辑两处分叉(审计去重原则)。


@router.get("/{domain}")
async def domain_workspace(
    domain: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """领域工作台：情景层(集合+最近内容) + 语义层(术语+主题)。"""
    validate_path_segment(domain, "domain")
    overview = await _overview_map(db, config)
    if domain not in overview:
        raise HTTPException(404, "domain not found")
    collections = await asyncio.to_thread(db.list_collections, domain)
    # issue 6:每集合各取最近 5 条。原先复用「全域最近 12 条」按 collection_id 分组,集合内容不在
    # 前 12 即被误显「该集合暂无最近内容」(如 finance 167 jobs、集合 79/45)。集合数通常个位数,
    # N+1 小查询可接受。loose/未归类仍用下方全域 recent_jobs。
    col_payload = []
    for c in collections:
        _, c_recent = await asyncio.to_thread(db.list_jobs, None, c.id, 5, 0, domain)
        col_payload.append({
            "id": c.id, "name": c.name, "job_count": c.job_count,
            "is_subscription": c.is_subscription,
            "source_id": c.source_id, "sync_enabled": c.sync_enabled,
            "recent": [_job_brief(j) for j in c_recent],
        })
    _, recent = await asyncio.to_thread(db.list_jobs, None, None, 12, 0, domain)
    top_terms = await asyncio.to_thread(db.domain_top_terms, domain, 30)
    topics = await asyncio.to_thread(db.domain_topics, domain)
    suggested = await asyncio.to_thread(db.list_glossary, domain, "suggested")
    return {
        "domain": domain,
        "stats": overview[domain],
        "collections": col_payload,
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
    validate_path_segment(domain, "domain")
    return await asyncio.to_thread(db.list_topic_concepts, domain)


@router.get("/{domain}/concept-timeline")
async def concept_timeline(
    domain: str,
    granularity: str = Query("month", pattern="^(day|week|month)$"),
    db: Database = Depends(get_db),
):
    """概念时间线：各概念 occurrences 经 job 创建时间分桶计数(day/week/month)。空领域返回空序列。"""
    validate_path_segment(domain, "domain")
    return await asyncio.to_thread(db.concept_timeline, domain, granularity)


@router.get("/{domain}/terms/{term}", response_model=GlossaryTermResponse)
async def term_detail(
    domain: str, term: str,
    db: Database = Depends(get_db),
):
    """术语详情：定义 + 关联 + 类型化出现处。形态与 /api/glossary/{d}/{t} 完全一致(共用 from_row)。"""
    validate_path_segment(domain, "domain")
    t = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if not t:
        raise HTTPException(404, "term not found")
    return GlossaryTermResponse.from_row(t)


@router.get("/{domain}/topics/{topic}")
async def topic_page(
    domain: str, topic: str,
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
):
    """主题页：域内带该标签(style_tags)的内容(跨集合/跨来源聚合)。"""
    validate_path_segment(domain, "domain")
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
