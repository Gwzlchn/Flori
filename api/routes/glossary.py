"""术语表路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from shared.config import AppConfig
from shared.db import ConceptConflictError, ConceptNotFoundError, Database
from shared.errors import AIProviderError
from shared.storage import StorageBackend
from api.deps import (
    get_config,
    get_db,
    get_storage,
    validate_path_segment,
    verify_token,
)
from api.routes.profiles import sync_term_to_profile
from api.schemas import (
    ConceptCasRequest,
    ConceptLockResponse,
    ConceptResynthesizeResponse,
    ConceptTermDetailResponse,
    GlossaryTermRequest,
    GlossaryTermResponse,
)
from api.services import concepts as concept_service
from api.wire_schemas import API_ERROR_RESPONSES, GlossaryBatchResponse


class TopicToggleRequest(BaseModel):
    is_topic: bool


class MergeRequest(BaseModel):
    target: str   # 合并目标(dst)主名;路径里的 term 是被并入方(src)


class WatchRequest(BaseModel):
    watched: bool


class BatchRequest(BaseModel):
    action: str                       # 'accept' | 'reject'
    items: list[dict]                 # [{domain, term}]


router = APIRouter(
    prefix="/api/glossary", tags=["glossary"],
    dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


def _to_response(row: dict) -> GlossaryTermResponse:
    """统一术语序列化(含 created_at/updated_at,ISO str|None)。与 domains 端点共用同一形态。"""
    return GlossaryTermResponse.from_row(row)


def _related_payload(related):
    if related is None:
        return None
    return [
        item.model_dump() if hasattr(item, "model_dump") else item
        for item in related
    ]


def _raise_concept_error(exc: Exception) -> None:
    if isinstance(exc, ConceptNotFoundError):
        raise HTTPException(404, "term not found") from exc
    if isinstance(exc, ConceptConflictError):
        raise HTTPException(409, str(exc)) from exc
    raise exc


def enrich_occurrence_titles(db: Database, row: dict) -> dict:
    """概念详情的出现处补 job 标题(title 可能缺:job 已删/未同步,前端回退显示 job_id)。
    只在单条详情端点用,列表端点不 enrich(条数 × 出现数的标题查询没必要)。"""
    occs = row.get("occurrences") or []
    titles = db.get_job_titles([o.get("job_id") for o in occs if isinstance(o, dict)])
    row["occurrences"] = [
        {**o, "title": titles.get(o.get("job_id"))} if isinstance(o, dict) else o
        for o in occs
    ]
    return row


@router.get("", response_model=list[GlossaryTermResponse])
async def list_terms(
    domain: str | None = None,
    status: str | None = None,
    q: str | None = None,
    db: Database = Depends(get_db),
):
    """列术语,可按 domain / status(suggested 待审 / accepted 已采纳)过滤;
    q 检索 term/zh_name/aliases 子串(中英说法都能搜到同一实体)。"""
    rows = await asyncio.to_thread(db.list_glossary, domain, status, q)
    return [_to_response(r) for r in rows]


@router.post("", response_model=GlossaryTermResponse, status_code=201)
async def create_term(
    req: GlossaryTermRequest,
    domain: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """手动新增术语:直接落 status='accepted',并同步进 Profile.terminology。"""
    validate_path_segment(domain, "domain")
    if not domain.strip():
        # domain 为空会写出空文件名 profile(.yaml)到不可达领域,与 domains 端点一致挡掉。
        raise HTTPException(400, "invalid domain")
    term = req.term.strip()
    if not term:
        raise HTTPException(400, "term required")
    definition = req.definition or ""
    try:
        await asyncio.to_thread(
            db.upsert_glossary_term,
            domain,
            term,
            definition,
            _related_payload(req.related),
            "accepted",
            create_only=True,
        )
    except ConceptConflictError as exc:
        _raise_concept_error(exc)
    await asyncio.to_thread(sync_term_to_profile, config, domain, term, definition)
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(row)


@router.get("/{domain}/{term}", response_model=ConceptTermDetailResponse)
async def get_term(
    domain: str,
    term: str,
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
):
    """术语详情，含定义历史与现场重验的来源佐证。"""
    validate_path_segment(domain, "domain")
    try:
        detail = await concept_service.project_concept_detail(
            db, storage, domain, term,
        )
    except (ConceptNotFoundError, ConceptConflictError) as exc:
        _raise_concept_error(exc)
    if detail is None:
        raise HTTPException(404, "term not found")
    return ConceptTermDetailResponse.model_validate(detail)


@router.post("/{domain}/{term}/merge", response_model=GlossaryTermResponse)
async def merge_term(
    domain: str,
    term: str,
    req: MergeRequest,
    db: Database = Depends(get_db),
):
    """把 {term}(src)并入 body.target(dst)实体:occurrence 并集、变体入 aliases、
    定义取更长者,src 行删除。src==dst 或任一不存在时返回 400/404。"""
    validate_path_segment(domain, "domain")
    target = (req.target or "").strip()
    if not target:
        raise HTTPException(400, "target required")
    if target == term:
        raise HTTPException(400, "cannot merge a term into itself")
    try:
        merged = await asyncio.to_thread(db.merge_glossary_terms, domain, term, target)
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))
    return _to_response(merged)


@router.put("/{domain}/{term}", response_model=GlossaryTermResponse)
async def update_term(
    domain: str,
    term: str,
    req: GlossaryTermRequest,
    db: Database = Depends(get_db),
):
    """改 definition / related；定义变更用 current version + lock revision CAS。"""
    validate_path_segment(domain, "domain")
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        raise HTTPException(404, "term not found")
    try:
        await asyncio.to_thread(
            db.update_glossary_definition_cas,
            domain=domain,
            term=term,
            definition=req.definition,
            related=_related_payload(req.related),
            expected_current_version_id=req.expected_current_version_id,
            expected_lock_revision=req.expected_lock_revision,
            actor="api:manual_edit",
        )
    except (ConceptNotFoundError, ConceptConflictError) as exc:
        _raise_concept_error(exc)
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


async def _set_definition_lock(
    *,
    domain: str,
    term: str,
    req: ConceptCasRequest,
    locked: bool,
    db: Database,
) -> ConceptLockResponse:
    validate_path_segment(domain, "domain")
    try:
        result = await asyncio.to_thread(
            db.set_concept_definition_lock,
            domain=domain,
            term=term,
            locked=locked,
            expected_current_version_id=req.expected_current_version_id,
            expected_lock_revision=req.expected_lock_revision,
        )
    except (ConceptNotFoundError, ConceptConflictError) as exc:
        _raise_concept_error(exc)
    return ConceptLockResponse.model_validate(result)


@router.post("/{domain}/{term}/lock", response_model=ConceptLockResponse)
async def lock_definition(
    domain: str,
    term: str,
    req: ConceptCasRequest,
    db: Database = Depends(get_db),
):
    """以 CAS 锁定定义，后台综合和人工改写都不得跨过。"""
    return await _set_definition_lock(
        domain=domain, term=term, req=req, locked=True, db=db,
    )


@router.post("/{domain}/{term}/unlock", response_model=ConceptLockResponse)
async def unlock_definition(
    domain: str,
    term: str,
    req: ConceptCasRequest,
    db: Database = Depends(get_db),
):
    """以 CAS 解锁定义，返回新 lock revision。"""
    return await _set_definition_lock(
        domain=domain, term=term, req=req, locked=False, db=db,
    )


@router.post(
    "/{domain}/{term}/resynthesize",
    response_model=ConceptResynthesizeResponse,
)
async def resynthesize_definition(
    domain: str,
    term: str,
    req: ConceptCasRequest,
    db: Database = Depends(get_db),
    storage: StorageBackend = Depends(get_storage),
    config: AppConfig = Depends(get_config),
):
    """只用通过可靠性与 canonical evidence 校验的文本重综合。"""
    validate_path_segment(domain, "domain")
    try:
        result = await concept_service.maybe_resynthesize_concept(
            db,
            storage,
            config,
            domain,
            term,
            expected_current_version_id=req.expected_current_version_id,
            expected_lock_revision=req.expected_lock_revision,
            actor="api:manual_resynthesis",
            strategy="manual_resynthesis",
        )
    except (ConceptNotFoundError, ConceptConflictError) as exc:
        _raise_concept_error(exc)
    except (
        concept_service.ConceptSynthesisConfigError,
        concept_service.ConceptSynthesisParseError,
        AIProviderError,
    ) as exc:
        raise HTTPException(502, "concept synthesis failed") from exc
    payload = dict(result)
    for key in ("current", "version"):
        if isinstance(payload.get(key), dict):
            payload[key] = concept_service.concept_definition_projection(payload[key])
    return ConceptResynthesizeResponse.model_validate(payload)


@router.post("/{domain}/{term}/accept", response_model=GlossaryTermResponse)
async def accept_term(
    domain: str,
    term: str,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """采纳候选术语:status -> 'accepted' 并同步进 Profile.terminology,让 AI 步骤可用。"""
    validate_path_segment(domain, "domain")
    row = await asyncio.to_thread(db.get_glossary_term, domain, term)
    if row is None:
        raise HTTPException(404, "term not found")
    await asyncio.to_thread(db.accept_glossary_term, domain, term)
    await asyncio.to_thread(
        sync_term_to_profile, config, domain, term, row["definition"] or "",
    )
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


@router.post("/{domain}/{term}/reject", response_model=GlossaryTermResponse)
async def reject_term(domain: str, term: str, db: Database = Depends(get_db)):
    """驳回概念:将 status 设为 rejected。行保留,采集链 resolve 命中即跳过。
    同名/变体不再被重复建议;列表/图谱/雷达/term_map 默认排除。term 不存在则 404。"""
    validate_path_segment(domain, "domain")
    ok = await asyncio.to_thread(db.reject_glossary_term, domain, term)
    if not ok:
        raise HTTPException(404, "term not found")
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


@router.post("/{domain}/{term}/watch", response_model=GlossaryTermResponse)
async def watch_term(
    domain: str,
    term: str,
    req: WatchRequest,
    db: Database = Depends(get_db),
):
    """关注/取关概念(P3,单用户):watched 概念在雷达页「我关注的概念」区置顶展示近窗动静。
    term 不存在 -> 404。"""
    validate_path_segment(domain, "domain")
    ok = await asyncio.to_thread(db.set_glossary_watched, domain, term, req.watched)
    if not ok:
        raise HTTPException(404, "term not found")
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


@router.post("/batch", response_model=GlossaryBatchResponse)
async def batch_terms(
    req: BatchRequest,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """批量采纳/驳回(P3,待审列表「全部采纳」/多选):action ∈ accept|reject,
    items=[{domain, term}]。accept 同步进 Profile.terminology(与单条 accept 一致);
    不存在的条目计入 skipped,不整批失败。返回 {updated, skipped}。"""
    if req.action not in ("accept", "reject"):
        raise HTTPException(400, "action must be 'accept' or 'reject'")
    updated = skipped = 0
    for it in req.items:
        domain = (it.get("domain") or "").strip()
        term = (it.get("term") or "").strip()
        if not domain or not term:
            skipped += 1
            continue
        validate_path_segment(domain, "domain")
        row = await asyncio.to_thread(db.get_glossary_term, domain, term)
        if row is None:
            skipped += 1
            continue
        if req.action == "accept":
            await asyncio.to_thread(db.accept_glossary_term, domain, term)
            await asyncio.to_thread(
                sync_term_to_profile, config, domain, term, row["definition"] or "",
            )
        else:
            await asyncio.to_thread(db.reject_glossary_term, domain, term)
        updated += 1
    return {"updated": updated, "skipped": skipped}


@router.post("/{domain}/{term}/topic", response_model=GlossaryTermResponse)
async def set_topic(
    domain: str,
    term: str,
    req: TopicToggleRequest,
    db: Database = Depends(get_db),
):
    """置该词是否为主题概念(is_topic)。term 不存在 -> 404。返回更新后的术语。"""
    validate_path_segment(domain, "domain")
    ok = await asyncio.to_thread(db.set_glossary_topic, domain, term, req.is_topic)
    if not ok:
        raise HTTPException(404, "term not found")
    updated = await asyncio.to_thread(db.get_glossary_term, domain, term)
    return _to_response(updated)


@router.delete("/{domain}/{term}", status_code=204)
async def delete_term(domain: str, term: str, db: Database = Depends(get_db)):
    """删一条术语(不动 Profile,避免误删手工维护的条目)。"""
    validate_path_segment(domain, "domain")
    await asyncio.to_thread(db.delete_glossary_term, domain, term)
