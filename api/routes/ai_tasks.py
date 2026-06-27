"""独立 AI task 的结果 / 白盒审计查看路由(P1-3,AI-worker-split)。

/ask、/digest 提交 AI task(queue:ai)后,前端经这里取结果(轮询)与审计:
- GET /api/ai-tasks/{task_id}/result  → pending / error / done(done 带 content)
- GET /api/ai-tasks/{task_id}/log     → 白盒审计(ai_task_logs:路由/尝试链/渲染 prompt/输出/raw/用量)
前端也可 WS /api/ws/jobs/{task_id} 拿 ai_task_done 信号(worker P1-2 已 publish),收到再来取 result。
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import APIRouter, Depends

from shared.db import Database
from shared.redis_client import RedisClient
from api.deps import get_db, get_redis, validate_path_segment, verify_token

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/ai-tasks", tags=["ai-tasks"], dependencies=[Depends(verify_token)])


@router.get("/{task_id}/result")
async def ai_task_result(task_id: str, redis: RedisClient = Depends(get_redis)):
    """取独立 AI task 结果。结果存 airesult:{task_id}(worker P1-2 写,带 TTL≈600s):
    None=未就绪→pending;{"error":...}=失败→error;否则=LLMResponse→done(带 content)。
    ask 读 answer_markdown、digest 读 markdown,两个别名都给 content,前端各取所需。"""
    validate_path_segment(task_id, "task_id")
    res = await redis.get_ai_result(task_id)
    if res is None:
        return {"status": "pending", "task_id": task_id}
    if isinstance(res, dict) and res.get("error"):
        return {"status": "error", "task_id": task_id, "error": res["error"]}
    content = (res or {}).get("content", "")
    return {
        "status": "done", "task_id": task_id,
        "content": content, "answer_markdown": content, "markdown": content,
        "provider": res.get("provider"), "model": res.get("model"), "cost_usd": res.get("cost_usd"),
    }


@router.get("/{task_id}/log")
async def ai_task_log(task_id: str, db: Database = Depends(get_db)):
    """独立 AI task 的【完整白盒审计】(prompt 白盒化,镜像 DAG 的 /jobs/{id}/ai-logs)。
    读 ai_task_logs(worker P1-2 每次执行写一条:路由/尝试链/渲染 prompt/输出/raw/用量),最近在前。"""
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
