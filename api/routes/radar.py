"""概念趋势雷达 + 本周摘要路由(均挂 verify_token).

GET /api/domains/{domain}/radar 只做 DB 计算,不调 LLM。POST /api/domains/{domain}/digest
投递 AI task 给 ai-worker,API 不在进程内调 claude。雷达与摘要分离:页面先用 GET
快速渲染各板块;用户生成摘要时再走 POST,返回 202 和 task_id。前端经
/api/ai-tasks/{task_id}/result 取 markdown.
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
    if not (
        radar_data["recent_jobs"]
        or radar_data["rising_concepts"]
        or radar_data["new_concepts"]
    ):
        return {
            "task_id": None,
            "window": radar_data["window"],
            "markdown": "本周暂无可验证的新内容。",
            "citation_validation": {
                "kind": "digest_citations", "status": "not_applicable",
                "reliable": True, "issues": [], "items": [],
                "checked_claims": 0, "supported_claims": 0,
                "manifest_sha256": None,
            },
        }

    task_id = f"at_{uuid.uuid4().hex}"
    source_manifest = await asyncio.to_thread(
        radar_service.build_digest_source_manifest,
        db,
        task_id=task_id,
        radar_data=radar_data,
    )
    if not source_manifest["sources"]:
        return {
            "task_id": None,
            "window": radar_data["window"],
            "markdown": "⚠️ 窗口内内容尚无可验证证据，未生成周摘要。",
            "citation_validation": {
                "kind": "digest_citations", "status": "unverified",
                "reliable": False, "issues": ["canonical_evidence_unavailable"],
                "items": [], "checked_claims": 0, "supported_claims": 0,
                "manifest_sha256": source_manifest["manifest_sha256"],
            },
        }

    system, user = radar_service.build_digest_prompt(radar_data, source_manifest)
    request_obj = LLMRequest(
        messages=[{"role": "user", "content": user}], system=system,
        max_tokens=2048, temperature=0,
    )
    payload = AITask(
        task_id=task_id,
        request=request_obj,
        step_name="digest",
        domain=domain,
        audit_context={"digest_source_manifest": source_manifest},
    ).to_task_payload()
    try:
        await redis.enqueue_ai_task(payload)
    except Exception as e:  # 投递失败时优雅降级,不冒 5xx;雷达数据走 GET 仍可看.
        log.warning("digest_enqueue_failed", domain=domain, error=str(e))
        return {
            "task_id": None, "window": radar_data["window"],
            "markdown": "⚠️ 周报生成暂不可用（任务投递失败）。雷达各板块见上方。",
            "citation_validation": {
                "kind": "digest_citations", "status": "unverified",
                "reliable": False, "issues": ["digest_enqueue_failed"],
                "items": [], "checked_claims": 0, "supported_claims": 0,
                "manifest_sha256": source_manifest["manifest_sha256"],
            },
        }

    return {
        "task_id": task_id,
        "window": radar_data["window"],
        "source_count": len(source_manifest["sources"]),
        "manifest_sha256": source_manifest["manifest_sha256"],
    }


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
    except Exception as e:   # Redis 不可用时优雅降级,不冒 5xx.
        log.warning("digest_latest_failed", domain=domain, error=str(e))
        info = None
    if not info:
        return {"task_id": None}
    result = dict(info)
    validation = result.get("citation_validation")
    if type(validation) is dict and validation.get("reliable") is True:
        return result

    result.pop("markdown", None)
    original_issues = (
        validation.get("issues")
        if type(validation) is dict and type(validation.get("issues")) is list
        else []
    )
    result["citation_validation"] = {
        "kind": "digest_citations",
        "status": "unverified",
        "reliable": False,
        "issues": list(dict.fromkeys([
            "latest_digest_not_reliable",
            *(str(issue) for issue in original_issues),
        ])),
        "items": [],
        "checked_claims": 0,
        "supported_claims": 0,
        "manifest_sha256": (
            validation.get("manifest_sha256")
            if type(validation) is dict else None
        ),
    }
    result.setdefault("error", "digest citation validation unavailable")
    return result
