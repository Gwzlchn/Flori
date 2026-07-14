"""跨源综合问答路由 POST /api/ask.

本路由只负责检索、拼 prompt 并投递 AI task 给 ai-worker。claude 调用、gateway、
pricing 与用量记账都在 worker 侧完成。答案与审计经 /api/ai-tasks/{task_id}/{result,log}
读取.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shared.ask_citations import build_source_manifest
from shared.db import Database
from shared.models import AITask, LLMRequest
from shared.redis_client import RedisClient
from shared.storage import StorageBackend
from api.deps import get_db, get_redis, get_storage, verify_token
from api.services import synthesis
from api.schemas import CanonicalEvidenceProjection
from api.services.evidence import attach_canonical_evidence
from api.wire_schemas import API_ERROR_RESPONSES

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api", tags=["ask"],
    dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000, description="自然语言问题")
    domain: str | None = Field(None, description="限定知识库(domain);null=全库")
    limit: int = Field(8, ge=1, le=20, description="检索并喂给 LLM 的最大笔记数")


class SourceItem(BaseModel):
    job_id: str
    title: str
    domain: str
    content_type: str
    evidence: dict
    canonical_evidence: list[CanonicalEvidenceProjection] = Field(default_factory=list)


class AskResponse(BaseModel):
    question: str
    task_id: str | None = None          # 无命中或投递失败时为 None.
    answer_markdown: str | None = None  # 有 task 时为 None,答案走 result 端点取.
    sources: list[SourceItem] = Field(default_factory=list)
    retrieved_count: int = 0


@router.post("/ask", response_model=AskResponse, status_code=202)
async def ask(
    req: AskRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    storage: StorageBackend = Depends(get_storage),
) -> AskResponse:
    """先检索 DB,有命中才投递 AI task. 无命中直接返回空答案,不投 task."""
    passages = await asyncio.to_thread(
        synthesis.retrieve, db, req.question, req.domain, req.limit
    )
    if not passages:
        return AskResponse(
            question=req.question, task_id=None,
            answer_markdown="没有找到相关笔记，无法作答。请换个说法或先往知识库里添加相关内容。",
            sources=[], retrieved_count=0,
        )

    await attach_canonical_evidence(db, storage, passages)

    sources = [
        SourceItem(
            job_id=p["job_id"], title=p["title"], domain=p["domain"],
            content_type=p["content_type"], evidence=p["evidence"],
            canonical_evidence=p["canonical_evidence"],
        )
        for p in passages
    ]
    system, user = synthesis.build_prompt(req.question, passages)
    request_obj = LLMRequest(
        messages=[{"role": "user", "content": user}], system=system,
        max_tokens=4096, temperature=0.3,  # 综合问答求稳,低温减少臆造
    )
    task_id = f"at_{uuid.uuid4().hex}"
    try:
        source_manifest = build_source_manifest(task_id, req.question, passages)
        payload = AITask(
            task_id=task_id,
            request=request_obj,
            step_name="synthesis",
            domain=req.domain,
            audit_context={"ask_source_manifest": source_manifest},
        ).to_task_payload()
        await redis.enqueue_ai_task(payload)
    except Exception as e:  # 投递失败时优雅降级,不冒 5xx;仍返回检索到的来源.
        log.warning("ask_enqueue_failed", error=str(e))
        return AskResponse(
            question=req.question, task_id=None,
            answer_markdown="⚠️ 综合服务暂不可用（任务投递失败），但已检索到下列相关笔记。",
            sources=sources, retrieved_count=len(passages),
        )

    return AskResponse(
        question=req.question, task_id=task_id, answer_markdown=None,
        sources=sources, retrieved_count=len(passages),
    )
