"""Worker：从资源池队列自取任务，执行步骤脚本，上报结果。

worker 只依赖 WorkerTransport(协调/状态后端)与 StorageBackend(产物),不直连
redis/db。P0-A 注入 RedisTransport(零行为变化);P1 起可换 GatewayTransport。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from shared.ai_gateway import collect_usage_from_file
from shared.config import AppConfig, build_step_config
from shared.models import generate_worker_id
from shared.storage import StorageBackend
from worker.step_runner import StepContext, create_step_runner
from worker.transport import WorkerTransport

logger = structlog.get_logger(component="worker")

WORKER_POOLS: dict[str, list[str]] = {
    "download": ["io"],
    "cpu": ["scene", "cpu", "io"],
    "ai": ["ai", "io"],
    "gpu": ["gpu", "scene", "cpu", "io"],
}


def auto_discover_tags() -> set[str]:
    tags = set()
    if os.environ.get("ANTHROPIC_API_KEY"):
        tags.add("vision")
    if shutil.which("claude"):
        tags.update(["vision", "claude-cli"])
    if os.environ.get("DEEPSEEK_API_KEY"):
        tags.add("text-only")
    if os.path.exists("/usr/bin/nvidia-smi"):
        tags.add("gpu")
    if os.environ.get("OLLAMA_URL"):
        tags.add("local")
    return tags


class Worker:
    def __init__(
        self,
        transport: WorkerTransport,
        config: AppConfig,
        storage: StorageBackend,
        worker_type: str,
        pools: list[str],
        tags: set[str],
        reject_tags: set[str],
    ):
        self.transport = transport
        self.config = config
        self.storage = storage
        self.worker_type = worker_type
        self.worker_id = generate_worker_id(worker_type)
        self.pools = pools
        self.tags = tags
        self.reject_tags = reject_tags
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "0"))
        self._shutdown = False
        self.runner = create_step_runner(self.worker_id)

    # ── 生命周期 ──

    async def run(self) -> None:
        await self.register()
        logger.info(
            "worker_start", worker_id=self.worker_id,
            type=self.worker_type, pools=self.pools,
            tags=sorted(self.tags), reject_tags=sorted(self.reject_tags),
        )
        try:
            await asyncio.gather(
                self.heartbeat_loop(),
                self.main_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.transport.update_status(self.worker_id, "offline")
            logger.info("worker_exit", worker_id=self.worker_id)

    def shutdown(self) -> None:
        logger.info("worker_shutdown", worker_id=self.worker_id)
        self._shutdown = True

    # ── 注册 + 心跳 ──

    async def register(self) -> None:
        # gateway 注册可能返回缓存身份(重启复用同一 id);runner 已用旧 id 创建但子进程忽略 worker_id,无碍。
        self.worker_id = await self.transport.register(
            worker_id=self.worker_id, worker_type=self.worker_type,
            pools=self.pools, tags=self.tags, reject_tags=self.reject_tags,
            hostname=socket.gethostname(), now=datetime.now(timezone.utc),
        )

    async def heartbeat_loop(self) -> None:
        while not self._shutdown:
            await self.transport.heartbeat(self.worker_id)
            await asyncio.sleep(10)

    # ── 主循环 ──

    async def main_loop(self) -> None:
        last_task_time = time.time()
        while not self._shutdown:
            task = await self.fetch_task()
            if task:
                last_task_time = time.time()
                await self.execute(task)
            else:
                if self.idle_timeout and time.time() - last_task_time > self.idle_timeout:
                    logger.info("idle_timeout_exit", worker_id=self.worker_id)
                    break
                await asyncio.sleep(1)

    # ── 任务获取 ──

    async def fetch_task(self) -> dict | None:
        if await self.transport.get_worker_status(self.worker_id) == "draining":
            return None

        for pool in self.pools:
            if await self.transport.is_pool_frozen(pool):
                continue

            pool_cfg = self.config.pools.get("pools", {}).get(pool, {})
            limit = pool_cfg.get("limit", 999)
            if not await self.transport.try_acquire_slot(pool, limit):
                continue

            result = await self.pop_matching_task(pool)
            if result:
                task, _raw_json, _score = result
                task["pool"] = pool
                if pool == "scene":
                    await self.transport.freeze_pool("cpu")
                return task

            await self.transport.release_slot(pool)

        return None

    async def pop_matching_task(
        self, pool: str, max_tries: int = 5,
    ) -> tuple[dict, str, float] | None:
        for _ in range(max_tries):
            result = await self.transport.dequeue_step_raw(pool)
            if result is None:
                return None

            raw_json, task, score = result
            require_tags = set(task.get("require_tags", []))
            all_tags = set(task.get("tags", []))

            if require_tags.issubset(self.tags) and not all_tags.intersection(self.reject_tags):
                return task, raw_json, score

            await self.transport.return_step(pool, raw_json, score)

        return None

    # ── 任务执行 ──

    async def execute(self, task: dict) -> None:
        job_id = task["job_id"]
        step = task["step"]
        pool = task["pool"]
        exec_id = f"{self.worker_id}:{int(time.time() * 1000)}"

        acquired = await self.transport.cas_step_status(job_id, step, "ready", "running")
        if not acquired:
            await self.transport.release_slot(pool)
            if pool == "scene":
                await self.transport.unfreeze_pool("cpu")
            return

        await self.transport.set_step_worker(job_id, step, self.worker_id)
        await self.transport.update_status(self.worker_id, "busy", job_id, step)
        await self.transport.publish_step_event("step_started", {
            "job_id": job_id, "step": step, "status": "running",
            "worker": self.worker_id, "exec_id": exec_id,
        })
        await self.transport.publish_step_event(f"events:{job_id}", {
            "event": "step_start", "step": step, "worker": self.worker_id,
        })

        start = time.time()
        work_dir = None
        try:
            work_dir = await self.storage.pull(job_id, step)

            pipeline = await self.transport.get_job_pipeline(job_id)
            job_info = await self.transport.get_job_info(job_id)
            domain = job_info.get("domain", "general")
            style_tags_raw = job_info.get("style_tags", "[]")
            try:
                style_tags = json.loads(style_tags_raw) if isinstance(style_tags_raw, str) else style_tags_raw
            except (json.JSONDecodeError, TypeError):
                style_tags = []
            step_cfg = build_step_config(
                self.config, pipeline, step, domain,
                style_tags=style_tags if isinstance(style_tags, list) else [],
            )

            raw_steps = self.config.pipelines[pipeline]["steps"]
            raw = next((s for s in raw_steps if s["name"] == step), None)
            if raw is None:
                raise ValueError(f"step '{step}' not found in pipeline '{pipeline}'")
            module = raw["module"]
            image = raw.get("image", "mnemo/step-base")
            use_gpu = ("gpu" in self.tags) and (
                pool == "gpu" or "gpu" in set(raw.get("tags", []))
            )
            ctx = StepContext(
                job_id=job_id, step=step, work_dir=work_dir, exec_id=exec_id,
                step_cfg=step_cfg, module=module, image=image,
                timeout_sec=step_cfg["step"]["timeout_sec"],
                pool=pool, use_gpu=use_gpu,
            )

            async def on_progress(event: str, payload: dict) -> None:
                await self.transport.publish_step_event(
                    f"events:{job_id}", {"event": event, **payload},
                )

            async def on_tick() -> None:
                # 续约:让 DB/Redis 里的 "当前 task" 秒级新鲜 + 推送运行中日志。
                await self.transport.update_status(self.worker_id, "busy", job_id, step)
                await self._push_step_log(job_id, step, work_dir)

            try:
                returncode, stderr = await self.runner.run_step(ctx, on_progress, on_tick)
            finally:
                # 不论成功/失败/超时,都把本步产物(含日志)推回存储,失败也能在前端看日志排错。
                await self._push_safe(job_id, step, work_dir)
            duration = time.time() - start

            if returncode == 0:
                await self._collect_usage(job_id, step, work_dir)
                await self.transport.publish_step_event("step_completed", {
                    "job_id": job_id, "step": step, "status": "done",
                    "duration": round(duration, 1),
                    "worker": self.worker_id, "exec_id": exec_id,
                })
                await self.transport.publish_step_event(f"events:{job_id}", {
                    "event": "step_done", "step": step,
                    "duration_sec": round(duration, 1),
                })
                await self.transport.update_step_result(
                    job_id, step, status="done", worker_id=self.worker_id,
                    started_at=datetime.fromtimestamp(start, timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    duration_sec=round(duration, 1),
                )
                await self.transport.increment_worker_stats(
                    self.worker_id, completed=1, duration=round(duration, 1),
                )
                logger.info(
                    "step_done", worker_id=self.worker_id,
                    job_id=job_id, step=step, duration=round(duration, 1),
                )
            else:
                error_msg = stderr[-500:] if stderr else "unknown error"
                error_type = self._parse_error_type(work_dir, step)
                await self.transport.publish_step_event("step_failed", {
                    "job_id": job_id, "step": step, "status": "failed",
                    "error": error_msg, "error_type": error_type,
                    "worker": self.worker_id, "exec_id": exec_id,
                })
                await self.transport.publish_step_event(f"events:{job_id}", {
                    "event": "step_failed", "step": step,
                    "error": error_msg[:200],
                })
                await self.transport.update_step_result(
                    job_id, step, status="failed", error=error_msg,
                    worker_id=self.worker_id,
                    started_at=datetime.fromtimestamp(start, timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    duration_sec=round(duration, 1),
                )
                await self.transport.increment_worker_stats(self.worker_id, failed=1)
                logger.warning(
                    "step_failed", worker_id=self.worker_id,
                    job_id=job_id, step=step, error=error_msg[:200],
                )

        except asyncio.TimeoutError:
            duration = time.time() - start
            await self.transport.publish_step_event("step_failed", {
                "job_id": job_id, "step": step, "status": "failed",
                "error": "timeout", "error_type": "timeout",
                "worker": self.worker_id,
            })
            await self.transport.publish_step_event(f"events:{job_id}", {
                "event": "step_failed", "step": step,
                "error": "timeout",
            })
            await self.transport.update_step_result(
                job_id, step, status="failed", error="timeout",
                worker_id=self.worker_id,
                started_at=datetime.fromtimestamp(start, timezone.utc),
                finished_at=datetime.now(timezone.utc),
                duration_sec=round(duration, 1),
            )
            logger.warning(
                "step_timeout", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        except Exception as e:
            duration = time.time() - start
            error_msg = str(e)[:500]
            await self.transport.publish_step_event("step_failed", {
                "job_id": job_id, "step": step, "status": "failed",
                "error": error_msg, "error_type": "processing",
                "worker": self.worker_id,
            })
            await self.transport.update_step_result(
                job_id, step, status="failed", error=error_msg,
                worker_id=self.worker_id,
                started_at=datetime.fromtimestamp(start, timezone.utc),
                finished_at=datetime.now(timezone.utc),
                duration_sec=round(duration, 1),
            )
            logger.exception(
                "step_unexpected_error", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        finally:
            if work_dir:
                await self.storage.cleanup(job_id, step, work_dir)
            await self.transport.release_slot(pool)
            if pool == "scene":
                await self.transport.unfreeze_pool("cpu")
            await self.transport.update_status(self.worker_id, "idle")

    # ── 运行中日志推送 ──

    async def _push_step_log(self, job_id: str, step: str, work_dir: Path) -> None:
        """把运行中日志推回存储,供前端准实时拉取。超阈值只推尾部,失败不致命。"""
        log_path = work_dir / "logs" / f"{step}.log"
        if not log_path.is_file():
            return
        try:
            tail_bytes = 256 * 1024
            size = log_path.stat().st_size
            if size > tail_bytes:
                with log_path.open("rb") as f:
                    f.seek(size - tail_bytes)
                    data = b"...(truncated)...\n" + f.read()
            else:
                data = log_path.read_bytes()
            await self.storage.write_file(job_id, f"logs/{step}.log", data)
        except Exception:
            logger.warning(
                "step_log_push_failed", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

    # ── 工具方法 ──

    def _parse_error_type(self, work_dir: Path, step: str) -> str:
        error_file = work_dir / f".{step}.error.json"
        if error_file.exists():
            try:
                data = json.loads(error_file.read_text())
                return data.get("error_type", "unknown")
            except (json.JSONDecodeError, OSError):
                pass
        return "unknown"

    async def _push_safe(self, job_id: str, step: str, work_dir: Path) -> None:
        """把本步产物(含日志)推回存储;失败不致命(避免遮蔽真正的步骤错误)。"""
        try:
            await self.storage.push(job_id, step, work_dir)
        except Exception:
            logger.warning(
                "storage_push_failed", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

    async def _collect_usage(self, job_id: str, step: str, work_dir: Path) -> None:
        usages = collect_usage_from_file(work_dir / "logs", step)
        for usage in usages:
            await self.transport.record_ai_usage(usage)
