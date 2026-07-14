"""独立 AI task 的结果 / 白盒审计查看路由.

/ask、/digest 提交 AI task(queue:ai)后,前端在这里轮询结果并查看审计.
GET /api/ai-tasks/{task_id}/result 返回 pending,error 或 done,done 带 content.
GET /api/ai-tasks/{task_id}/log 返回 ai_task_logs 中的路由,尝试链,渲染 prompt,输出,raw 与用量.
前端也可通过 WS /api/ws/jobs/{task_id} 等 ai_task_done 信号,收到后再取 result.
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, Depends

from shared.ask_citations import validate_bound_ask_citations
from shared.db import Database
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, validate_path_segment, verify_token

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/ai-tasks", tags=["ai-tasks"], dependencies=[Depends(verify_token)])


@router.get("/{task_id}/result")
async def ai_task_result(task_id: str, redis: RedisClient = Depends(get_redis)):
    """取独立 AI task 结果. 结果存 airesult:{task_id}(worker 写,TTL 约 600s):
    None 表示 pending;{"error":...} 表示 error;否则按 LLMResponse 返回 done.
    ask 读 answer_markdown,digest 读 markdown,两个别名都给 content,前端各取所需."""
    validate_path_segment(task_id, "task_id")
    res = await redis.get_ai_result(task_id)
    if res is None:
        return {"status": "pending", "task_id": task_id}
    content = (res or {}).get("content", "")
    source_manifest = res.get("source_manifest")
    citation_validation = res.get("citation_validation")
    original_payload = await redis.get_ai_task_original_payload(task_id)
    if original_payload is None:
        # 结果由 Worker 提供,不能反过来充当服务端来源锚点。锚点与结果同时
        # 缺失或锚点损坏时统一拒绝展示,避免恶意 Worker 自报 valid 后替换来源。
        citation_validation = validate_bound_ask_citations(
            task_id, content, source_manifest, None,
        )
        return {
            "status": "error", "task_id": task_id,
            "error": res.get("error") or "AI task provenance unavailable",
            "source_manifest": source_manifest,
            "citation_validation": citation_validation,
        }
    audit_context = (
        original_payload.get("audit_context")
        if original_payload is not None else None
    )
    original_manifest = (
        audit_context.get("ask_source_manifest")
        if type(audit_context) is dict
        else None
    )
    is_ask = original_payload.get("step") == "synthesis"
    if is_ask or source_manifest is not None or original_manifest is not None:
        # claim 中的原始 payload 是不可变锚点,远端 Worker 不能替换整套来源后自报 valid.
        citation_validation = validate_bound_ask_citations(
            task_id, content, source_manifest, original_manifest,
        )
    if res.get("error"):
        return {
            "status": "error", "task_id": task_id, "error": res["error"],
            "source_manifest": source_manifest,
            "citation_validation": citation_validation,
        }
    return {
        "status": "done", "task_id": task_id,
        "content": content, "answer_markdown": content, "markdown": content,
        "provider": res.get("provider"), "model": res.get("model"), "cost_usd": res.get("cost_usd"),
        "source_manifest": source_manifest,
        "citation_validation": citation_validation,
    }


@router.get("/{task_id}/log")
async def ai_task_log(task_id: str, db: Database = Depends(get_db)):
    """独立 AI task 的完整白盒审计(prompt 白盒化,镜像 DAG 的 /jobs/{id}/ai-logs)。
    读 ai_task_logs. worker 每次执行写一条,内容包含路由,尝试链,渲染 prompt,输出,raw 与用量."""
    validate_path_segment(task_id, "task_id")
    rows = await asyncio.to_thread(db.get_ai_task_logs, task_id)
    calls = []
    for r in rows:
        try:
            rec = json.loads(r.get("record_json") or "{}")
        except Exception:
            rec = {}
        calls.append({
            "task_id": r.get("task_id"), "exec_id": r.get("exec_id"),
            "step": r.get("step_name"), "domain": r.get("domain"),
            "provider": r.get("provider"), "model": r.get("model"),
            "ok": bool(r.get("ok")), "error": r.get("error"),
            "created_at": r.get("created_at"),
            "record": rec,
        })
    return {"task_id": task_id, "count": len(calls), "calls": calls}
