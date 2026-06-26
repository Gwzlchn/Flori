"""跨源综合问答路由 POST /api/ask。

提问 → 跨语料检索(api.services.synthesis.retrieve)→ LLM 综合带引用答案 → 记 AIUsage。
检索缓解(整句字面短语无法命中)与 prompt/综合逻辑都在 services/synthesis.py,本文件只做
HTTP 编排 + gateway 装配 + 用量记账,保持自包含(避免与并行特性改 shared 撞车)。
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from shared.ai_gateway import AIGateway
from shared.config import AppConfig
from shared.db import Database
from shared.models import AIUsage
from api.deps import get_config, get_db, verify_token
from api.services import synthesis

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api", tags=["ask"],
    dependencies=[Depends(verify_token)],
)

# 综合问答走 claude-cli 订阅(api 容器内 claude CLI 可用,凭证部署期注入)。
# gateway 路由按 step_name 取 ai 配置,这里就地给一个只含 synthesis 步的 pipelines_config,
# 不依赖 configs/pipelines.yaml 是否声明该步(自包含,不动共享配置)。
_SYNTH_PIPELINE = {
    "steps": [
        {
            "name": "synthesis",
            "ai": {"primary": {"provider": "claude-cli", "model": "subscription"}},
        }
    ]
}


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
    answer_markdown: str
    sources: list[SourceItem] = Field(default_factory=list)
    retrieved_count: int = 0


def _build_gateway(config: AppConfig) -> AIGateway:
    """装配只为 synthesis 步服务的 gateway:providers 取 app.state.config.providers
    (含 claude-cli),pipelines 用本模块就地的单步配置。提取成函数便于测试注入假 gateway。"""
    return AIGateway(config.providers or {}, _SYNTH_PIPELINE)


def _record_usage(request: Request, db: Database, resp, exec_id: str) -> None:
    """记一次综合问答的 AI 用量(mirror runner.record_usage)。

    claude-cli 订阅路径自带等价 total_cost_usd(gateway 已折算),不用 pricing 覆盖;
    其它 provider 若 app.state.pricing 有表则以权威价覆盖。best-effort,不让记账失败冒泡。"""
    usage = AIUsage(
        exec_id=exec_id,
        provider=resp.provider,
        model=resp.model,
        job_id=None,
        step="synthesis",
        worker_id=None,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cache_creation_input_tokens=resp.cache_creation_input_tokens,
        cache_read_input_tokens=resp.cache_read_input_tokens,
        cost_usd=resp.cost_usd,
        duration_sec=resp.duration_sec,
        num_turns=resp.num_turns,
        cached=resp.cached,
    )
    if resp.provider != "claude-cli":
        pricing = getattr(request.app.state, "pricing", None)
        if pricing is not None:
            c = pricing.cost(
                resp.provider, resp.model, resp.input_tokens, resp.output_tokens,
                resp.cache_creation_input_tokens, resp.cache_read_input_tokens,
            )
            if c is not None:
                usage.cost_usd = round(c, 6)
    db.record_ai_usage(usage)


@router.post("/ask", response_model=AskResponse)
async def ask(
    req: AskRequest,
    request: Request,
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
) -> AskResponse:
    """提问 → 检索 → 综合带引用答案。无命中则直接返回空答案不调 LLM。"""
    passages = await asyncio.to_thread(
        synthesis.retrieve, db, req.question, req.domain, req.limit
    )
    if not passages:
        return AskResponse(
            question=req.question,
            answer_markdown="没有找到相关笔记，无法作答。请换个说法或先往知识库里添加相关内容。",
            sources=[],
            retrieved_count=0,
        )

    sources = [
        SourceItem(
            job_id=p["job_id"], title=p["title"], domain=p["domain"], content_type=p["content_type"],
        )
        for p in passages
    ]

    # gateway 可被测试经 app.state.synthesis_gateway 注入(假 gateway 返回固定 LLMResponse)。
    gateway = getattr(request.app.state, "synthesis_gateway", None) or _build_gateway(config)
    try:
        resp = await synthesis.synthesize(gateway, req.question, passages)
    except Exception as e:  # LLM 不可用(未配凭证/调用失败)→ 优雅降级,不冒 5xx;仍回检索到的来源。
        log.warning("ask_synthesis_failed", error=str(e))
        return AskResponse(
            question=req.question,
            answer_markdown="⚠️ 综合服务暂不可用（LLM 未配置或调用失败），但已检索到下列相关笔记。",
            sources=sources,
            retrieved_count=len(passages),
        )

    exec_id = f"ask-{uuid.uuid4().hex}"
    await asyncio.to_thread(_record_usage, request, db, resp, exec_id)

    return AskResponse(
        question=req.question,
        answer_markdown=resp.content,
        sources=sources,
        retrieved_count=len(passages),
    )
