"""Worker：从资源池队列自取任务，执行步骤脚本，上报结果。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path

import structlog

from shared.ai_gateway import collect_usage_from_file
from shared.config import AppConfig, build_step_config
from shared.db import Database
from shared.models import Worker as WorkerModel, generate_worker_id
from shared.redis_client import RedisClient
from shared.storage import StorageBackend

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
        redis: RedisClient,
        db: Database,
        config: AppConfig,
        storage: StorageBackend,
        worker_type: str,
        pools: list[str],
        tags: set[str],
        reject_tags: set[str],
    ):
        self.redis = redis
        self.db = db
        self.config = config
        self.storage = storage
        self.worker_type = worker_type
        self.worker_id = generate_worker_id(worker_type)
        self.pools = pools
        self.tags = tags
        self.reject_tags = reject_tags
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "0"))
        self._shutdown = False

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
            await self._update_worker_status("offline")
            logger.info("worker_exit", worker_id=self.worker_id)

    def shutdown(self) -> None:
        logger.info("worker_shutdown", worker_id=self.worker_id)
        self._shutdown = True

    # ── 注册 + 心跳 ──

    async def register(self) -> None:
        now = datetime.now()
        info = {
            "type": self.worker_type,
            "pools": ",".join(self.pools),
            "tags": ",".join(sorted(self.tags)),
            "reject_tags": ",".join(sorted(self.reject_tags)),
            "hostname": socket.gethostname(),
            "status": "idle",
            "started_at": now.isoformat(),
            "last_heartbeat": now.isoformat(),
        }
        await self.redis.register_worker(self.worker_id, info, ttl=30)

        worker_model = WorkerModel(
            id=self.worker_id,
            type=self.worker_type,
            pools=self.pools,
            tags=self.tags,
            reject_tags=self.reject_tags,
            hostname=socket.gethostname(),
            status="idle",
            started_at=now,
            first_seen=now,
            last_heartbeat=now,
        )
        await asyncio.to_thread(self.db.upsert_worker, worker_model)

    async def heartbeat_loop(self) -> None:
        while not self._shutdown:
            await self.redis.heartbeat(self.worker_id, ttl=30)
            # 同步刷新 DB 心跳：/api/workers 与前端 online 判定读的是 DB，
            # 不写回 DB 则 last_heartbeat 永远停在注册时刻 → Worker 页面显示空。
            await asyncio.to_thread(
                self.db.update_worker_heartbeat, self.worker_id,
            )
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
        info = await self.redis.get_worker_info(self.worker_id)
        if info and info.get("status") == "draining":
            return None

        for pool in self.pools:
            if await self.redis.is_pool_frozen(pool):
                continue

            pool_cfg = self.config.pools.get("pools", {}).get(pool, {})
            limit = pool_cfg.get("limit", 999)
            if not await self.redis.try_acquire_slot(pool, limit):
                continue

            result = await self.pop_matching_task(pool)
            if result:
                task, _raw_json, _score = result
                task["pool"] = pool
                if pool == "scene":
                    await self.redis.freeze_pool("cpu")
                return task

            await self.redis.release_slot(pool)

        return None

    async def pop_matching_task(
        self, pool: str, max_tries: int = 5,
    ) -> tuple[dict, str, float] | None:
        for _ in range(max_tries):
            result = await self.redis.dequeue_step_raw(pool)
            if result is None:
                return None

            raw_json, task, score = result
            require_tags = set(task.get("require_tags", []))
            all_tags = set(task.get("tags", []))

            if require_tags.issubset(self.tags) and not all_tags.intersection(self.reject_tags):
                return task, raw_json, score

            await self.redis.return_step(pool, raw_json, score)

        return None

    # ── 任务执行 ──

    async def execute(self, task: dict) -> None:
        job_id = task["job_id"]
        step = task["step"]
        pool = task["pool"]
        exec_id = f"{self.worker_id}:{int(time.time() * 1000)}"

        acquired = await self.redis.cas_step_status(job_id, step, "ready", "running")
        if not acquired:
            await self.redis.release_slot(pool)
            if pool == "scene":
                await self.redis.unfreeze_pool("cpu")
            return

        await self.redis.set_step_worker(job_id, step, self.worker_id)
        await self._update_worker_status("busy", job_id, step)
        await self.redis.publish("step_started", {
            "job_id": job_id, "step": step,
            "worker": self.worker_id, "exec_id": exec_id,
        })
        await self.redis.publish(f"events:{job_id}", {
            "event": "step_start", "step": step, "worker": self.worker_id,
        })

        start = time.time()
        work_dir = None
        try:
            work_dir = await self.storage.pull(job_id, step)

            pipeline = await self.redis.get_job_pipeline(job_id)
            job_info = await self.redis.get_job_info(job_id)
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

            returncode, stderr = await self._run_step(
                job_id, step, work_dir, exec_id, step_cfg, module,
            )
            duration = time.time() - start

            if returncode == 0:
                await self.storage.push(job_id, step, work_dir)
                await self._collect_usage(job_id, step, work_dir)
                await self.redis.publish("step_completed", {
                    "job_id": job_id, "step": step, "status": "done",
                    "duration": round(duration, 1),
                    "worker": self.worker_id, "exec_id": exec_id,
                })
                await self.redis.publish(f"events:{job_id}", {
                    "event": "step_done", "step": step,
                    "duration_sec": round(duration, 1),
                })
                await asyncio.to_thread(
                    self.db.update_step, job_id, step,
                    status="done", worker_id=self.worker_id,
                    started_at=datetime.fromtimestamp(start),
                    finished_at=datetime.now(),
                    duration_sec=round(duration, 1),
                )
                await asyncio.to_thread(
                    self.db.increment_worker_stats, self.worker_id,
                    completed=1, duration=round(duration, 1),
                )
                logger.info(
                    "step_done", worker_id=self.worker_id,
                    job_id=job_id, step=step, duration=round(duration, 1),
                )
            else:
                error_msg = stderr[-500:] if stderr else "unknown error"
                error_type = self._parse_error_type(work_dir, step)
                await self.redis.publish("step_failed", {
                    "job_id": job_id, "step": step, "status": "failed",
                    "error": error_msg, "error_type": error_type,
                    "worker": self.worker_id, "exec_id": exec_id,
                })
                await self.redis.publish(f"events:{job_id}", {
                    "event": "step_failed", "step": step,
                    "error": error_msg[:200],
                })
                await asyncio.to_thread(
                    self.db.update_step, job_id, step,
                    status="failed", error=error_msg,
                    worker_id=self.worker_id,
                    started_at=datetime.fromtimestamp(start),
                    finished_at=datetime.now(),
                    duration_sec=round(duration, 1),
                )
                await asyncio.to_thread(
                    self.db.increment_worker_stats, self.worker_id, failed=1,
                )
                logger.warning(
                    "step_failed", worker_id=self.worker_id,
                    job_id=job_id, step=step, error=error_msg[:200],
                )

        except asyncio.TimeoutError:
            duration = time.time() - start
            await self.redis.publish("step_failed", {
                "job_id": job_id, "step": step, "status": "failed",
                "error": "timeout", "error_type": "timeout",
                "worker": self.worker_id,
            })
            await self.redis.publish(f"events:{job_id}", {
                "event": "step_failed", "step": step,
                "error": "timeout",
            })
            await asyncio.to_thread(
                self.db.update_step, job_id, step,
                status="failed", error="timeout",
                worker_id=self.worker_id,
                started_at=datetime.fromtimestamp(start),
                finished_at=datetime.now(),
                duration_sec=round(duration, 1),
            )
            logger.warning(
                "step_timeout", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        except Exception as e:
            duration = time.time() - start
            error_msg = str(e)[:500]
            await self.redis.publish("step_failed", {
                "job_id": job_id, "step": step, "status": "failed",
                "error": error_msg, "error_type": "processing",
                "worker": self.worker_id,
            })
            await asyncio.to_thread(
                self.db.update_step, job_id, step,
                status="failed", error=error_msg,
                worker_id=self.worker_id,
                started_at=datetime.fromtimestamp(start),
                finished_at=datetime.now(),
                duration_sec=round(duration, 1),
            )
            logger.exception(
                "step_unexpected_error", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        finally:
            if work_dir:
                await self.storage.cleanup(job_id, step, work_dir)
            await self.redis.release_slot(pool)
            if pool == "scene":
                await self.redis.unfreeze_pool("cpu")
            await self._update_worker_status("idle")

    # ── 子进程执行 ──

    async def _run_step(
        self,
        job_id: str,
        step: str,
        work_dir: Path,
        exec_id: str,
        step_cfg: dict,
        module: str,
    ) -> tuple[int, str]:
        config_path = work_dir / f".{step}.config.json"
        config_path.write_text(json.dumps(step_cfg, ensure_ascii=False, indent=2))

        timeout = step_cfg["step"]["timeout_sec"]
        env = {**os.environ, "STEP_EXEC_ID": exec_id}

        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", module,
            "--job-dir", str(work_dir),
            "--step-config", str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        monitor_task = asyncio.create_task(
            self._progress_monitor(job_id, step, work_dir, proc)
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            config_path.unlink(missing_ok=True)

        log_dir = work_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{step}.log"
        log_content = ""
        if stdout:
            log_content += stdout.decode(errors="replace")
        if stderr:
            log_content += "\n--- STDERR ---\n" + stderr.decode(errors="replace")
        if log_content:
            log_path.write_text(log_content)

        return proc.returncode, stderr.decode(errors="replace") if stderr else ""

    async def _progress_monitor(
        self,
        job_id: str,
        step: str,
        work_dir: Path,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """每 10s 写 worker_heartbeat_at + 转发步骤进度事件。
        不覆盖步骤自己写的 updated_at，否则 check_stuck 失效。"""
        progress_file = work_dir / f".{step}.progress"

        while proc.returncode is None:
            await asyncio.sleep(10)

            progress_data: dict = {}
            if progress_file.exists():
                try:
                    progress_data = json.loads(progress_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

            progress_data["worker_heartbeat_at"] = time.time()
            progress_file.write_text(json.dumps(progress_data))

            if "current" in progress_data and "total" in progress_data:
                await self.redis.publish(f"events:{job_id}", {
                    "event": "step_progress",
                    "step": step,
                    "current": progress_data["current"],
                    "total": progress_data["total"],
                    "pct": progress_data.get("pct", 0),
                    "message": progress_data.get("message", ""),
                })

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

    async def _collect_usage(self, job_id: str, step: str, work_dir: Path) -> None:
        usages = collect_usage_from_file(work_dir / "logs", step)
        for usage in usages:
            await asyncio.to_thread(self.db.record_ai_usage, usage)

    async def _update_worker_status(
        self,
        status: str,
        job_id: str | None = None,
        step: str | None = None,
    ) -> None:
        await self.redis.set_worker_field(self.worker_id, "status", status)
        await self.redis.set_worker_field(
            self.worker_id, "current_job", job_id or "",
        )
        await self.redis.set_worker_field(
            self.worker_id, "current_step", step or "",
        )
        # 状态变更同样写回 DB（/api/workers 的数据源），并刷新心跳，
        # 保证 busy/idle/offline 与当前任务在 Worker 页面实时可见。
        await asyncio.to_thread(
            self.db.update_worker_heartbeat, self.worker_id,
            status=status, current_job=job_id or "", current_step=step or "",
        )
