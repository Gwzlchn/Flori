"""概念趋势雷达 + 本周摘要路由（均挂 verify_token）。

- GET  /api/domains/{domain}/radar    雷达数据(无 LLM,秒开,供页面加载)
- POST /api/domains/{domain}/digest   按需调 LLM 生成本周摘要 markdown + 记 AIUsage

雷达/摘要分离:页面先用 GET 快速渲染各板块,用户点「生成本周摘要」再走 POST(LLM,较慢)。
gateway 调用代码自包含在本文件 + services/radar.py,不改动 shared 文件(降低合并冲突)。
"""

from __future__ import annotations

import asyncio
import secrets

import structlog
from fastapi import APIRouter, Depends, Query, Request

from shared.ai_gateway import AIGateway
from shared.config import AppConfig
from shared.db import Database
from shared.models import AIUsage
from api.deps import get_config, get_db, validate_path_segment, verify_token
from api.services import radar as radar_service

router = APIRouter(prefix="/api/domains", tags=["radar"], dependencies=[Depends(verify_token)])

log = structlog.get_logger(__name__)


@router.get("/{domain}/radar")
async def get_radar(
    domain: str,
    window_days: int = Query(7, ge=1, le=90),
    db: Database = Depends(get_db),
):
    """概念趋势雷达:飙升/新出现概念 + 窗口内新增内容 + 最热概念。纯 DB 计算,不调 LLM。"""
    validate_path_segment(domain, "domain")
    return await asyncio.to_thread(radar_service.radar, db, domain, window_days)


@router.post("/{domain}/digest")
async def post_digest(
    domain: str,
    request: Request,
    window_days: int = Query(7, ge=1, le=90),
    db: Database = Depends(get_db),
    config: AppConfig = Depends(get_config),
):
    """生成本周摘要:先算雷达 → 喂 LLM(claude-cli 订阅步)→ 记 AIUsage → 返回 markdown。"""
    validate_path_segment(domain, "domain")
    radar_data = await asyncio.to_thread(radar_service.radar, db, domain, window_days)
    recent_titles = [j["title"] for j in radar_data.get("recent_jobs", []) if j.get("title")]

    # 摘要步固定走 claude-cli 订阅(image 内有 CLI,凭证部署期注入);providers 取自全局配置。
    gateway = AIGateway(
        config.providers,
        {"steps": [{"name": "digest", "ai": {
            "primary": {"provider": "claude-cli", "model": "subscription"},
        }}]},
    )
    try:
        response = await radar_service.digest(gateway, radar_data, recent_titles)
    except Exception as e:  # LLM 不可用→优雅降级,不冒 5xx;雷达数据走 GET 仍可看。
        log.warning("digest_failed", domain=domain, error=str(e))
        return {
            "markdown": "⚠️ 周报生成暂不可用（LLM 未配置或调用失败）。雷达各板块见上方。",
            "window": radar_data["window"],
        }

    # 成本归因(镜像 runner.record_usage):exec_id 唯一去重,job_id=None(领域级,非单条内容)。
    usage = AIUsage(
        exec_id=f"digest-{domain}-{secrets.token_hex(8)}",
        provider=response.provider,
        model=response.model,
        job_id=None,
        step="digest",
        worker_id="api",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cache_creation_input_tokens=response.cache_creation_input_tokens,
        cache_read_input_tokens=response.cache_read_input_tokens,
        cost_usd=response.cost_usd,
        duration_sec=response.duration_sec,
        num_turns=response.num_turns,
        cached=response.cached,
    )
    await asyncio.to_thread(db.record_ai_usage, usage)

    return {"markdown": response.content, "window": radar_data["window"]}
