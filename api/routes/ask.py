"""跨源综合问答路由 POST /api/ask(异步:提交 AI task 给 ai-worker,API 不在进程内调 claude)。

提问 → 跨语料检索(api.services.synthesis.retrieve,纯 DB/CPU)→ 拼 prompt(synthesis.build_prompt)→
组 LLMRequest → 投递独立 AI task(queue:ai)→ 返 202 {task_id, sources}。
答案/审计经 GET /api/ai-tasks/{task_id}/{result,log}。claude 调用全在 ai-worker(P1-2);
本路由不再持 claude / gateway / pricing(用量记账已移 worker)。
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shared.db import Database
from shared.models import AITask, LLMRequest
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, verify_token
from api.services import synthesis

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api", tags=["ask"],
    dependencies=[Depends(verify_token)],
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="自然语言问题")
    domain: str | None = Field(None, description="限定知识库(domain);null=全库")
    limit: int = Field(8, ge=1, le=20, description="检索并喂给 LLM 的最大笔记数")


class SourceItem(BaseModel):
    job_id: str
    title: str
    domain: str
    content_type: str


class AskResponse(BaseModel):
    question: str
    task_id: str | None = None          # 提交的 AI task(轮询 /api/ai-tasks/{task_id}/result);无命中/投递失败时 None
    answer_markdown: str | None = None  # 仅无命中/投递失败时直接给消息;有 task 时为 None(答案走 result 端点取)
    sources: list[SourceItem] = Field(default_factory=list)
    retrieved_count: int = 0


@router.post("/ask", response_model=AskResponse, status_code=202)
async def ask(
    req: AskRequest,
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
) -> AskResponse:
    """提问 → 检索(DB)→ 投递 AI task(claude 在 ai-worker 跑)→ 202 + task_id。无命中直接返回空答案不投 task。"""
    passages = await asyncio.to_thread(
        synthesis.retrieve, db, req.question, req.domain, req.limit
    )
    if not passages:
        return AskResponse(
            question=req.question, task_id=None,
            answer_markdown="没有找到相关笔记，无法作答。请换个说法或先往知识库里添加相关内容。",
            sources=[], retrieved_count=0,
        )

    sources = [
        SourceItem(
            job_id=p["job_id"], title=p["title"], domain=p["domain"], content_type=p["content_type"],
        )
        for p in passages
    ]
    # 用现有纯 builder synthesis.build_prompt 拼 prompt,组 LLMRequest(max4096/温0.3)投给 ai-worker。
    system, user = synthesis.build_prompt(req.question, passages)
    request_obj = LLMRequest(
        messages=[{"role": "user", "content": user}], system=system,
        max_tokens=4096, temperature=0.3,  # 综合问答求稳,低温减少臆造
    )
    task_id = f"at_{uuid.uuid4().hex}"
    payload = AITask(
        task_id=task_id, request=request_obj, step_name="synthesis", domain=req.domain,
    ).to_task_payload()
    try:
        await redis.enqueue_ai_task(payload)
    except Exception as e:  # 投递失败(redis 不可用)→ 优雅降级,不冒 5xx;仍回检索到的来源。
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
