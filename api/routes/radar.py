"""概念趋势雷达 + 本周摘要路由(均挂 verify_token)。

- GET  /api/domains/{domain}/radar    雷达数据(无 LLM,秒开,供页面加载)
- POST /api/domains/{domain}/digest   按需生成本周摘要(异步:投递 AI task 给 ai-worker,API 不调 claude)

雷达/摘要分离:页面先用 GET 快速渲染各板块;用户点「生成本周摘要」走 POST 投递 AI task,
返回 202 + task_id,经 GET /api/ai-tasks/{task_id}/result 取 markdown。
claude 在 ai-worker 跑,API 不持 claude/gateway/pricing。
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from fastapi import APIRouter, Depends, Query

from shared.db import Database
from shared.models import AITask, LLMRequest
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, validate_path_segment, verify_token
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


@router.post("/{domain}/digest", status_code=202)
async def post_digest(
    domain: str,
    window_days: int = Query(7, ge=1, le=90),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    """生成本周摘要(异步):先在 DB 算雷达,拼 prompt 后投递 AI task 给 ai-worker,返回 202 + task_id。
    前端轮询 /api/ai-tasks/{task_id}/result 取 markdown,digest 读 markdown 别名。"""
    validate_path_segment(domain, "domain")
    radar_data = await asyncio.to_thread(radar_service.radar, db, domain, window_days)
    recent_titles = [j["title"] for j in radar_data.get("recent_jobs", []) if j.get("title")]

    system, user = radar_service.build_digest_prompt(radar_data, recent_titles)
    request_obj = LLMRequest(
        messages=[{"role": "user", "content": user}], system=system,
        max_tokens=2048, temperature=0.7,
    )
    task_id = f"at_{uuid.uuid4().hex}"
    payload = AITask(
        task_id=task_id, request=request_obj, step_name="digest", domain=domain,
    ).to_task_payload()
    try:
        await redis.enqueue_ai_task(payload)
    except Exception as e:  # 投递失败(redis 不可用)→ 优雅降级,不冒 5xx;雷达数据走 GET 仍可看。
        log.warning("digest_enqueue_failed", domain=domain, error=str(e))
        return {
            "task_id": None, "window": radar_data["window"],
            "markdown": "⚠️ 周报生成暂不可用（任务投递失败）。雷达各板块见上方。",
        }

    return {"task_id": task_id, "window": radar_data["window"]}


@router.get("/{domain}/digest/latest")
async def get_latest_digest(
    domain: str,
    redis: RedisClient = Depends(get_redis),
):
    """最新一期自动周报(scheduler 每周定时投递并收割结果,见 §自动周报):
    {task_id, queued_at, markdown?, generated_at?, error?};从未生成过返回 {task_id: null}。"""
    validate_path_segment(domain, "domain")
    try:
        info = await redis.get_latest_auto_digest(domain)
    except Exception as e:   # redis 不可用 → 优雅降级,不冒 5xx
        log.warning("digest_latest_failed", domain=domain, error=str(e))
        info = None
    return info or {"task_id": None}
