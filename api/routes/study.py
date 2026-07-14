"""学习闭环路由:卡片、到期队列和复习评分。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictInt, field_validator

from shared.db import Database
from shared.study import (
    MAX_SQLITE_INTEGER,
    StudyConflictError,
    StudyGrade,
    StudyNotFoundError,
)
from api.deps import get_db, validate_path_segment, verify_token


CardType = Literal["basic", "cloze", "qa", "quiz_single", "quiz_multi"]
CardStatus = Literal["suggested", "active", "suspended", "rejected"]


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


router = APIRouter(
    prefix="/api/study", tags=["study"],
    dependencies=[Depends(verify_token)],
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
    raise exc


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
    ok = await asyncio.to_thread(db.delete_study_card, card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    return Response(status_code=204)
