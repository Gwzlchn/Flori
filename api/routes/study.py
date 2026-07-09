"""学习闭环路由:卡片、到期队列和复习评分。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from shared.db import Database
from api.deps import get_db, validate_path_segment, verify_token
from api.services.study import StudyGrade, schedule_next_review


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
    front: str = Field(..., min_length=1)
    back: str = Field(..., min_length=1)
    explanation: str = ""
    evidence: Any = Field(default_factory=list)
    status: CardStatus = "active"
    source: str = "manual"


class StudyCardStatusRequest(BaseModel):
    status: CardStatus


class StudyReviewRequest(BaseModel):
    card_id: str
    grade: StudyGrade
    response_ms: int | None = Field(None, ge=0)


router = APIRouter(
    prefix="/api/study", tags=["study"],
    dependencies=[Depends(verify_token)],
)


@router.post("/cards", response_model=StudyCardResponse, status_code=201)
async def create_card(req: StudyCardCreate, db: Database = Depends(get_db)):
    """手动创建学习卡片。active 卡片立即进入复习队列。"""
    domain = req.domain.strip() or "general"
    card = await asyncio.to_thread(
        db.create_study_card,
        card_id=f"sc_{uuid.uuid4().hex}",
        domain=domain,
        job_id=req.job_id,
        concept_term=req.concept_term,
        card_type=req.card_type,
        front=req.front.strip(),
        back=req.back.strip(),
        explanation=req.explanation.strip(),
        evidence=req.evidence,
        status=req.status,
        source=req.source.strip() or "manual",
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


@router.post("/reviews", response_model=StudyCardResponse)
async def review_card(req: StudyReviewRequest, db: Database = Depends(get_db)):
    """提交一次复习评分,并按简化 SM-2 更新 due_at。"""
    validate_path_segment(req.card_id, "card_id")
    card = await asyncio.to_thread(db.get_study_card, req.card_id)
    if card is None:
        raise HTTPException(404, "card not found")
    schedule = schedule_next_review(card, req.grade)
    updated = await asyncio.to_thread(
        db.record_study_review,
        card_id=req.card_id,
        grade=req.grade,
        next_due_at=schedule["next_due_at"],
        interval_days=schedule["interval_days"],
        ease=schedule["ease"],
        repetitions=schedule["repetitions"],
        lapses=schedule["lapses"],
        response_ms=req.response_ms,
        reviewed_at=schedule["reviewed_at"],
    )
    if updated is None:
        raise HTTPException(404, "card not found")
    return StudyCardResponse(**updated)


@router.post("/cards/{card_id}/status", response_model=StudyCardResponse)
async def set_card_status(
    card_id: str,
    req: StudyCardStatusRequest,
    db: Database = Depends(get_db),
):
    """暂停、恢复或驳回卡片。恢复 active 时没有复习状态则立即排入队列。"""
    validate_path_segment(card_id, "card_id")
    updated = await asyncio.to_thread(db.set_study_card_status, card_id, req.status)
    if updated is None:
        raise HTTPException(404, "card not found")
    return StudyCardResponse(**updated)


@router.delete("/cards/{card_id}", status_code=204)
async def delete_card(card_id: str, db: Database = Depends(get_db)):
    """删除卡片及其复习状态/日志。"""
    validate_path_segment(card_id, "card_id")
    ok = await asyncio.to_thread(db.delete_study_card, card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    return Response(status_code=204)
