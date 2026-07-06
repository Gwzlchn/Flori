"""调度器:监听步骤完成/失败事件,推进 DAG,管理 Job 生命周期。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import structlog

from shared.config import AppConfig, load_config
from shared.db import Database
from shared.errors import RETRY_POLICY, get_retry_delay
from shared.models import Job, JobStatus, Step, StepStatus
from shared.notify import notify
from shared.redis_client import RedisClient
from shared.runner_ops import parse_style_tags
from shared.terms import extract_pairs, zh_name_from_glossary_row
from shared.net_zone import required_zone
from shared.source_detect import detect_source
from shared.storage import StorageBackend
from shared.version import FLORI_VERSION

logger = structlog.get_logger(component="scheduler")

# 命中来源站点、需按网络可达区域(net-zone)路由的步骤;其余步骤本地/AI,不分区域。
# 区域判定见 shared.net_zone(按 URL + 构建时烤入的 CN 域名表);worker 启动自动探测自报覆盖区域。
# 网络路由 tag 只有 net-cn / net-global;B站 SESSDATA 等凭证是 worker 本地的事(下载步自读),非路由 tag。
_NET_STEPS = {"01_download", "07_danmaku"}

# 步骤静态优先级加权(分数 -= boost;zpopmin 越小越先)。02_whisper 防饿死(出稿硬依赖它)。
_PRIORITY_BOOST = {"02_whisper": 100}

# 延迟重试任务的 name 前缀,跟踪/按 job 取消时复用,避免格式漂移。
_DELAYED_PREFIX = "delayed_enqueue:"

# 笔记产出步 -> note_type。smart 已版本化(取最新版本文件),mechanical 走固定路径。
_NOTE_STEPS = {
    "11_smart": "smart",
    "05_smart_paper": "smart",
    "09_mechanical": "mechanical",
}
_NOTE_FILES = {
    "mechanical": "output/notes_mechanical.md",
}
# 评审步:完成后读 review.json,把 key_terms(讲清楚的概念 + 候选定义)采集为候选术语。
_REVIEW_STEPS = {"12_review", "06_review", "05_review"}  # video / paper / (article|audio)
# article 链的独立概念步(必跑)是 glossary 的主采集源:评审可选时仍能进图谱。
# 与 review 双触发无害——add_glossary_suggestion 按 job_id 去重 occurrence(幂等)。
_CONCEPT_STEPS = {"05_concepts"}
# 翻译步:完成后读 output/term_pairs.json,把本篇新定的「英文→中文译名」回流 glossary
# (术语一致性飞轮:下一篇同域翻译经 input/term_map.json 注入,见 shared/terms.py)。
_TRANSLATE_STEPS = {"04_translate_paper", "04_translate_article"}


def _markdown_to_text(md: str) -> str:
    """Markdown 去标记取纯文本(轻量、零依赖,够 FTS 索引用):剥代码围栏、
    图片/链接、标题/列表/强调标记,折叠空白。"""
    import re

    md = re.sub(r"```.*?```", " ", md, flags=re.DOTALL)        # 代码围栏
    md = re.sub(r"<[^>]+>", " ", md)                             # HTML 标签(防搜索高亮 XSS)
    md = re.sub(r"`([^`]*)`", r"\1", md)                         # 行内代码
    md = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", md)            # 图片:保留 alt 描述进 FTS
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)            # 链接取文字
    md = re.sub(r"^\s{0,3}#{1,6}\s*", "", md, flags=re.MULTILINE)  # 标题井号
    md = re.sub(r"^\s{0,3}[-*+]\s+", "", md, flags=re.MULTILINE)   # 无序列表标记
    md = re.sub(r"[*_~>]+", " ", md)                             # 强调/引用标记
    return " ".join(md.split())


class Scheduler:
    def __init__(
        self,
        redis: RedisClient,
        db: Database,
        config: AppConfig,
        storage: StorageBackend | None = None,
    ):
        self.redis = redis
        self.db = db
        self.config = config
        # storage 在 NAS 侧(调度器有 DB)读笔记/评审产物做索引与术语采集;
        # worker 可能远程无 DB,故索引必须落在这里。未注入则跳过(向后兼容)。
        self.storage = storage
        self.jobs_dir = config.jobs_dir
        self._shutdown = False
        self._pubsub_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # 心跳 payload 源:启动时刻(算 uptime)+ 上一拍 periodic 循环的实测延迟(loop_lag)。
        self._started_at_iso = datetime.now(timezone.utc).isoformat()
        self._last_tick: float | None = None       # 上一拍 periodic 循环的 monotonic 时刻
        self._last_loop_lag: float = 0.0            # 实测间隔 - 期望(30s)的超出量,≥5s 叠加 degraded
        # 跟踪所有 _delayed_enqueue fire-and-forget 任务,供 shutdown / rerun /
        # job 失败时取消,避免泄漏或旧重试与新状态串台。
        self._delayed_tasks: set[asyncio.Task] = set()
        # job_id -> 首次被判定"无 worker 可推进"的时刻,超宽限期才 fail-fast(容忍 worker 重启)。
        self._no_worker_since: dict[str, float] = {}
        # (job_id, step) -> 首次发现"在跑步骤的 worker 上报的 current_step 不是本步"的时刻,
        # 超宽限期才回收(容忍认领后首拍心跳延迟),防 gateway 认领响应丢失导致的永久卡 running。
        self._claim_mismatch_since: dict[tuple[str, str], float] = {}
        # 上一拍判定为"陈旧"(持有槽但不属于任何 running 步)的 holder 集合。仅连续两拍都陈旧才 SREM,
        # 避开"刚占槽、尚未写 running 状态"的认领窗口被周期对账误清(同 _claim_mismatch_since 的宽限思路)。
        self._slot_reconcile_suspect: set[str] = set()

    # 生命周期

    async def run(self) -> None:
        logger.info("scheduler_start")
        self._started_at_iso = datetime.now(timezone.utc).isoformat()
        await self._publish_resource_limits()
        await self._recover()
        self._pubsub_task = asyncio.create_task(self._event_loop())
        self._periodic_task = asyncio.create_task(self._periodic_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await asyncio.gather(
                self._pubsub_task,
                self._periodic_task,
                self._heartbeat_task,
            )
        except asyncio.CancelledError:
            logger.info("scheduler_cancelled")

    async def _publish_resource_limits(self) -> None:
        """把 configs/resources.yaml 的资源上限刷进 redis(单一事实源),供 claim_step 读。
        资源上限改动需重启 scheduler 才重推(资源集稳定,极少改)。"""
        await self.redis.set_resource_limits(self.config.resources or {})

    async def shutdown(self) -> None:
        logger.info("scheduler_shutdown")
        self._shutdown = True
        if self._pubsub_task and not self._pubsub_task.done():
            self._pubsub_task.cancel()
        if self._periodic_task and not self._periodic_task.done():
            self._periodic_task.cancel()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        pending = [t for t in self._delayed_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _on_delayed_done(self, task: asyncio.Task) -> None:
        """延迟任务完成回调:从跟踪集合移除;非取消的真异常上报。"""
        self._delayed_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "delayed_enqueue_failed",
                task=task.get_name(), exc_info=task.exception(),
            )

    def _cancel_delayed_tasks(self, job_id: str) -> None:
        """取消某 job 在途的延迟重试任务(rerun / job 失败时调用)。"""
        prefix = f"{_DELAYED_PREFIX}{job_id}:"
        for t in list(self._delayed_tasks):
            if t.get_name().startswith(prefix) and not t.done():
                t.cancel()

    # 主循环

    async def _event_loop(self) -> None:
        """订阅事件并分发。连接级异常(redis 超时/断连)不崩进程:
        指数退避后重连重订阅;启动恢复也会补推漏掉的步骤。"""
        backoff = 1
        while not self._shutdown:
            try:
                async for msg in self.redis.subscribe(
                    "step_started", "step_completed", "step_failed", "job_command",
                ):
                    if self._shutdown:
                        break
                    backoff = 1  # 收到任何消息说明连接健康,重置退避
                    try:
                        await self._dispatch(msg)
                    except Exception:
                        logger.exception("event_handler_error", msg=msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._shutdown:
                    break
                logger.exception("event_loop_reconnect", backoff=backoff)
                # 重连前先尝试重建底层连接 + 补推可能漏掉的事件
                try:
                    await self.redis.reconnect()
                    await self._recover()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("event_loop_recover_failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _dispatch(self, msg: dict) -> None:
        status = msg.get("status")
        command = msg.get("command") or msg.get("action")

        if status == "running":
            await self.on_step_started(
                msg["job_id"], msg["step"], worker=msg.get("worker"),
            )
        elif status == "done":
            await self.on_step_done(
                msg["job_id"], msg["step"],
                duration=msg.get("duration"),
                worker=msg.get("worker"),
                exec_id=msg.get("exec_id"),
            )
        elif status == "failed":
            await self.on_step_failed(
                msg["job_id"], msg["step"],
                msg.get("error", ""),
                msg.get("error_type", "unknown"),
                exec_id=msg.get("exec_id"),
            )
        elif command == "new_job":
            job = await asyncio.to_thread(self.db.get_job, msg["job_id"])
            if job:
                await self.submit_job(job)
        elif command == "rerun":
            await self.rerun(msg["job_id"], msg["from_step"])
        elif command == "resubmit":
            await self.resubmit(msg["job_id"])
        elif command == "retry":
            await self._retry_failed(msg["job_id"])
        elif command == "delete":
            # 消费 delete_job 端点的 publish:删 job 的编排状态收尾——取消在途重试、移出
            # active_jobs、清五个 Redis 编排 hash(job:{id}/steps/retries/step_worker/step_exec)。
            # 否则删在途(processing) job 后这些键泄漏,幽灵 job 被 orphan_scan/check_no_worker/
            # check_stuck 周期空扫,迟到的 on_step_done 还可能 CAS 推进已删 job。
            job_id = msg["job_id"]
            self._cancel_delayed_tasks(job_id)
            await self.redis.remove_active_job(job_id)
            await self.redis.cleanup_job(job_id)
            # 清队列里该 job 尚未认领的排队 task(queue:{pool}+queue:enqueued)。
            # API 删除路径已同步清过;此处兜底 CLI/其它经 pubsub 发起的删除。幂等。
            await self.redis.remove_job_tasks(job_id)
            logger.info("job_deleted_cleanup", job_id=job_id)

    _PERIODIC_INTERVAL_SEC = 30

    async def _periodic_loop(self) -> None:
        while not self._shutdown:
            # 实测本拍与上一拍的间隔,超出期望(30s)的部分=loop_lag(循环被拖慢的信号);
            # 心跳把它带给 /api/status 的 scheduler 组件(>5s 叠加 degraded)。
            now = time.monotonic()
            if self._last_tick is not None:
                self._last_loop_lag = max(
                    0.0, (now - self._last_tick) - self._PERIODIC_INTERVAL_SEC,
                )
            self._last_tick = now
            try:
                await self.orphan_scan()
                await self.check_stuck()
                await self.check_no_worker()
                await self.cleanup_stale_workers()
                await self.reconcile_slots()
            except Exception:
                logger.exception("periodic_error")
            await asyncio.sleep(self._PERIODIC_INTERVAL_SEC)

    async def _heartbeat_loop(self) -> None:
        """每 ~10s 写 component:scheduler 心跳(<online_window/3,容忍丢 2 拍仍 up)。
        瞬态 redis 抖动不中断循环:记日志后续跑,下一拍重写;丢几拍由 stale 窗口容忍。"""
        while not self._shutdown:
            try:
                await self.redis.set_component_heartbeat("scheduler", {
                    "version": FLORI_VERSION,
                    "started_at": self._started_at_iso,
                    "loop_lag_sec": round(self._last_loop_lag, 2),
                    "loop_interval_sec": self._PERIODIC_INTERVAL_SEC,
                    "pid": os.getpid(),
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("heartbeat_failed", exc_info=True)
            await asyncio.sleep(10)

    async def cleanup_stale_workers(self, timeout_sec: int | None = None) -> None:
        """清理僵尸 worker:DB 中 last_heartbeat 超时且 Redis 注册已过期(worker 真没了)
        的记录删除;仅 DB 过期但 Redis 仍在的标 offline(容器可能刚重启换 id)。

        删除阈值默认取 config.pools['worker_status'].stale_window_sec,与 API 侧
        compute_worker_status 的 STALE 窗口对齐(单一事实源)。阈值若小于该窗口,
        GC 会在 worker 进入 STALE 公开态之前就删 DB 行,使 STALE 态实际不可达;
        对齐后 worker 在被判 STALE 之前不会被回收。"""
        from datetime import timedelta

        if timeout_sec is None:
            ws = (self.config.pools.get("worker_status") or {}) if self.config else {}
            timeout_sec = int(ws.get("stale_window_sec", 900))
        workers = await asyncio.to_thread(self.db.list_workers)
        now = datetime.now(timezone.utc)
        for w in workers:
            hb = w.last_heartbeat
            stale = hb is None or (now - hb) > timedelta(seconds=timeout_sec)
            if not stale:
                continue
            alive = await self.redis.worker_exists(w.id)
            if alive:
                # list_workers 已按心跳新鲜度衍生公共状态,故此处直接持久化(幂等),
                # 不能用 w.status 判断是否需要写。
                await asyncio.to_thread(
                    self.db.set_worker_status, w.id, "offline",
                )
            else:
                await asyncio.to_thread(self.db.delete_worker, w.id)
                await self.redis.push_event("worker_cleaned", worker_id=w.id)
                logger.info("worker_cleaned", worker_id=w.id)

    async def _recover(self) -> None:
        """启动恢复:补推满足依赖的步骤,回收无主 running 步骤。"""
        active_jobs = await self.redis.get_active_jobs()
        logger.info("recover_start", active_jobs=len(active_jobs))

        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            if not statuses:
                await self.redis.remove_active_job(job_id)
                continue

            for step, status in statuses.items():
                if status == "running":
                    worker_id = await self.redis.get_step_worker(job_id, step)
                    if not worker_id or not await self.redis.worker_exists(worker_id):
                        await self._reclaim_step(
                            job_id, step, f"recover: worker {worker_id or 'none'} lost"
                        )

            await self._check_downstream(job_id)

        logger.info("recover_done", active_jobs=len(active_jobs))

    # Job 提交

    async def submit_job(self, job: Job) -> None:
        """API 调用:提交新任务,初始化步骤状态,入队无依赖步骤。"""
        pipeline_steps = self._get_pipeline_steps(job.pipeline)
        if not pipeline_steps:
            logger.warning("empty_pipeline", job_id=job.id, pipeline=job.pipeline)
            await asyncio.to_thread(
                self.db.update_job, job.id,
                status=JobStatus.FAILED, error=f"unknown pipeline: {job.pipeline}",
            )
            return

        await self.redis.init_job(job.id, job.pipeline, {
            "domain": job.domain,
            "style_tags": job.style_tags,
            "url": job.url or "",
            "source": job.source or "",
            # 投递开关(如 smart_note),供 rules 的 if_flag 求值,条件跳步见 _eval_rules。
            "flags": (job.meta or {}).get("flags", {}),
        })

        for name, cfg in pipeline_steps.items():
            await self.redis.set_step_status(job.id, name, "waiting")
            await asyncio.to_thread(
                self.db.upsert_step,
                Step(job_id=job.id, name=name, status=StepStatus.WAITING, pool=cfg["pool"]),
            )

        await self._export_term_map(job)

        await self.redis.add_active_job(job.id)
        await self._check_downstream(job.id)

        logger.info("job_submitted", job_id=job.id, pipeline=job.pipeline)

    # 事件处理

    async def _exec_is_current(self, job_id: str, step: str, exec_id: str) -> bool:
        """事件携带的 exec_id 是否为该步当前在跑的执行实例。
        无记录(旧库/未写回)时放行,保持向后兼容。"""
        current = await self.redis.get_step_exec_id(job_id, step)
        return current is None or current == exec_id

    async def on_step_started(
        self, job_id: str, step: str, worker: str | None = None,
    ) -> None:
        # 把"运行中"落 DB,让 REST(/api/jobs)也能显示 running,不只 WebSocket。
        # 仅当 Redis 仍为 running 时写:避免快步骤的 step_completed 先到、迟到的
        # step_started 把已完成步骤倒回 running(两条不同频道,跨频道顺序无保证)。
        if await self.redis.get_step_status(job_id, step) != "running":
            return
        await asyncio.to_thread(
            self.db.update_step, job_id, step,
            status="running", worker_id=worker, started_at=datetime.now(timezone.utc),
        )

    async def on_step_done(
        self,
        job_id: str,
        step: str,
        duration: float | None = None,
        worker: str | None = None,
        exec_id: str | None = None,
    ) -> None:
        # 丢弃陈旧执行的完成事件:孤儿重排后旧 worker 迟到上报,其 exec_id 不再是当前
        # 在跑的实例 → 忽略,避免提前置 done 顶替仍在跑的新执行(双执行/读到不完整产物)。
        if exec_id is not None and not await self._exec_is_current(job_id, step, exec_id):
            logger.warning("stale_exec_done_ignored", job_id=job_id, step=step, exec_id=exec_id)
            return
        ok = await self.redis.cas_step_status(job_id, step, "running", "done")
        if not ok:
            return

        await asyncio.to_thread(
            self.db.update_step, job_id, step,
            status="done",
            worker_id=worker,
            finished_at=datetime.now(timezone.utc),
            duration_sec=duration,
        )

        progress = await self._update_progress(job_id)
        await self.redis.publish(f"events:{job_id}", {
            "event": "step_done", "step": step,
            "duration_sec": duration, "progress_pct": progress,
        })

        # 笔记产出步 -> 建全文索引;评审步 -> 采集候选术语。失败只 log 不致命。
        await self._index_on_step_done(job_id, step)

        logger.info("step_done", job_id=job_id, step=step, duration=duration)
        await self._check_downstream(job_id)

    async def _index_on_step_done(self, job_id: str, step: str) -> None:
        """步骤完成后的知识库副作用:笔记产出步建 FTS 索引、评审步采集术语。
        全程容错——无 storage / 读不到产物 / 解析异常都只记日志,绝不影响 DAG 推进。"""
        if self.storage is None:
            return
        try:
            if step in ("01_download", "02_pdf_parse", "02_parse_article"):
                # 下载完即从 metadata/article_meta 同步标题/时间,使内容名在处理过程中即可显示;
                # 论文标题只在 02_pdf_parse 写的 parsed.json,01 时还没有,故解析步后再同步一次。
                # 这样 AI 步未跑、job 卡住时也不必等 job_done 就能出标题;job_done 时仍兜底。
                await self._sync_published_at(job_id)
            elif step in _NOTE_STEPS:
                await self._index_job_notes(job_id, _NOTE_STEPS[step])
            elif step in _REVIEW_STEPS or step in _CONCEPT_STEPS:
                await self._collect_glossary(job_id)
            elif step in _TRANSLATE_STEPS:
                await self._collect_term_pairs(job_id)
        except Exception:
            logger.warning("index_step_done_failed", job_id=job_id, step=step)

    async def _index_job_notes(self, job_id: str, note_type: str) -> None:
        """读该 job 的笔记 Markdown,去标记取纯文本,连同 job 元信息写入 FTS 索引。"""
        rel = _NOTE_FILES.get(note_type)
        if note_type == "smart":   # 智能笔记已版本化,取最新版本文件
            from shared.notes_versions import latest_smart
            rel = latest_smart(await self.storage.list_files(job_id))
        if not rel:
            return
        data = await self.storage.read_file(job_id, rel)
        if not data:
            return
        md = data.decode("utf-8", errors="replace")
        body = _markdown_to_text(md)
        if not body:
            return
        job = await asyncio.to_thread(self.db.get_job, job_id)
        title = (job.title if job else None) or job_id
        domain = job.domain if job else ""
        content_type = job.content_type if job else ""
        collection_id = (job.collection_id if job else "") or ""
        await asyncio.to_thread(
            self.db.index_job_notes,
            job_id, note_type, title, body,
            content_type, domain, collection_id,
        )
        logger.info("notes_indexed", job_id=job_id, note_type=note_type)

    async def _export_term_map(self, job: Job) -> None:
        """术语一致性 L1(+L2)导出:把该 domain 的 glossary 译名快照写 input/term_map.json,
        供翻译步(worker 无 DB)按 chunk 命中注入。job 属集合且集合有 terms.json(L2,book)
        则合并(L2 覆盖 L1)。best-effort:失败只 warn,不阻塞提交。"""
        if self.storage is None:
            return
        try:
            rows = await asyncio.to_thread(self.db.glossary_term_rows, job.domain or "general")
            tmap: dict[str, str] = {}
            for r in rows:
                pair = zh_name_from_glossary_row(r.get("term") or "", r.get("zh_name"), r.get("definition") or "")
                if pair:
                    tmap[pair[0]] = pair[1]
            if job.collection_id:
                raw = await self.storage.read_file(f"collections/{job.collection_id}", "terms.json")
                if raw:
                    try:
                        tmap.update(json.loads(raw.decode("utf-8", errors="replace")))
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("collection_terms_invalid", collection=job.collection_id)
            if not tmap:
                return
            await self.storage.write_file(
                job.id, "input/term_map.json",
                json.dumps(tmap, ensure_ascii=False, indent=1).encode("utf-8"),
            )
            logger.info("term_map_exported", job_id=job.id, terms=len(tmap))
        except Exception:
            logger.warning("term_map_export_failed", job_id=job.id, exc_info=True)

    async def _collect_term_pairs(self, job_id: str) -> None:
        """翻译步完成回流:output/term_pairs.json(本篇新定译名)→ glossary(suggested,带 zh_name);
        job 属集合(book)时同步 merge 进 collections/{id}/terms.json(L2,后章注入)。"""
        if self.storage is None:
            return
        data = await self.storage.read_file(job_id, "output/term_pairs.json")
        if not data:
            return
        try:
            pairs = json.loads(data.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(pairs, dict) or not pairs:
            return
        job = await asyncio.to_thread(self.db.get_job, job_id)
        domain = (job.domain if job else "") or "general"
        for en, zh in pairs.items():
            if not isinstance(en, str) or not isinstance(zh, str) or not en or not zh:
                continue
            await asyncio.to_thread(
                self.db.add_glossary_suggestion,
                domain, en, job_id, job.content_type if job else "", None, "", zh,
            )
        if job and job.collection_id:
            try:
                prefix = f"collections/{job.collection_id}"
                raw = await self.storage.read_file(prefix, "terms.json")
                merged: dict = {}
                if raw:
                    try:
                        merged = json.loads(raw.decode("utf-8", errors="replace")) or {}
                    except (json.JSONDecodeError, ValueError):
                        merged = {}
                # 先到先得:已有译名不被后章覆盖(与注入层 L2>L1、篇内首译优先一致)。
                for en, zh in pairs.items():
                    merged.setdefault(en, zh)
                await self.storage.write_file(
                    prefix, "terms.json",
                    json.dumps(merged, ensure_ascii=False, indent=1).encode("utf-8"),
                )
            except Exception:
                logger.warning("collection_terms_merge_failed", job_id=job_id, exc_info=True)
        logger.info("term_pairs_collected", job_id=job_id, count=len(pairs))

    async def _collect_glossary(self, job_id: str) -> None:
        """把 key_terms(这篇讲清楚的概念 + 候选定义)采集为候选术语。
        主喂养源是评审"讲清楚了什么"一节;missing_concepts(知识缺口)只留评审面板,不喂术语库。
        采集源:优先 output/concepts.json(article 链的独立概念步,必跑),回退 output/review.json
        (video/paper/audio 由评审步出 key_terms)。"""
        data = await self.storage.read_file(job_id, "output/concepts.json")
        if not data:
            data = await self.storage.read_file(job_id, "output/review.json")
        if not data:
            return
        try:
            review = json.loads(data.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return
        key_terms = review.get("key_terms") or []
        if not isinstance(key_terms, list) or not key_terms:
            return
        job = await asyncio.to_thread(self.db.get_job, job_id)
        domain = (job.domain if job else "") or "general"
        content_type = job.content_type if job else ""
        collected = 0
        for t in key_terms:
            if isinstance(t, dict):
                term, definition = t.get("term"), (t.get("definition") or "")
                zh_name = t.get("zh_name") if isinstance(t.get("zh_name"), str) else ""
            else:
                term, definition, zh_name = t, "", ""
            if not term or not isinstance(term, str):
                continue
            await asyncio.to_thread(
                self.db.add_glossary_suggestion,
                domain, term, job_id, content_type, None, definition, zh_name,
            )
            collected += 1
        logger.info("glossary_collected", job_id=job_id, count=collected)

    async def on_step_failed(
        self,
        job_id: str,
        step: str,
        error: str,
        error_type: str = "unknown",
        exec_id: str | None = None,
    ) -> None:
        # 同 on_step_done:丢弃陈旧执行的失败事件,不让旧实例顶替当前在跑的步骤。
        if exec_id is not None and not await self._exec_is_current(job_id, step, exec_id):
            logger.warning("stale_exec_failed_ignored", job_id=job_id, step=step, exec_id=exec_id)
            return
        ok = await self.redis.cas_step_status(job_id, step, "running", "failed")
        if not ok:
            return

        logger.warning(
            "step_failed", job_id=job_id, step=step,
            error_type=error_type, error=error[:200],
        )

        pipeline_steps = await self._get_job_pipeline_steps(job_id)
        if not pipeline_steps:
            return
        cfg = pipeline_steps.get(step, {})
        pipeline_retries = cfg.get("retries", 0)

        # 缺表项(如 unknown)按 max 0 处理:未归类失败默认 BUILD,不重试。
        # pipeline_retries 二次封顶 policy_max:用户不可放大 SYSTEM 类的上限。
        policy = RETRY_POLICY.get(error_type, {})
        policy_max = policy.get("max", 0)
        max_retries = min(policy_max, pipeline_retries)

        current_retries = await self.redis.get_step_retries(job_id, step)

        if current_retries < max_retries:
            await self.redis.incr_step_retries(job_id, step)
            # 同步 DB retries 列:重试计数权威在 redis(job:{id}:retries),但 DB 只在终态才写,
            # 重试中 UI/排查看到 retries=0 会误判"超时不计数、无限循环"(线上 GPT-3 翻译步实证误读)。
            await asyncio.to_thread(
                self.db.update_step, job_id, step, retries=current_retries + 1,
            )
            delay = get_retry_delay(error_type, current_retries) or 0
            logger.info(
                "step_retry", job_id=job_id, step=step,
                attempt=current_retries + 1, max=max_retries, delay=delay,
            )
            # enqueue_step will set status to "ready" (from current "failed")
            if delay > 0:
                task = asyncio.create_task(
                    self._delayed_enqueue(delay, job_id, step),
                    name=f"{_DELAYED_PREFIX}{job_id}:{step}",
                )
                self._delayed_tasks.add(task)
                task.add_done_callback(self._on_delayed_done)
            else:
                await self.enqueue_step(job_id, step)

            await self.redis.publish(f"events:{job_id}", {
                "event": "step_failed", "step": step,
                "error": error[:200], "retries": current_retries + 1,
            })
        else:
            # CAS already set it to "failed", just update DB
            await asyncio.to_thread(
                self.db.update_step, job_id, step,
                status="failed", error=error[:500],
                finished_at=datetime.now(timezone.utc),
                retries=current_retries,
            )
            await self.mark_job_failed(job_id, f"{step}: {error[:200]}")

    async def _delayed_enqueue(self, delay: int, job_id: str, step: str) -> None:
        await asyncio.sleep(delay)
        await self.enqueue_step(job_id, step)

    # DAG 推进

    async def _check_downstream(self, job_id: str) -> None:
        """检查所有 waiting/skipped 步骤是否可推进。生产路径由 on_step_done 调用。"""
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        steps = self._get_pipeline_steps(pipeline)
        statuses = await self.redis.get_all_step_statuses(job_id)

        for name, cfg in steps.items():
            status = statuses.get(name)
            if status not in ("waiting", "skipped"):
                continue

            deps = cfg.get("depends_on", [])
            if not all(statuses.get(d) in ("done", "skipped") for d in deps):
                continue

            conditional = self._step_is_conditional(cfg)
            if conditional and not await self._eval_step_condition(job_id, cfg):
                if status == "waiting":
                    await self.redis.set_step_status(job_id, name, "skipped")
                    await asyncio.to_thread(
                        self.db.update_step, job_id, name, status="skipped",
                    )
                    await self.redis.publish(f"events:{job_id}", {
                        "event": "step_skipped", "step": name,
                    })
                    statuses[name] = "skipped"
                continue

            if status == "skipped":
                if not conditional:
                    continue
                ok = await self.redis.cas_step_status(job_id, name, "skipped", "ready")
                if not ok:
                    continue
            await self.enqueue_step(job_id, name)
            statuses[name] = "ready"

        fresh = await self.redis.get_all_step_statuses(job_id)
        if fresh and all(v in ("done", "skipped") for v in fresh.values()):
            await self.mark_job_done(job_id)
        elif fresh:
            # 死锁打破器:仅当剩余未完成步骤全部为 ready(无 running、无 waiting)才介入。
            not_done = {k: v for k, v in fresh.items() if v not in ("done", "skipped")}
            all_remaining_ready = bool(not_done) and all(
                v == "ready" for v in not_done.values()
            )
            if all_remaining_ready:
                pipeline = await self.redis.get_job_pipeline(job_id)
                if pipeline:
                    steps_cfg = self._get_pipeline_steps(pipeline)
                    pool_ok: dict[str, bool] = {}  # 同 pool 只查一次,免逐步重复扫 worker
                    for step_name in not_done:
                        pool = steps_cfg.get(step_name, {}).get("pool", "")
                        if pool not in pool_ok:
                            pool_ok[pool] = await self._pool_has_workers(pool)
                        if pool_ok[pool]:
                            continue
                        # 缺 worker 只 skip 条件步(可选步缺能力=合理跳过);必需步不 skip,留给
                        # check_no_worker 超宽限 fail-fast——避免末端必需步被静默 skip 后 job
                        # 不完整却显示完成(对齐 pools.yaml fail-fast 注释)。
                        if not self._step_is_conditional(steps_cfg.get(step_name, {})):
                            continue
                        # CAS 保护 ready→skipped:若该步骤刚被 worker 抢成 running,
                        # CAS 失败 → 放弃 skip,避免覆盖在途执行。
                        if not await self.redis.cas_step_status(
                            job_id, step_name, "ready", "skipped"
                        ):
                            continue
                        logger.info(
                            "skip_no_worker", job_id=job_id,
                            step=step_name, pool=pool,
                        )
                        await asyncio.to_thread(
                            self.db.update_step, job_id, step_name, status="skipped",
                        )
                        await self.redis.publish(f"events:{job_id}", {
                            "event": "step_skipped", "step": step_name,
                            "reason": f"no workers in pool '{pool}'",
                        })
                    fresh2 = await self.redis.get_all_step_statuses(job_id)
                    if fresh2 and all(v in ("done", "skipped") for v in fresh2.values()):
                        await self.mark_job_done(job_id)

    async def enqueue_step(self, job_id: str, step_name: str) -> None:
        pipeline_steps = await self._get_job_pipeline_steps(job_id)
        if not pipeline_steps:
            return
        step_cfg = pipeline_steps.get(step_name)
        if not step_cfg:
            return

        await self.redis.set_step_status(job_id, step_name, "ready")
        pool = step_cfg["pool"]

        static_tags = step_cfg.get("tags", [])
        if pool == "ai":
            job_info = await self.redis.get_job_info(job_id)
            domain = job_info.get("domain", "")
            style_tags = parse_style_tags(job_info.get("style_tags", "[]"))
            dynamic_tags = [domain] + style_tags
            merged_tags = sorted(set(static_tags + [t for t in dynamic_tags if t]))
        else:
            merged_tags = list(static_tags)

        # 网络区域路由(net-zone):任务分发时按 URL 判区域,require 对应 net-cn / net-global tag;
        # 只有自报覆盖该区域的 worker 才能认领(境外→香港/带代理 worker;大陆→大陆 worker)。
        # 代理 HOW 全在 worker。区域判定与 tag 语义见文件头 _NET_STEPS 注释与 shared.net_zone。
        nr = self.config.net_routing or {}
        net_steps = set(nr.get("net_steps") or _NET_STEPS)
        require_tags = list(static_tags)
        if step_name in net_steps:
            info = job_info if pool == "ai" else await self.redis.get_job_info(job_id)
            src = (info.get("source") or "").strip() or detect_source(info.get("url", ""))
            zone = required_zone(src, info.get("url", ""))   # net-cn / net-global
            merged_tags = sorted(set(merged_tags + [zone]))
            require_tags = sorted(set(require_tags + [zone]))   # 硬门控:worker 须覆盖该区域

        statuses = await self.redis.get_all_step_statuses(job_id)
        done_count = sum(1 for v in statuses.values() if v in ("done", "skipped"))
        # zpopmin:分数越小越先出。priority=-done_count 让晚到步骤优先,但 02_whisper 处在链路早段
        # 会被各 job 的视觉步(04/05/06,同 cpu 池)长期抢占而饿死。给它静态加权(更小分数)抢先转写;
        # 出稿硬依赖转写,不加权会让出稿步空等。
        priority = -done_count - _PRIORITY_BOOST.get(step_name, 0)

        await self.redis.enqueue_step(
            pool, job_id, step_name, merged_tags, priority,
            require_tags=require_tags,
            resources=step_cfg.get("resources") or [],
        )

        await asyncio.to_thread(
            self.db.update_step, job_id, step_name, status="ready",
        )
        await self.redis.publish(f"events:{job_id}", {
            "event": "step_ready", "step": step_name,
        })

        logger.info("step_enqueued", job_id=job_id, step=step_name, pool=pool, priority=priority)

    async def _list_job_files(self, job_id: str) -> list[str]:
        """列出 job 现有产物的相对路径。分布式部署产物在对象存储(MinIO)、不在调度器本地盘,
        故优先走 storage;无 storage(单机/测试)回退本地 jobs_dir。条件/规则据此判存在。"""
        if self.storage is not None:
            try:
                return await self.storage.list_files(job_id)
            except Exception:
                logger.warning("list_job_files_failed", job_id=job_id)
                return []
        job_dir = self.jobs_dir / job_id

        def _local() -> list[str]:
            if not job_dir.exists():
                return []
            return [p.relative_to(job_dir).as_posix() for p in job_dir.rglob("*") if p.is_file()]

        return await asyncio.to_thread(_local)

    async def check_condition(self, job_id: str, condition: str) -> bool:
        files = await self._list_job_files(job_id)
        has_srt = any(fnmatch.fnmatch(f, "input/*.srt") for f in files)
        has_ass = any(fnmatch.fnmatch(f, "input/*.ass") for f in files)
        if condition == "no_subtitle":
            return not has_srt
        if condition == "has_subtitle":
            return has_srt
        if condition == "has_danmaku":
            return has_ass
        return True

    def _step_is_conditional(self, cfg: dict) -> bool:
        """step 是否带跳过条件:condition 字符串或声明式 rules 均算。"""
        return bool(cfg.get("condition") or cfg.get("rules"))

    async def _eval_step_condition(self, job_id: str, cfg: dict) -> bool:
        """求值 step 是否应运行:优先 condition 字符串,否则用声明式 rules。"""
        condition = cfg.get("condition")
        if condition:
            return await self.check_condition(job_id, condition)
        rules = cfg.get("rules")
        if rules:
            return await self._eval_rules(job_id, rules)
        return True

    async def _eval_rules(self, job_id: str, rules: list) -> bool:
        """声明式 rules 求值器:自上而下首条命中生效,命中 when=skip 则跳过,
        支持 exists(相对 job 根的 glob)与 if_flag(投递开关),无命中默认运行。
        存在性查 storage(产物在 MinIO,不在调度器本地盘);if_flag 查 redis job info。"""
        files = await self._list_job_files(job_id)
        _flags_cache: dict | None = None

        async def _flags() -> dict:
            nonlocal _flags_cache
            if _flags_cache is None:
                info = await self.redis.get_job_info(job_id)
                try:
                    _flags_cache = json.loads(info.get("flags") or "{}")
                except (json.JSONDecodeError, ValueError, AttributeError):
                    _flags_cache = {}
            return _flags_cache

        def _when(rule: dict) -> str:
            when = rule.get("when", "on")
            if when is True:
                return "on"
            if when is False:
                return "skip"
            return str(when)

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            glob = rule.get("exists")
            if glob is not None:
                hit = any(fnmatch.fnmatch(f, glob) for f in files)
                if not hit:
                    continue
            # if_flag:投递开关为真才命中(假则本条不生效,落到后续兜底规则)。
            flag = rule.get("if_flag")
            if flag is not None:
                if not (await _flags()).get(flag):
                    continue
            # exists/if_flag 命中、或无条件的兜底规则:本条生效。
            return _when(rule) != "skip"
        return True

    async def _pool_has_workers(self, pool: str) -> bool:
        """检查某个 pool 是否有可认领新任务的 worker。排除 paused/offline:claim_step 对
        paused 直接拒认领,若 pool 只剩 paused,no-worker 判定/死锁打破器会误判为可推进 →
        ready 步既无人认领又不被 fail-fast/skip,永久卡 ready。
        暂停态算"无可用 worker":暂停期下载好的 job 进到该池会等候,超 NO_WORKER_GRACE_SEC 才 fail。"""
        workers = await self.redis.list_worker_ids()
        for wid in workers:
            info = await self.redis.get_worker_info(wid)
            if not info:
                continue
            if info.get("admin_status") == "paused" or info.get("status") == "offline":
                continue
            if pool in info.get("pools", "").split(","):
                return True
        return False

    async def _pool_has_workers_for(self, pool: str, require_tags: list[str]) -> bool:
        """同 _pool_has_workers,但额外要求在线 worker 的 tags 满足 require_tags(硬门控)。
        require_tags 空 → 等价 _pool_has_workers。check_no_worker 若只看池不看 tag,
        池有 worker 但无人满足 require_tags 时(如境外内容 require net-global 却无覆盖全球的
        worker)会躲过 fail-fast、永久卡 ready 且无报错;用本函数后超 NO_WORKER_GRACE_SEC
        给明确失败。"""
        req = {t for t in (require_tags or []) if t}
        if not req:
            return await self._pool_has_workers(pool)
        workers = await self.redis.list_worker_ids()
        for wid in workers:
            info = await self.redis.get_worker_info(wid)
            if not info:
                continue
            if info.get("admin_status") == "paused" or info.get("status") == "offline":
                continue
            if pool not in info.get("pools", "").split(","):
                continue
            wtags = {t for t in info.get("tags", "").split(",") if t}
            if req.issubset(wtags):
                return True
        return False

    # Job 状态

    async def mark_job_done(self, job_id: str) -> None:
        await self._sync_published_at(job_id)
        await asyncio.to_thread(
            self.db.update_job, job_id,
            status=JobStatus.DONE, progress_pct=100,
        )
        await self.redis.publish(f"events:{job_id}", {
            "event": "job_done", "progress_pct": 100,
        })
        await self.redis.remove_active_job(job_id)
        logger.info("job_done", job_id=job_id)

    async def _sync_published_at(self, job_id: str) -> None:
        """把源内容发布时间(01_download 写入 input/metadata.json 的 published_at)同步进 DB,
        供概念时间线按源内容发布时间(而非入库时间)分桶。best-effort——读不到/解析失败/无该字段
        都只记日志,绝不阻塞 job 完成。

        经 storage 读 metadata.json:分布式部署产物在对象存储(MinIO)、不在调度器本地盘,
        与 _index_job_notes/_collect_glossary 同一路径。未注入 storage(老式单机)则跳过——
        留空时概念时间线对该 job 回退到 created_at,不丢计数。"""
        if self.storage is None:
            return
        try:
            raw = await self.storage.read_file(job_id, "input/metadata.json")
            md = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            # 文章的 title/date 在 input/article_meta.json(metadata.json 常无这两项)→ 合并兜底:
            # 以 metadata.json 的非空值优先,article_meta 填补 title/date。
            am_raw = await self.storage.read_file(job_id, "input/article_meta.json")
            if am_raw:
                am = json.loads(am_raw.decode("utf-8", errors="replace"))
                md = {**am, **{k: v for k, v in md.items() if v}}
            # 论文/文章的 title/date 也在 02 解析写的 intermediate/parsed.json,论文标题尤其只在此,
            # 故作末位兜底;仍以已有非空值优先,不覆盖 metadata/article_meta 已填的。
            if not md.get("title") or not (md.get("published_at") or md.get("date")):
                pj_raw = await self.storage.read_file(job_id, "intermediate/parsed.json")
                if pj_raw:
                    pj = json.loads(pj_raw.decode("utf-8", errors="replace"))
                    md = {**{k: pj[k] for k in ("title", "date") if pj.get(k)}, **{k: v for k, v in md.items() if v}}
            if not md:
                return
            fields: dict = {}
            published = md.get("published_at") or md.get("date")
            if published:
                fields["published_at"] = published
            # 标题:01_download 从源(youtube info.json / article_meta)写入时回填——仅当 DB 标题为空,
            # 不覆盖订阅/用户已填的标题。例外:已入库标题是垃圾(pdf-only 的 PDF 内嵌 metadata,
            # 如 "10things"/"paper.dvi"/"NBER WORKING PAPER SERIES")且候选明显更像真标题
            # (非垃圾+含空格+更长)→ 允许覆盖,与 02 步提取共用 shared.titles 同一套判定。
            title = (md.get("title") or "").strip()
            if title:
                from shared.titles import is_suspicious_title
                job = await asyncio.to_thread(self.db.get_job, job_id)
                cur = (job.title or "").strip() if job else ""
                better = (is_suspicious_title(cur) and not is_suspicious_title(title)
                          and " " in title and len(title) > len(cur))
                if job and (not cur or better):
                    fields["title"] = title
            if not fields:
                return
            await asyncio.to_thread(self.db.update_job, job_id, **fields)
            logger.info("metadata_synced", job_id=job_id, **fields)
        except Exception:
            logger.warning("metadata_sync_failed", job_id=job_id)

    async def mark_job_failed(self, job_id: str, error: str) -> None:
        self._cancel_delayed_tasks(job_id)
        progress = await self._update_progress(job_id)
        await asyncio.to_thread(
            self.db.update_job, job_id,
            status=JobStatus.FAILED, error=error[:500],
        )
        await self.redis.publish(f"events:{job_id}", {
            "event": "job_failed", "error": error[:200], "progress_pct": progress,
        })
        await self.redis.push_event("job_failed", job_id=job_id, error=error[:200])
        await self.redis.remove_active_job(job_id)
        # 失败即停:清掉该 job 仍残留在 queue:{pool} 的兄弟 ready task。并行分支下某步终态失败时,
        # 其它已入队的兄弟步是死任务,job 已 FAILED 不该再跑;不清则 worker 仍会认领,cas_step_status
        # 因 steps hash 未清而成功,跑已失败 job 的步,甚至 _check_downstream 把它重标 done,还留下
        # 指向 FAILED job 的孤儿 task。保留 job:{id}:steps hash(不调 cleanup_job),重试/重跑仍可用。
        await self.redis.remove_job_tasks(job_id)
        logger.info("job_failed", job_id=job_id, error=error[:200])

    # 孤儿回收 + 卡住检测

    # 认领后到首个进度心跳之间允许的无心跳窗口。尤其 gateway worker 拉大源文件(source.mp4)
    # 的 pull 阶段:子进程未起、on_tick 未触发,progress_at 为 None。取 120s 覆盖慢链路拉取,
    # 避免 pull 中的步被误判 claim lost 回收(实测误判会让 03_scene 等步雪崩)。真丢认领最迟
    # 120s 回收(罕见,可接受)。开 STORAGE_WORKDIR_REUSE 后 pull 近乎瞬时、基本不触发。env 可调。
    _CLAIM_MISMATCH_GRACE_SEC = int(os.environ.get("CLAIM_MISMATCH_GRACE_SEC", "120"))
    # 判"这步是否有人在跑"用每步独立的进度心跳新鲜度(worker on_tick 每 10s 刷一步)。
    # 30s(≈3 拍)留余量,避免扫描时序抖动误判正在跑的步。
    _STEP_PROGRESS_FRESH_SEC = 30

    async def orphan_scan(self) -> None:
        active_jobs = await self.redis.get_active_jobs()
        live_mismatch: set[tuple[str, str]] = set()
        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            for step, status in statuses.items():
                if status != "running":
                    continue
                worker_id = await self.redis.get_step_worker(job_id, step)
                if not worker_id:
                    await self._reclaim_step(job_id, step, "no worker assigned")
                    continue
                if not await self.redis.worker_exists(worker_id):
                    await self._reclaim_step(job_id, step, f"worker {worker_id} lost")
                    continue
                # worker 存活,但这步没有近期进度心跳 → 认领响应丢失/未真正运行,实际没人在跑。
                # 判活用每步独立的进度心跳(job:*:step_progress,worker on_tick 每 10s 刷一步),
                # 而非 worker 的单个 current_step——后者在 concurrency>1 时只能反映 N 个并发步中的 1 个,
                # 会把其余并发步全误判为 claim lost 反复回收(并发越高越严重,实测会致失败雪崩)。
                # 持续超宽限期(容忍认领后首拍心跳延迟)才回收。
                progress_at = await self.redis.get_step_progress_at(job_id, step)
                if progress_at is not None and time.time() - progress_at < self._STEP_PROGRESS_FRESH_SEC:
                    self._claim_mismatch_since.pop((job_id, step), None)
                    continue
                key = (job_id, step)
                live_mismatch.add(key)
                first = self._claim_mismatch_since.setdefault(key, time.time())
                if time.time() - first >= self._CLAIM_MISMATCH_GRACE_SEC:
                    self._claim_mismatch_since.pop(key, None)
                    await self._reclaim_step(
                        job_id, step,
                        f"worker {worker_id} not running this step (claim lost?)",
                    )
        # 清理不再 mismatch 的计时,避免泄漏。
        for k in [k for k in self._claim_mismatch_since if k not in live_mismatch]:
            self._claim_mismatch_since.pop(k, None)

    async def _reclaim_step(
        self, job_id: str, step: str, reason: str,
    ) -> None:
        logger.warning("reclaim_step", job_id=job_id, step=step, reason=reason)
        await self.redis.push_event("orphan_reclaimed", job_id=job_id, step=step, reason=reason)

        # holder 集合:按本步 holder(=exec_id)SREM 释放其占的池槽/资源槽。SREM 幂等——即便 worker
        # 仍存活、它自己的 release_step 也 SREM 同一 holder,双方都安全(不双减)。故 reclaim 一律按
        # holder 释放:死 worker 的槽必被回收,不泄漏;活 worker 重复释放也无害。
        holder = await self.redis.get_step_exec_id(job_id, step)
        if holder:
            pipeline_steps = await self._get_job_pipeline_steps(job_id)
            if pipeline_steps:
                pool = pipeline_steps.get(step, {}).get("pool")
                if pool:
                    await self.redis.release_slot(pool, holder)
            resources = await self.redis.get_step_resources(job_id, step)
            if resources:
                for res in resources:
                    await self.redis.release_resource(res, holder)
                await self.redis.clear_step_resources(job_id, step)

        await self.redis.publish("step_failed", {
            "job_id": job_id, "step": step, "status": "failed",
            "error": f"orphan reclaimed: {reason}",
            "error_type": "processing",
        })

    async def reconcile_slots(self) -> None:
        """周期对账并发槽:持有 holder(=exec_id)但不属于任何 running 步的 = 泄漏(worker 突死没 release_step、
        删 running job 漏放、占槽后死在写状态前等)。SCARD 是真实占用,但这些陈旧 holder 仍占名额 → 清掉收敛。
        宽限:仅连续两拍(2×30s)都陈旧才 SREM,避开"刚占槽、还没写 running 状态"的认领窗口被误清。"""
        try:
            held = await self.redis.get_all_holders()
            if not held:
                self._slot_reconcile_suspect = set()
                return
            # live = 当前所有 running 步的 exec_id(= 合法持有者)。
            live: set[str] = set()
            for job_id in await self.redis.get_active_jobs():
                statuses = await self.redis.get_all_step_statuses(job_id)
                for step, status in statuses.items():
                    if status == "running":
                        ex = await self.redis.get_step_exec_id(job_id, step)
                        if ex:
                            live.add(ex)
            suspects = held - live
            confirmed = suspects & self._slot_reconcile_suspect   # 连续两拍都陈旧 → 真泄漏
            if confirmed:
                n = await self.redis.release_holders(confirmed)
                logger.info("slots_reconciled", removed=n, holders=sorted(confirmed)[:10])
            self._slot_reconcile_suspect = suspects
        except Exception:
            logger.exception("reconcile_slots_error")

    async def check_stuck(self) -> None:
        # 进度停滞检测:本地 job 读 jobs_dir/.{step}.progress(worker _progress_monitor 写其
        # work_dir;单机 LocalStorage 下 work_dir==jobs_dir 才可见)。远程 job(Gateway/Remote
        # 存储,work_dir 是 worker 本地 tmp、不落调度器盘)退回读 redis 步进度心跳——由 worker
        # on_tick 每 10s(仅子进程存活时)经 set_step_progress_at 刷新。
        active_jobs = await self.redis.get_active_jobs()
        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            for step, status in statuses.items():
                if status != "running":
                    continue
                progress_file = self.jobs_dir / job_id / f".{step}.progress"
                if progress_file.exists():
                    try:
                        data = json.loads(await asyncio.to_thread(progress_file.read_text))
                    except (json.JSONDecodeError, OSError):
                        continue
                    latest = max(
                        filter(None, [data.get("updated_at"), data.get("worker_heartbeat_at")]),
                        default=None,
                    )
                else:
                    # 远程 job:退回 redis 步进度心跳(无文件且无心跳=刚起步/未上报,跳过)。
                    latest = await self.redis.get_step_progress_at(job_id, step)
                if latest is None:
                    continue
                age = time.time() - latest
                # 180s:worker 心跳每 10s(best-effort 走 gateway),但 api recreate/网络抖动可断 1-2 分钟,
                # 60s 一次部署就误杀在跑的步(线上 04_translate 被 "stale 71s" 杀过);真卡死 180s 内回收仍可接受。
                if age > 180:
                    logger.warning(
                        "step_stuck", job_id=job_id, step=step, age_sec=round(age),
                    )
                    await self.redis.push_event("step_stuck", job_id=job_id, step=step, stalled_sec=round(age))
                    # 主动告警(设了 ALERT_WEBHOOK_URL 才外发;best-effort,不阻塞调度循环)。
                    await asyncio.to_thread(
                        notify, "step_stuck",
                        f"job {job_id} 的 {step} 进度停滞 {age:.0f}s,worker 可能卡死,已触发重试",
                        job_id=job_id, step=step, age_sec=round(age),
                    )
                    await self.redis.publish("step_failed", {
                        "job_id": job_id, "step": step, "status": "failed",
                        "error": f"progress stale ({age:.0f}s, worker process may be stuck)",
                        "error_type": "timeout",
                    })

    # 默认 90s 宽限即 fail-fast(无可用 worker 的 job)。可经 env 调大,用于
    # "只跑部分 worker"的运维窗口(如夜间仅 ECS download worker,其余步骤明天再跑),
    # 避免下载完的 job 因缺 scene/cpu/ai worker 被误判失败。
    _NO_WORKER_GRACE_SEC = int(os.environ.get("NO_WORKER_GRACE_SEC", "90"))

    async def check_no_worker(self) -> None:
        """无法推进的 job 持续超宽限期则 fail-fast,避免永久卡住。

        判定:无 running 步,且所有 ready 步所在 pool 都无在线 worker——
        典型是未部署 gpu worker 时 audio 的 02_whisper 卡在 queue:gpu。
        给出明确错误而非静默挂起;宽限期容忍 worker 短暂重启。
        """
        active_jobs = await self.redis.get_active_jobs()
        for job_id in active_jobs:
            statuses = await self.redis.get_all_step_statuses(job_id)
            if not statuses or any(v == "running" for v in statuses.values()):
                self._no_worker_since.pop(job_id, None)
                continue
            ready = [s for s, v in statuses.items() if v == "ready"]
            if not ready:
                self._no_worker_since.pop(job_id, None)
                continue

            pipeline = await self.redis.get_job_pipeline(job_id)
            steps_cfg = self._get_pipeline_steps(pipeline) if pipeline else {}
            stuck: list[tuple[str, str]] = []
            progressable = False
            nr = self.config.net_routing or {}
            net_steps = set(nr.get("net_steps") or _NET_STEPS)
            job_src: str | None = None  # 懒查 job 来源(仅 net_steps 需要)
            job_url: str = ""
            pool_ok: dict[tuple, bool] = {}  # 按 (pool, require_tags) 缓存:同池不同门控要分别判
            for step in ready:
                pool = steps_cfg.get(step, {}).get("pool", "")
                # 重算该 step 的 require_tags,与 enqueue_step 同逻辑:net-zone 按 URL 区域。
                req: list[str] = []
                if step in net_steps:
                    if job_src is None:
                        jinfo = await self.redis.get_job_info(job_id)
                        job_url = jinfo.get("url", "")
                        job_src = (jinfo.get("source") or "").strip() or detect_source(job_url)
                    req.append(required_zone(job_src, job_url))
                key = (pool, frozenset(req))
                if key not in pool_ok:
                    pool_ok[key] = await self._pool_has_workers_for(pool, req)
                if pool_ok[key]:
                    progressable = True
                    break
                stuck.append((step, pool))
            if progressable or not stuck:
                self._no_worker_since.pop(job_id, None)
                continue

            first = self._no_worker_since.setdefault(job_id, time.time())
            if time.time() - first < self._NO_WORKER_GRACE_SEC:
                continue
            waited = round(time.time() - first)
            self._no_worker_since.pop(job_id, None)
            pairs = ", ".join(f"{s}(pool '{p}')" for s, p in stuck)
            logger.warning("job_no_worker", job_id=job_id, stuck=pairs)
            await self.redis.push_event(
                "no_worker", job_id=job_id, step=stuck[0][0], pool=stuck[0][1], waited_sec=waited)
            await self.mark_job_failed(job_id, f"无可用 worker 执行步骤: {pairs}")

        # 清理已离开 active 集合的计时,避免泄漏。
        active_set = set(active_jobs)
        for jid in [j for j in self._no_worker_since if j not in active_set]:
            self._no_worker_since.pop(jid, None)

    # 重跑 / 重提交

    async def _retry_failed(self, job_id: str) -> None:
        """重试失败 Job:从第一个 failed 步骤开始重跑。"""
        statuses = await self.redis.get_all_step_statuses(job_id)
        failed_steps = [s for s, st in statuses.items() if st == "failed"]
        if not failed_steps:
            return
        first_failed = sorted(failed_steps)[0]
        await self.rerun(job_id, first_failed)
        logger.info("job_retry", job_id=job_id, from_step=first_failed)

    async def rerun(self, job_id: str, from_step: str) -> list[str]:
        """从指定步骤开始重跑,清除该步骤及所有下游的 .done 标记。返回被重置的步骤列表。"""
        self._cancel_delayed_tasks(job_id)  # 取消在途延迟重试,防与新一轮状态串台
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return []
        steps = self._get_pipeline_steps(pipeline)
        downstream = self._get_downstream(steps, from_step)
        reset_steps = [from_step] + downstream

        for step in reset_steps:
            done_file = self.jobs_dir / job_id / f".{step}.done"
            await asyncio.to_thread(done_file.unlink, True)
            # 中心存储的 .done 必须一并删:MinIO 部署下 .done 在 bucket,只删本地是 no-op →
            # worker pull 回旧 .done 指纹命中直接跳过,rerun/「重跑该步」整体失效。
            # best-effort:删失败只告警不挡主流程(兜底=改步 version 失效指纹)。
            if self.storage is not None:
                try:
                    await self.storage.delete_file(job_id, f".{step}.done")
                except Exception:
                    logger.warning("rerun_central_done_delete_failed",
                                   job_id=job_id, step=step, exc_info=True)
            await self.redis.set_step_status(job_id, step, "waiting")
            # 清重试计数,否则重跑曾耗尽重试的步骤会零重试预算、首次失败即终止。
            await self.redis.reset_step_retries(job_id, step)
            await asyncio.to_thread(
                self.db.update_step, job_id, step,
                # 清掉上一轮的起止/耗时,否则重置成 waiting 的步骤会显示旧时间(诡异)。
                status="waiting", error=None,
                started_at=None, finished_at=None, duration_sec=None,
            )

        # 刷新术语快照:P3 修复路径 = 人工定准 glossary.zh_name 后 rerun 04,必须让新表生效。
        job = await asyncio.to_thread(self.db.get_job, job_id)
        if job:
            await self._export_term_map(job)

        await asyncio.to_thread(
            self.db.update_job, job_id, status=JobStatus.PROCESSING,
        )
        await self.redis.add_active_job(job_id)
        await self._check_downstream(job_id)

        logger.info("job_rerun", job_id=job_id, from_step=from_step, reset=reset_steps)
        return reset_steps

    async def resubmit(self, job_id: str) -> None:
        """按当前 pipelines.yaml 重新初始化步骤,保留已有步骤的状态。

        以当前 pipeline 为准对齐 redis 与 DB 两侧:删去 pipeline 不再有的步(两侧都删)、
        补齐新步、并把每个步在 redis/DB 写到同一状态。不变量:redis 与 DB 步集一致——
        删旧步若只删 redis 不删 DB,或用 redis existing 当判据跳过 DB 回填,renumber/改
        pipeline 后流水线读 DB 会显示旧步、与实际执行的 redis 分叉。"""
        self.reload_config()

        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return
        steps = self._get_pipeline_steps(pipeline)
        # 状态真源:redis(运行态)优先,redis 无则用 DB,都无则 waiting——保留已完成/已跑步骤状态。
        existing = await self.redis.get_all_step_statuses(job_id)
        db_status = {
            s.name: (s.status.value if isinstance(s.status, StepStatus) else s.status)
            for s in await asyncio.to_thread(self.db.get_steps, job_id)
        }

        # 删去当前 pipeline 不再有的步:redis 与 DB 都删,否则 DB 残留旧步。
        for name in (set(existing) | set(db_status)) - set(steps):
            await self.redis.delete_step_status(job_id, name)
            await asyncio.to_thread(self.db.delete_step, job_id, name)

        # 当前 pipeline 的每个步:取已有状态(缺则 waiting),redis 与 DB 都对齐到该状态。
        # DB 侧:已有行只在状态变化时 update_step(status=)——不能 upsert_step 整行替换,
        # 否则会抹掉已完成步的 started_at/finished_at/duration/input_hash(流水线显示无时间);
        # 仅 DB 缺该步(分叉)时才 upsert_step 新建。
        for name, cfg in steps.items():
            status = existing.get(name) or db_status.get(name) or "waiting"
            await self.redis.set_step_status(job_id, name, status)
            if name in db_status:
                if db_status[name] != status:
                    await asyncio.to_thread(
                        self.db.update_step, job_id, name, status=StepStatus(status),
                    )
            else:
                await asyncio.to_thread(
                    self.db.upsert_step,
                    Step(job_id=job_id, name=name, status=StepStatus(status), pool=cfg["pool"]),
                )

        await asyncio.to_thread(
            self.db.update_job, job_id, status=JobStatus.PROCESSING,
        )
        await self.redis.add_active_job(job_id)
        await self._check_downstream(job_id)

        logger.info("job_resubmit", job_id=job_id, pipeline=pipeline)

    def reload_config(self) -> None:
        self.config = load_config(
            config_dir=self.config.config_dir,
            data_dir=self.config.data_dir,
        )
        logger.info("config_reloaded")

    # 内部工具

    def _get_pipeline_steps(self, pipeline: str) -> dict[str, dict]:
        steps_list = self.config.pipelines.get(pipeline, {}).get("steps", [])
        return {s["name"]: s for s in steps_list}

    async def _get_job_pipeline_steps(self, job_id: str) -> dict[str, dict] | None:
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return None
        return self._get_pipeline_steps(pipeline)

    def _get_downstream(self, steps: dict[str, dict], from_step: str) -> list[str]:
        """递归获取 from_step 的所有下游步骤。"""
        dependents: dict[str, list[str]] = {}
        for name, cfg in steps.items():
            for dep in cfg.get("depends_on", []):
                dependents.setdefault(dep, []).append(name)

        result = []
        q = deque(dependents.get(from_step, []))
        visited = set()
        while q:
            s = q.popleft()
            if s in visited:
                continue
            visited.add(s)
            result.append(s)
            q.extend(dependents.get(s, []))
        return result

    def _calc_progress(self, steps_config: list[dict], statuses: dict[str, str]) -> int:
        done_weight = sum(
            s.get("weight", 1) for s in steps_config
            if statuses.get(s["name"]) in ("done", "skipped")
        )
        total_weight = sum(s.get("weight", 1) for s in steps_config)
        return round(100 * done_weight / max(total_weight, 1))

    async def _update_progress(self, job_id: str) -> int:
        pipeline = await self.redis.get_job_pipeline(job_id)
        if not pipeline:
            return 0
        steps_config = self.config.pipelines.get(pipeline, {}).get("steps", [])
        statuses = await self.redis.get_all_step_statuses(job_id)
        progress = self._calc_progress(steps_config, statuses)
        await asyncio.to_thread(
            self.db.update_job, job_id, progress_pct=progress,
        )
        return progress
