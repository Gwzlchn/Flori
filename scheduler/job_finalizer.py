"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
import os
import secrets

import structlog

from shared.models import JobStatus

if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

class JobFinalizer:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def mark_job_done(self, job_id: str) -> bool:
        generation = await self.owner.redis.get_job_generation(job_id)
        owner = f"scheduler:{os.getpid()}:{secrets.token_hex(8)}"
        finalizer = await self.owner.redis.acquire_job_finalizer(
            job_id, generation, "done", owner,
        )
        if finalizer == 0:
            return False
        if finalizer == 2:
            await asyncio.to_thread(
                self.owner.db.update_job, job_id,
                status=JobStatus.DONE, progress_pct=100,
            )
            await self.owner.redis.remove_active_job(job_id)
            return True
        # 只有声明副作用全部成功才越过 job 终态门。失败时步骤保持 done、job 留在 active,
        # 周期对账会继续幂等重放,不会要求 worker 重跑昂贵步骤。
        if not await self.owner._reconcile_completed_effects(job_id):
            logger.warning("job_completion_effects_pending", job_id=job_id)
            return False
        await asyncio.to_thread(
            self.owner.db.update_job, job_id,
            status=JobStatus.DONE, progress_pct=100,
        )
        await self.owner.redis.publish(f"events:{job_id}", {
            "event": "job_done", "progress_pct": 100,
        })
        await self.owner._advance_book_chain(job_id)
        await self.owner.redis.complete_job_finalizer(
            job_id, generation, "done", owner,
        )
        await self.owner.redis.remove_active_job(job_id)
        logger.info("job_done", job_id=job_id)
        return True

    async def reconcile_completion_effects(self) -> None:
        """周期收敛 active 终态门,并补齐历史已完成但无全文索引的 job。"""
        for job_id in await self.owner.redis.get_active_jobs():
            statuses = await self.owner.redis.get_all_step_statuses(job_id)
            if statuses and all(v in ("done", "skipped") for v in statuses.values()):
                await self.owner.mark_job_done(job_id)
        if self.owner.storage is None:
            return
        jobs = await asyncio.to_thread(self.owner.db.list_unindexed_done_jobs)
        for job in jobs:
            indexed = False
            for step, cfg in self.owner._get_pipeline_steps(job.pipeline).items():
                effects = [
                    effect for effect in (cfg.get("on_complete") or [])
                    if isinstance(effect, dict) and effect.get("action") == "index_note"
                ]
                for effect in effects:
                    indexed = (
                        await self.owner._run_completion_effects(job.id, step, [effect])
                        or indexed
                    )
            if indexed:
                logger.info(
                    "search_index_reconciled", job_id=job.id, pipeline=job.pipeline,
                )

    async def _advance_book_chain(self, job_id: str) -> None:
        """book 章序:本 job 属 book_toc 集合且到终态后,按 created_at 序 submit 下一待投章.
        best-effort:任何异常只 warn(书链卡住可由重新 sync 兜底 kick)。"""
        try:
            job = await asyncio.to_thread(self.owner.db.get_job, job_id)
            if not job or not job.collection_id:
                return
            coll = await asyncio.to_thread(self.owner.db.get_collection, job.collection_id)
            if not coll or getattr(coll, "source_type", None) != "book_toc":
                return
            from shared.book_chain import next_chapter_job
            nxt = await next_chapter_job(self.owner.db, self.owner.redis, job.collection_id)
            if not nxt:
                return
            nxt_job = await asyncio.to_thread(self.owner.db.get_job, nxt)
            if nxt_job:
                await self.owner.submit_job(nxt_job)
                logger.info("book_chain_advanced", coll=job.collection_id,
                            prev=job_id, next=nxt)
        except Exception:
            logger.warning("book_chain_advance_failed", job_id=job_id, exc_info=True)

    async def _sync_published_at(self, job_id: str) -> None:
        """把源内容发布时间(01_download 写入 input/metadata.json 的 published_at)同步进 DB,
        供概念时间线按源内容发布时间(而非入库时间)分桶;best-effort,读不到/解析失败/无该字段
        都只记日志,绝不阻塞 job 完成。

        经 storage 读 metadata.json:分布式部署产物在对象存储(MinIO)、不在调度器本地盘,
        与 _index_job_notes/_collect_glossary 同一路径;未注入 storage(老式单机)则跳过,
        留空时概念时间线对该 job 回退到 created_at,不丢计数。"""
        if self.owner.storage is None:
            return
        try:
            raw = await self.owner.storage.read_file(job_id, "input/metadata.json")
            md = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            document: dict = {}
            # 下载元数据优先；Document parser 的 canonical metadata 只补齐空标题/日期。
            if not md.get("title") or not (md.get("published_at") or md.get("date")):
                document_raw = await self.owner.storage.read_file(
                    job_id, "intermediate/document.json",
                )
                if document_raw:
                    document = json.loads(document_raw.decode("utf-8", errors="replace"))
                    metadata = document.get("metadata") if isinstance(document, dict) else {}
                    metadata = metadata if isinstance(metadata, dict) else {}
                    titles = metadata.get("titles") if isinstance(metadata.get("titles"), dict) else {}
                    fallback = {
                        "title": titles.get("original"),
                        "published_at": metadata.get("published_at"),
                    }
                    md = {
                        **{key: value for key, value in fallback.items() if value},
                        **{key: value for key, value in md.items() if value},
                    }
            if not md:
                return
            fields: dict = {}
            published = md.get("published_at") or md.get("date")
            if published:
                fields["published_at"] = published
            # 标题:01_download 从源 metadata 写入时回填,仅当 DB 标题为空,
            # 不覆盖订阅/用户已填的标题。例外:已入库标题是垃圾(pdf-only 的 PDF 内嵌 metadata,
            # 如 "10things"/"paper.dvi"/"NBER WORKING PAPER SERIES")且候选明显更像真标题
            # 非垃圾,含空格且更长时允许覆盖,与 02 步提取共用 shared.titles 同一套判定.
            title = (md.get("title") or "").strip()
            if title:
                from shared.titles import is_suspicious_title
                job = await asyncio.to_thread(self.owner.db.get_job, job_id)
                cur = (job.title or "").strip() if job else ""
                first_title = next((
                    str(block.get("text") or "").strip()
                    for block in document.get("blocks", [])
                    if isinstance(block, dict) and block.get("kind") == "title"
                ), "")
                canonical_recovery = (
                    cur and cur == first_title and cur != title
                    and not is_suspicious_title(title)
                )
                better = (
                    is_suspicious_title(cur) and not is_suspicious_title(title)
                    and " " in title and len(title) > len(cur)
                ) or canonical_recovery
                if job and (not cur or better):
                    fields["title"] = title
            if not fields:
                return
            await asyncio.to_thread(self.owner.db.update_job, job_id, **fields)
            logger.info("metadata_synced", job_id=job_id, **fields)
        except Exception:
            logger.warning("metadata_sync_failed", job_id=job_id)

    async def mark_job_failed(self, job_id: str, error: str) -> None:
        generation = await self.owner.redis.get_job_generation(job_id)
        owner = f"scheduler:{os.getpid()}:{secrets.token_hex(8)}"
        finalizer = await self.owner.redis.acquire_job_finalizer(
            job_id, generation, "failed", owner,
        )
        if finalizer == 0:
            return
        if finalizer == 2:
            await asyncio.to_thread(
                self.owner.db.update_job, job_id,
                status=JobStatus.FAILED, error=error[:500],
            )
            await self.owner.redis.remove_active_job(job_id)
            return
        self.owner._cancel_delayed_tasks(job_id)
        for step, status in (await self.owner.redis.get_all_step_statuses(job_id)).items():
            if status == "running":
                await self.owner._revoke_step_execution(job_id, step)
        progress = await self.owner._update_progress(job_id)
        await asyncio.to_thread(
            self.owner.db.update_job, job_id,
            status=JobStatus.FAILED, error=error[:500],
        )
        await self.owner.redis.publish(f"events:{job_id}", {
            "event": "job_failed", "error": error[:200], "progress_pct": progress,
        })
        await self.owner.redis.push_event("job_failed", job_id=job_id, error=error[:200])
        # 失败即停:清掉该 job 仍残留在 queue:{pool} 的兄弟 ready task。并行分支下某步终态失败时,
        # 其它已入队的兄弟步是死任务,job 已 FAILED 不该再跑;不清则 worker 仍会认领,cas_step_status
        # 因 steps hash 未清而成功,跑已失败 job 的步,甚至 _check_downstream 把它重标 done,还留下
        # 指向 FAILED job 的孤儿 task。保留 job:{id}:steps hash(不调 cleanup_job),重试/重跑仍可用。
        await self.owner.redis.remove_job_tasks(job_id)
        # book:失败章不卡整书,照样放行下一章(失败章单独 rerun;L2 术语表已含累积对照)。
        await self.owner._advance_book_chain(job_id)
        await self.owner.redis.complete_job_finalizer(
            job_id, generation, "failed", owner,
        )
        await self.owner.redis.remove_active_job(job_id)
        logger.info("job_failed", job_id=job_id, error=error[:200])
