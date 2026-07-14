"""学习闭环路由:卡片、到期队列和复习评分。"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from shared.db import Database
from shared.config import AppConfig
from shared.study import (
    MAX_SQLITE_INTEGER,
    StudyConflictError,
    StudyGrade,
    StudyNotFoundError,
)
from shared.study_suggestions import (
    MAX_BATCH_ITEMS,
    MAX_GENERATED_CARDS,
    StudySuggestionConflictError,
    StudySuggestionNotFoundError,
    resolve_study_suggestion_prompt,
)
from api.deps import get_config, get_db, validate_path_segment, verify_token
from api.wire_schemas import API_ERROR_RESPONSES


CardType = Literal["basic", "cloze", "qa", "quiz_single", "quiz_multi"]
CardStatus = Literal["suggested", "active", "suspended", "rejected"]
SuggestionStatus = Literal["suggested", "accepted", "rejected"]
SuggestionAction = Literal["edit", "accept", "reject"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StudyReviewState(BaseModel):
    due_at: str
    interval_days: float
    ease: float
    repetitions: int
    lapses: int
    last_grade: str | None = None
    last_reviewed_at: str | None = None
    updated_at: str


class StudyCardResponse(BaseModel):
    card_id: str
    domain: str
    job_id: str | None = None
    concept_term: str | None = None
    card_type: str
    front: str
    back: str
    explanation: str = ""
    evidence: Any = Field(default_factory=list)
    status: str
    source: str
    revision: int
    created_at: str
    updated_at: str
    review: StudyReviewState | None = None


class StudyCardListResponse(BaseModel):
    total: int
    items: list[StudyCardResponse]


class StudyCardCreate(BaseModel):
    domain: str = Field("general", min_length=1)
    job_id: str | None = None
    concept_term: str | None = None
    card_type: CardType = "basic"
    front: str = Field(..., min_length=1, max_length=20_000)
    back: str = Field(..., min_length=1, max_length=100_000)
    explanation: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    status: Literal["active", "suspended"] = "active"
    source: Literal["manual"] = "manual"

    @field_validator("domain", "front", "back", "source")
    @classmethod
    def require_nonempty_after_strip(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("explanation")
    @classmethod
    def strip_explanation(cls, value: str) -> str:
        return value.strip()


class StudyCardStatusRequest(BaseModel):
    status: CardStatus
    expected_revision: StrictInt = Field(..., ge=1, le=MAX_SQLITE_INTEGER)


class StudyReviewRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128)
    card_id: str = Field(..., min_length=1, max_length=256)
    grade: StudyGrade
    expected_revision: StrictInt = Field(..., ge=1, le=MAX_SQLITE_INTEGER)
    response_ms: StrictInt | None = Field(None, ge=0, le=MAX_SQLITE_INTEGER)

    @field_validator("request_id", "card_id")
    @classmethod
    def require_nonempty_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class StudyStatusCounts(BaseModel):
    suggested: int
    active: int
    suspended: int
    rejected: int


class StudyGradeCounts(BaseModel):
    again: int
    hard: int
    good: int
    easy: int


class StudyStatsResponse(BaseModel):
    total: int
    statuses: StudyStatusCounts
    due: int
    reviewed_cards: int
    reviews_total: int
    grades: StudyGradeCounts
    retained_reviews: int
    retention_rate: float


class StudySuggestionBatchCreate(StrictRequest):
    request_id: str = Field(..., min_length=1, max_length=128)
    domain: str = Field(..., min_length=1, max_length=256)
    job_ids: list[str] | None = Field(None, max_length=100)
    concept_terms: list[str] | None = Field(None, max_length=100)
    max_cards: StrictInt = Field(10, ge=1, le=MAX_GENERATED_CARDS)

    @field_validator("request_id", "domain")
    @classmethod
    def strip_required_batch_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class StudySuggestionBatchRetry(StrictRequest):
    request_id: str = Field(..., min_length=1, max_length=128)
    expected_revision: StrictInt = Field(..., ge=1, le=MAX_SQLITE_INTEGER)

    @field_validator("request_id")
    @classmethod
    def strip_retry_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class StudySuggestionBatchResponse(BaseModel):
    batch_id: str
    domain: str
    status: str
    revision: int
    attempt: int
    task_id: str
    provider: str
    model: str
    max_cards: int
    error_code: str | None = None
    error_message: str | None = None
    deadline_at: str
    evidence_count: int = 0
    suggestion_count: int = 0
    created_at: str
    updated_at: str


class StudySuggestionEvidenceResponse(BaseModel):
    evidence_id: str
    job_id: str
    chunk_id: str
    note_type: str
    source_domain: str
    current_domain: str
    title: str
    section: str
    quote: str
    quote_sha256: str
    body_sha256: str
    locator: dict[str, Any]
    status: str
    invalid_reason: str | None = None


class StudySuggestionResponse(BaseModel):
    suggestion_id: str
    batch_id: str
    ordinal: int
    status: str
    revision: int
    domain: str
    concept_term: str | None = None
    knowledge_key: str
    card_type: str
    front: str
    back: str
    explanation: str
    accepted_card_id: str | None = None
    rejection_reason: str | None = None
    evidence: list[StudySuggestionEvidenceResponse]
    created_at: str
    updated_at: str


class StudySuggestionListResponse(BaseModel):
    total: int
    items: list[StudySuggestionResponse]


class StudySuggestionPatch(StrictRequest):
    card_type: Literal["basic", "cloze", "qa"] | None = None
    front: str | None = Field(None, min_length=1, max_length=20_000)
    back: str | None = Field(None, min_length=1, max_length=100_000)
    explanation: str | None = Field(None, max_length=100_000)
    concept_term: str | None = Field(None, max_length=256)


class StudySuggestionOperationItem(StrictRequest):
    suggestion_id: str = Field(..., min_length=1, max_length=256)
    expected_revision: StrictInt = Field(..., ge=1, le=MAX_SQLITE_INTEGER)
    action: SuggestionAction
    patch: StudySuggestionPatch | None = None
    reason: str | None = Field(None, max_length=2_000)


class StudySuggestionOperationsRequest(StrictRequest):
    request_id: str = Field(..., min_length=1, max_length=128)
    batch_id: str = Field(..., min_length=1, max_length=256)
    items: list[StudySuggestionOperationItem] = Field(
        ..., min_length=1, max_length=MAX_BATCH_ITEMS
    )


class StudySuggestionOperationsResponse(BaseModel):
    batch_id: str
    items: list[StudySuggestionResponse]
    cards: list[StudyCardResponse]


class StudyMasteryItem(BaseModel):
    domain: str
    concept_term: str
    score: int
    level: Literal["fragile", "learning", "mastered"]
    reviewed_cards: int
    reviews_total: int
    last_reviewed_at: str


class StudyMasteryResponse(BaseModel):
    total: int
    items: list[StudyMasteryItem]


router = APIRouter(
    prefix="/api/study", tags=["study"],
    dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


@router.post("/cards", response_model=StudyCardResponse, status_code=201)
async def create_card(req: StudyCardCreate, db: Database = Depends(get_db)):
    """手动创建学习卡片.active 卡片立即进入复习队列."""
    card = await asyncio.to_thread(
        db.create_study_card,
        card_id=f"sc_{uuid.uuid4().hex}",
        domain=req.domain,
        job_id=req.job_id,
        concept_term=req.concept_term,
        card_type=req.card_type,
        front=req.front,
        back=req.back,
        explanation=req.explanation,
        evidence=req.evidence,
        status=req.status,
        source=req.source,
    )
    return StudyCardResponse(**card)


@router.get("/cards", response_model=StudyCardListResponse)
async def list_cards(
    domain: str | None = None,
    status: CardStatus | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0, le=2_147_483_647),
    db: Database = Depends(get_db),
):
    """卡片库列表,支持 domain/status/q 过滤。"""
    total, items = await asyncio.to_thread(
        db.list_study_cards,
        domain=domain, status=status, q=(q or "").strip() or None,
        limit=limit, offset=offset,
    )
    return StudyCardListResponse(total=total, items=[StudyCardResponse(**it) for it in items])


@router.get("/due", response_model=StudyCardListResponse)
async def list_due_cards(
    domain: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
):
    """今日/当前已到期复习队列,按 due_at 从早到晚返回。"""
    total, items = await asyncio.to_thread(
        db.list_due_study_cards,
        domain=domain, limit=limit,
    )
    return StudyCardListResponse(total=total, items=[StudyCardResponse(**it) for it in items])


@router.get("/stats", response_model=StudyStatsResponse)
async def get_study_stats(
    domain: str | None = None,
    db: Database = Depends(get_db),
):
    """从全量已提交学习事实返回状态,到期和复习留存统计."""
    stats = await asyncio.to_thread(
        db.get_study_stats,
        domain=(domain or "").strip() or None,
    )
    return StudyStatsResponse(**stats)


def _raise_study_error(exc: Exception) -> None:
    if isinstance(exc, StudyNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, StudyConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "study_request_invalid", "message": str(exc)},
        ) from exc
    if isinstance(exc, StudySuggestionNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, StudySuggestionConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, sqlite3.IntegrityError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "study_suggestion_constraint_conflict",
                "message": "request conflicts with a committed study fact",
            },
        ) from exc
    raise exc


@router.post(
    "/suggestion-batches",
    response_model=StudySuggestionBatchResponse,
    status_code=202,
)
async def create_suggestion_batch(
    req: StudySuggestionBatchCreate,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """固化证据输入并创建持久 AI 批次."""
    try:
        batch = await asyncio.to_thread(
            db.create_study_suggestion_batch,
            request_id=req.request_id,
            domain=req.domain,
            job_ids=req.job_ids,
            concept_terms=req.concept_terms,
            max_cards=req.max_cards,
            prompt_snapshot=resolve_study_suggestion_prompt(
                hot_dir=config.prompts_dir / "templates",
                image_dir=config.config_dir / "prompts" / "templates",
            ),
        )
    except (
        StudySuggestionNotFoundError,
        StudySuggestionConflictError,
        ValueError,
        sqlite3.IntegrityError,
    ) as exc:
        _raise_study_error(exc)
    return StudySuggestionBatchResponse(**batch)


@router.get(
    "/suggestion-batches/{batch_id}",
    response_model=StudySuggestionBatchResponse,
)
async def get_suggestion_batch(
    batch_id: str,
    db: Database = Depends(get_db),
):
    """读取批次持久状态,前端可跨刷新继续轮询."""
    validate_path_segment(batch_id, "batch_id")
    batch = await asyncio.to_thread(db.get_study_suggestion_batch, batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "study_suggestion_batch_not_found", "message": "batch not found"},
        )
    return StudySuggestionBatchResponse(**batch)


@router.post(
    "/suggestion-batches/{batch_id}/retry",
    response_model=StudySuggestionBatchResponse,
    status_code=202,
)
async def retry_suggestion_batch(
    batch_id: str,
    req: StudySuggestionBatchRetry,
    db: Database = Depends(get_db),
):
    """仅对 failed 批次以新 task_id 执行幂等重试."""
    validate_path_segment(batch_id, "batch_id")
    try:
        batch = await asyncio.to_thread(
            db.retry_study_suggestion_batch,
            batch_id,
            request_id=req.request_id,
            expected_revision=req.expected_revision,
        )
    except (
        StudySuggestionNotFoundError,
        StudySuggestionConflictError,
        ValueError,
        sqlite3.IntegrityError,
    ) as exc:
        _raise_study_error(exc)
    return StudySuggestionBatchResponse(**batch)


@router.get("/suggestions", response_model=StudySuggestionListResponse)
async def list_suggestions(
    domain: str | None = None,
    batch_id: str | None = None,
    status: SuggestionStatus | None = None,
    limit: StrictInt = Query(100, ge=1, le=200),
    offset: StrictInt = Query(0, ge=0, le=2_147_483_647),
    db: Database = Depends(get_db),
):
    """按领域,批次和人工审核状态读取学习候选."""
    try:
        total, items = await asyncio.to_thread(
            db.list_study_suggestions,
            domain=(domain or "").strip() or None,
            batch_id=(batch_id or "").strip() or None,
            status=status,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        _raise_study_error(exc)
    return StudySuggestionListResponse(
        total=total,
        items=[StudySuggestionResponse(**item) for item in items],
    )


@router.post(
    "/suggestions/operations",
    response_model=StudySuggestionOperationsResponse,
)
async def operate_suggestions(
    req: StudySuggestionOperationsRequest,
    db: Database = Depends(get_db),
):
    """批量编辑,接受或拒绝候选,整批只提交一次."""
    validate_path_segment(req.batch_id, "batch_id")
    items = []
    for item in req.items:
        items.append(
            {
                "suggestion_id": item.suggestion_id,
                "expected_revision": item.expected_revision,
                "action": item.action,
                "patch": (
                    item.patch.model_dump(exclude_unset=True)
                    if item.patch is not None
                    else {}
                ),
                "reason": item.reason,
            }
        )
    try:
        result = await asyncio.to_thread(
            db.apply_study_suggestion_operations,
            request_id=req.request_id,
            batch_id=req.batch_id,
            items=items,
        )
    except (
        StudySuggestionNotFoundError,
        StudySuggestionConflictError,
        ValueError,
        sqlite3.IntegrityError,
    ) as exc:
        _raise_study_error(exc)
    return StudySuggestionOperationsResponse(**result)


@router.get("/mastery", response_model=StudyMasteryResponse)
async def get_mastery(
    domain: str | None = None,
    db: Database = Depends(get_db),
):
    """只从有真实复习日志的 active/suspended 卡片聚合概念掌握度."""
    try:
        result = await asyncio.to_thread(
            db.get_study_mastery,
            domain=(domain or "").strip() or None,
        )
    except ValueError as exc:
        _raise_study_error(exc)
    return StudyMasteryResponse(**result)


@router.post("/reviews", response_model=StudyCardResponse)
async def review_card(req: StudyReviewRequest, db: Database = Depends(get_db)):
    """幂等提交一次 active 卡片评分,在单事务内按 CAS 更新 SRS."""
    validate_path_segment(req.card_id, "card_id")
    try:
        updated = await asyncio.to_thread(
            db.record_study_review,
            request_id=req.request_id,
            card_id=req.card_id,
            grade=req.grade,
            expected_revision=req.expected_revision,
            response_ms=req.response_ms,
        )
    except (StudyNotFoundError, StudyConflictError, ValueError) as exc:
        _raise_study_error(exc)
    return StudyCardResponse(**updated)


@router.post("/cards/{card_id}/status", response_model=StudyCardResponse)
async def set_card_status(
    card_id: str,
    req: StudyCardStatusRequest,
    db: Database = Depends(get_db),
):
    """按 revision 执行 active/suspended 互转或 suggested 驳回."""
    validate_path_segment(card_id, "card_id")
    try:
        updated = await asyncio.to_thread(
            db.set_study_card_status,
            card_id,
            req.status,
            expected_revision=req.expected_revision,
        )
    except (StudyNotFoundError, StudyConflictError, ValueError) as exc:
        _raise_study_error(exc)
    return StudyCardResponse(**updated)


@router.delete("/cards/{card_id}", status_code=204)
async def delete_card(card_id: str, db: Database = Depends(get_db)):
    """删除卡片及其复习状态/日志。"""
    validate_path_segment(card_id, "card_id")
    try:
        ok = await asyncio.to_thread(db.delete_study_card, card_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "study_card_audit_protected",
                "message": "accepted suggestion card is protected by its audit trail",
            },
        ) from exc
    if not ok:
        raise HTTPException(404, "card not found")
    return Response(status_code=204)
