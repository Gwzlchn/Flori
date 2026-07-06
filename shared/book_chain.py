"""book 章序投递(工单 26-07-06/04 P2):找同书下一待投章。

book collection 的章 job 由 sync 全量建好但 defer(不触发调度);顺序执行:
前章 job 到终态(done/failed——失败也放行下一章,失败章单独 rerun,不卡整书)
→ scheduler 调 next_chapter_job 找「最早建的、尚未初始化调度」的章 → submit。
纯查询函数,publish/submit 由调用方(api sync 兜底 / scheduler)做。
"""

from __future__ import annotations


async def next_chapter_job(db, redis, collection_id: str) -> str | None:
    """返回该集合下一待投章的 job_id;有章在跑(已初始化未终态)或无待投章返回 None。

    待投 = DB status=pending 且 redis 无 job:{id}:steps(从未 submit);
    在跑 = 已有 steps hash 且 job 非终态(completed/failed)。
    章序 = created_at 升序(sync 按 toc 序建 job,天然即章序)。
    """
    import asyncio
    _, jobs = await asyncio.to_thread(
        db.list_jobs, None, collection_id, 100000, 0)
    jobs = sorted(jobs, key=lambda j: j.created_at or "")
    deferred = []
    for j in jobs:
        status = j.status.value if hasattr(j.status, "value") else str(j.status)
        inited = await redis.get_all_step_statuses(j.id)
        if inited:
            if status not in ("done", "failed"):
                return None                      # 有章在跑:严格串行,不投
        elif status == "pending":
            deferred.append(j)
    return deferred[0].id if deferred else None
