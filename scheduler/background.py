"""调度器内部职责组件,通过显式 Scheduler facade 协作。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import structlog

from shared.status import OFFLINE, PAUSED
from shared.version import FLORI_VERSION

if TYPE_CHECKING:
    from scheduler.scheduler import Scheduler


logger = structlog.get_logger(component="scheduler")

# 延迟重试任务的 name 前缀,跟踪/按 job 取消时复用,避免格式漂移。
_DELAYED_PREFIX = "delayed_enqueue:"

class BackgroundServices:
    """封装单一调度职责,跨职责调用经 Scheduler 显式 facade。"""

    def __init__(self, owner: Scheduler):
        self.owner = owner

    async def run(self) -> None:
        logger.info("scheduler_start")
        self.owner._started_at_iso = datetime.now(timezone.utc).isoformat()
        await self.owner._publish_resource_limits()
        # 下载凭证镜像重灌:redis 卷重建/清库后 cred:* 丢,DB 是持久源(docs/03 §1.7.1)。
        try:
            from shared.credentials import mirror_all_from_db
            await mirror_all_from_db(self.owner.redis, self.owner.db)
        except Exception:
            logger.warning("credential_mirror_failed")
        await self.owner._recover()
        self.owner._stream_task = asyncio.create_task(self.owner._event_loop())
        self.owner._pubsub_task = asyncio.create_task(self.owner._notification_loop())
        self.owner._periodic_task = asyncio.create_task(self.owner._periodic_loop())
        self.owner._heartbeat_task = asyncio.create_task(self.owner._heartbeat_loop())
        try:
            await asyncio.gather(
                self.owner._pubsub_task,
                self.owner._stream_task,
                self.owner._periodic_task,
                self.owner._heartbeat_task,
            )
        except asyncio.CancelledError:
            logger.info("scheduler_cancelled")

    async def _publish_resource_limits(self) -> None:
        """把 configs/resources.yaml 的资源上限刷进 redis(单一事实源),供 claim_step 读。
        资源上限改动需重启 scheduler 才重推(资源集稳定,极少改)。"""
        await self.owner.redis.set_resource_limits(self.owner.config.resources or {})

    async def shutdown(self) -> None:
        logger.info("scheduler_shutdown")
        self.owner._shutdown = True
        if self.owner._pubsub_task and not self.owner._pubsub_task.done():
            self.owner._pubsub_task.cancel()
        if self.owner._stream_task and not self.owner._stream_task.done():
            self.owner._stream_task.cancel()
        if self.owner._periodic_task and not self.owner._periodic_task.done():
            self.owner._periodic_task.cancel()
        if self.owner._heartbeat_task and not self.owner._heartbeat_task.done():
            self.owner._heartbeat_task.cancel()
        pending = [t for t in self.owner._delayed_tasks if not t.done()]
        pending.extend(
            task for task in self.owner._concept_synthesis_tasks.values()
            if not task.done()
        )
        self.owner._concept_synthesis_pending.clear()
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _on_delayed_done(self, task: asyncio.Task) -> None:
        """延迟任务完成回调:从跟踪集合移除;非取消的真异常上报。"""
        self.owner._delayed_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "delayed_enqueue_failed",
                task=task.get_name(), exc_info=task.exception(),
            )

    def _cancel_delayed_tasks(self, job_id: str) -> None:
        """取消某 job 在途的延迟重试任务(rerun / job 失败时调用)。"""
        prefix = f"{_DELAYED_PREFIX}{job_id}:"
        for t in list(self.owner._delayed_tasks):
            if t.get_name().startswith(prefix) and not t.done():
                t.cancel()

    async def _event_loop(self) -> None:
        """消费 durable lifecycle Stream;成功后 ACK,失败留 PEL 重领。"""
        consumer = f"scheduler-{os.getpid()}"
        backoff = 1
        while not self.owner._shutdown:
            try:
                messages = await self.owner.redis.read_lifecycle_events(consumer)
                backoff = 1
                for message_id, fields in messages:
                    try:
                        topic = fields.get("topic")
                        payload = json.loads(fields.get("payload", ""))
                        if not isinstance(topic, str) or not isinstance(payload, dict):
                            raise ValueError("invalid lifecycle envelope")
                        payload["_stream_id"] = message_id
                        await self.owner._dispatch(payload)
                    except asyncio.CancelledError:
                        raise
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        await self.owner.redis.reject_lifecycle_event(
                            message_id, fields, f"invalid envelope: {exc}", max_attempts=1,
                        )
                        logger.warning("lifecycle_poison_isolated", message_id=message_id)
                    except Exception as exc:
                        isolated = await self.owner.redis.reject_lifecycle_event(
                            message_id, fields, str(exc),
                        )
                        logger.exception(
                            "lifecycle_handler_error",
                            message_id=message_id,
                            poison_isolated=isolated,
                        )
                    else:
                        await self.owner.redis.ack_lifecycle_event(message_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.owner._shutdown:
                    break
                logger.exception("event_loop_reconnect", backoff=backoff)
                # 重连前先尝试重建底层连接 + 补推可能漏掉的事件
                try:
                    await self.owner.redis.reconnect()
                    await self.owner._recover()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("event_loop_recover_failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _notification_loop(self) -> None:
        """Pub/Sub 只承载可丢的 step_started 展示通知。"""
        async for msg in self.owner.redis.subscribe("step_started"):
            if self.owner._shutdown:
                return
            try:
                await self.owner._dispatch(msg)
            except Exception:
                logger.exception("notification_handler_error", msg=msg)

    async def _dispatch(self, msg: dict) -> None:
        status = msg.get("status")
        command = msg.get("command") or msg.get("action")
        stream_id = msg.get("_stream_id")

        if status == "running":
            await self.owner.on_step_started(
                msg["job_id"], msg["step"], worker=msg.get("worker"),
            )
        elif status == "done":
            await self.owner.on_step_done(
                msg["job_id"], msg["step"],
                duration=msg.get("duration"),
                worker=msg.get("worker"),
                exec_id=msg.get("exec_id"),
                generation=msg.get("generation"),
                started_at=msg.get("started_at"),
            )
        elif status == "failed":
            await self.owner.on_step_failed(
                msg["job_id"], msg["step"],
                msg.get("error", ""),
                msg.get("error_type", "unknown"),
                worker=msg.get("worker"),
                exec_id=msg.get("exec_id"),
                generation=msg.get("generation"),
                duration=msg.get("duration"),
                started_at=msg.get("started_at"),
                count_stats=bool(msg.get("count_stats", False)),
            )
        elif command == "new_job":
            job = await asyncio.to_thread(self.owner.db.get_job, msg["job_id"])
            if job:
                await self.owner.submit_job(job)
        elif command == "rerun":
            await self.owner.rerun(
                msg["job_id"], msg["from_step"], idempotency_key=stream_id,
            )
        elif command == "resubmit":
            await self.owner.resubmit(msg["job_id"], idempotency_key=stream_id)
        elif command == "retry":
            await self.owner._retry_failed(msg["job_id"], idempotency_key=stream_id)
        elif command == "delete":
            # 消费 delete_job 端点的 publish,完成 job 编排状态收尾:取消在途重试,
            # 移出 active_jobs,清五个 Redis 编排 hash(job:{id}/steps/retries/step_worker/step_exec).
            # 否则删在途 processing job 后这些键泄漏,幽灵 job 被 orphan_scan/check_no_worker/
            # check_stuck 周期空扫,迟到的 on_step_done 还可能 CAS 推进已删 job。
            job_id = msg["job_id"]
            self.owner._cancel_delayed_tasks(job_id)
            await self.owner.redis.remove_active_job(job_id)
            await self.owner.redis.cleanup_job(job_id)
            # 清队列里该 job 尚未认领的排队 task(queue:{pool}+queue:enqueued)。
            # API 删除路径已同步清过;此处兜底 CLI/其它经 pubsub 发起的删除。幂等。
            await self.owner.redis.remove_job_tasks(job_id)
            logger.info("job_deleted_cleanup", job_id=job_id)

    async def _periodic_loop(self) -> None:
        while not self.owner._shutdown:
            # 实测本拍与上一拍的间隔,超出期望(30s)的部分=loop_lag(循环被拖慢的信号);
            # 心跳把它带给 /api/status 的 scheduler 组件(>5s 叠加 degraded)。
            now = time.monotonic()
            if self.owner._last_tick is not None:
                self.owner._last_loop_lag = max(
                    0.0, (now - self.owner._last_tick) - self.owner._PERIODIC_INTERVAL_SEC,
                )
            self.owner._last_tick = now
            try:
                await self.owner.orphan_scan()
                await self.owner.check_stuck()
                await self.owner.check_no_worker()
                await self.owner.cleanup_stale_workers()
                await self.owner.reconcile_slots()
                await self.owner.reconcile_completion_effects()
                await self.owner.reconcile_study_suggestion_batches()
                await self.owner.check_radar_digest()
            except Exception:
                logger.exception("periodic_error")
            await asyncio.sleep(self.owner._PERIODIC_INTERVAL_SEC)

    async def _heartbeat_loop(self) -> None:
        """每 ~10s 写 component:scheduler 心跳(<online_window/3,容忍丢 2 拍仍 up)。
        瞬态 redis 抖动不中断循环:记日志后续跑,下一拍重写;丢几拍由 stale 窗口容忍。"""
        while not self.owner._shutdown:
            try:
                await self.owner.redis.set_component_heartbeat("scheduler", {
                    "version": FLORI_VERSION,
                    "started_at": self.owner._started_at_iso,
                    "loop_lag_sec": round(self.owner._last_loop_lag, 2),
                    "loop_interval_sec": self.owner._PERIODIC_INTERVAL_SEC,
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
            ws = (self.owner.config.pools.get("worker_status") or {}) if self.owner.config else {}
            timeout_sec = int(ws.get("stale_window_sec", 900))
        workers = await asyncio.to_thread(self.owner.db.list_workers)
        now = datetime.now(timezone.utc)
        for w in workers:
            hb = w.last_heartbeat
            stale = hb is None or (now - hb) > timedelta(seconds=timeout_sec)
            if not stale:
                continue
            alive = await self.owner.redis.worker_exists(w.id)
            if alive:
                # list_workers 已按心跳新鲜度衍生公共状态,故此处直接持久化(幂等),
                # 不能用 w.status 判断是否需要写。
                await asyncio.to_thread(
                    self.owner.db.set_worker_status, w.id, OFFLINE,
                )
            elif w.admin_status == PAUSED:
                await asyncio.to_thread(self.owner.db.set_worker_status, w.id, OFFLINE)
                logger.info("paused_worker_preserved", worker_id=w.id)
            else:
                await asyncio.to_thread(self.owner.db.delete_worker, w.id)
                await self.owner.redis.push_event("worker_cleaned", worker_id=w.id)
                logger.info("worker_cleaned", worker_id=w.id)
