"""任务队列只读视图:列出各资源池里排队中 + 运行中的 task。

task = 某作业(job)的某步骤(step)的一次执行。
- 排队 task:来自 redis `queue:{pool}` ZSET(只读窥视,不弹出)+ join `queue:enqueued` 补入队时刻。
- 运行 task:来自 job_steps 里 status=running 的行(权威来源,自带 pool/worker_id/started_at)。
两类都 enrich 作业标题/类型,使前端显作业标题而非裸 job_id,并与 worker 任务历史共用同款 TaskRow。
"""
import asyncio

from fastapi import APIRouter, Depends, Query

from shared.config import AppConfig
from shared.db import Database
from shared.redis_client import RedisClient

from api.deps import get_config, get_db, get_redis, verify_token

router = APIRouter(prefix="/api/queue", tags=["queue"], dependencies=[Depends(verify_token)])

LIMIT = 200  # 每池排队 task 最多列出条数(超出仍报总数,不静默截断)


@router.get("")
async def get_queue(
    pool: str | None = Query(None, description="只看单个池(缺省=全部池)"),
    config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
):
    pools_cfg = config.pools.get("pools", {})
    pool_names = [pool] if pool else list(pools_cfg.keys())

    # 1) 各池排队 task(只读 ZRANGE,不弹出)+ 总数(可能 > 列出数)
    per_pool_queued: dict[str, list[dict]] = {}
    queued_counts: dict[str, int] = {}
    job_ids: set[str] = set()
    for name in pool_names:
        info = await redis.get_queue_info(name)
        queued_counts[name] = info.get("length", 0)
        items = await redis.list_queue(name, limit=LIMIT)
        per_pool_queued[name] = items
        for it in items:
            if it.get("job_id"):
                job_ids.add(it["job_id"])

    # 2) 运行 task = job_steps status=running(自带 pool/worker_id/started_at)
    running_steps = await asyncio.to_thread(db.list_running_steps)
    workers = await asyncio.to_thread(db.list_workers)
    worker_by_id = {w.id: w for w in workers}
    for s in running_steps:
        if s.job_id:
            job_ids.add(s.job_id)

    # 3) 批量 enrich 作业标题/类型/流水线(一次 IN 查询,避免 N+1)
    briefs = await asyncio.to_thread(db.jobs_brief, list(job_ids))

    def _brief(jid: str) -> dict:
        b = briefs.get(jid) or {}
        return {
            "title": b.get("title"),
            "content_type": b.get("content_type"),
            "domain": b.get("domain"),
            "pipeline": b.get("pipeline"),
        }

    def _running_view(s) -> dict:
        w = worker_by_id.get(s.worker_id)
        return {
            "state": "running",
            "job_id": s.job_id, "step": s.name, "pool": s.pool,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "worker_id": s.worker_id,
            "worker_type": w.type if w else None,
            "worker_hostname": w.hostname if w else None,
            **_brief(s.job_id),
        }

    # 4) 组装 per-pool 输出
    pools_out: list[dict] = []
    for name in pool_names:
        queued = [
            {
                "state": "queued",
                "job_id": it["job_id"], "step": it["step"], "pool": name,
                "priority": it.get("priority"),
                "enqueued_at": it.get("enqueued_at"),
                "tags": it.get("tags", []), "require_tags": it.get("require_tags", []),
                **_brief(it["job_id"]),
            }
            for it in per_pool_queued[name]
        ]
        running = [_running_view(s) for s in running_steps if s.pool == name]
        pools_out.append({
            "name": name,
            "queued_count": queued_counts.get(name, 0),
            "queued_shown": len(queued),
            "running": running,
            "queued": queued,
        })

    # 运行中但 pool 不在所选池范围的 task 单列兜底组,避免异常或历史 pool 名静默丢失.
    orphan = [_running_view(s) for s in running_steps if s.pool not in pool_names]
    if orphan:
        pools_out.append({
            "name": "(未归类)", "queued_count": 0, "queued_shown": 0,
            "running": orphan, "queued": [],
        })

    return {"pools": pools_out, "limit": LIMIT}
