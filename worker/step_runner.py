"""StepRunner：把步骤执行底座抽成可换实现，对齐 StorageBackend 的分流模式。

SubprocessStepRunner 是默认实现（零行为变化）；DockerStepRunner 为每步一容器的草案，
由 STEP_RUNTIME=docker 启用。runner 只读写 work_dir，不连 Redis/DB/对象存储——
控制面交互（状态续约、日志推送、事件发布）全经 worker 注入的回调。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

import structlog

logger = structlog.get_logger(component="step_runner")

# 进度发布回调：(event_name, payload) -> 让 runner 对控制面无知。
ProgressPublisher = Callable[[str, dict], Awaitable[None]]
# 周期回调：每 10s 一次，worker 用它续约状态 + 推送运行中日志。
TickCallback = Callable[[], Awaitable[None]]


@dataclass
class StepContext:
    job_id: str
    step: str
    work_dir: Path
    exec_id: str
    step_cfg: dict
    module: str
    image: str = "mnemo/step-base"
    timeout_sec: int = 600
    pool: str = ""
    use_gpu: bool = False


class StepRunner(Protocol):
    async def run_step(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
    ) -> tuple[int, str]:
        """跑一个 step，返回 (returncode, stderr_tail)。
        超时写完含 TIMEOUT 标记的日志后抛 asyncio.TimeoutError。
        只读写 ctx.work_dir，不碰 .done/.meta/.error 语义，不连 Redis/对象存储。"""
        ...


class SubprocessStepRunner:
    """子进程执行：边读管道边落盘，每 10s 续约 + 转发进度。"""

    async def run_step(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
    ) -> tuple[int, str]:
        work_dir = ctx.work_dir
        step = ctx.step
        config_path = work_dir / f".{step}.config.json"
        config_path.write_text(json.dumps(ctx.step_cfg, ensure_ascii=False, indent=2))

        timeout = ctx.timeout_sec
        env = {**os.environ, "STEP_EXEC_ID": ctx.exec_id}

        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", ctx.module,
            "--job-dir", str(work_dir),
            "--step-config", str(config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # 运行中即可见:边读管道边追加到 logs/{step}.log(stdout/stderr 合一,带前缀)。
        log_dir = work_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{step}.log"
        log_file = log_path.open("w", encoding="utf-8")
        stderr_tail: list[str] = []

        async def _drain(stream: asyncio.StreamReader, prefix: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace")
                log_file.write(prefix + text if prefix else text)
                log_file.flush()
                if prefix:
                    stderr_tail.append(text)
                    if len(stderr_tail) > 50:
                        del stderr_tail[0]

        monitor_task = asyncio.create_task(
            self._progress_monitor(ctx, on_progress, on_tick, lambda: proc.returncode is None)
        )
        drain_task = asyncio.gather(
            _drain(proc.stdout, ""),
            _drain(proc.stderr, "[stderr] "),
        )

        timed_out = False
        try:
            await asyncio.wait_for(asyncio.shield(drain_task), timeout=timeout)
            await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
            await drain_task
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            if timed_out:
                log_file.write(f"\n--- TIMEOUT after {timeout}s ---\n")
            log_file.flush()
            log_file.close()
            config_path.unlink(missing_ok=True)

        if timed_out:
            raise asyncio.TimeoutError()

        return proc.returncode, "".join(stderr_tail)

    async def _progress_monitor(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
        proc_alive: Callable[[], bool],
    ) -> None:
        """每 10s 续约 worker 状态 + 推日志(on_tick)，写 worker_heartbeat_at，
        转发步骤进度事件。不覆盖步骤自己写的 updated_at，否则 check_stuck 失效。"""
        progress_file = ctx.work_dir / f".{ctx.step}.progress"

        while proc_alive():
            await asyncio.sleep(10)

            # 续约:让 DB/Redis 里的 "当前 task" 秒级新鲜,scheduler 据此回收僵尸 worker。
            await on_tick()

            progress_data: dict = {}
            if progress_file.exists():
                try:
                    progress_data = json.loads(progress_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

            progress_data["worker_heartbeat_at"] = time.time()
            progress_file.write_text(json.dumps(progress_data))

            if "current" in progress_data and "total" in progress_data:
                await on_progress("step_progress", {
                    "step": ctx.step,
                    "current": progress_data["current"],
                    "total": progress_data["total"],
                    "pct": progress_data.get("pct", 0),
                    "message": progress_data.get("message", ""),
                })


class DockerStepRunner:
    """每步一容器：work_dir bind-mount 到 /job，GPU 经 DeviceRequest，container.wait
    + kill 复刻超时，labels 防泄漏。阶段0 不默认启用，仅须语法/导入安全。"""

    def __init__(self, worker_id: str, host_work_root: str | None = None):
        import docker  # 延迟导入：subprocess 模式不强依赖 docker SDK。

        self._client = docker.from_env()
        self._worker_id = worker_id
        # DooD：bind-mount 必须用宿主路径，非 worker 容器内路径。None 时退化为原路径。
        self._host_work_root = host_work_root

    def _host_path(self, work_dir: Path) -> str:
        if not self._host_work_root:
            return str(work_dir)
        return str(Path(self._host_work_root) / work_dir.name)

    async def run_step(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
    ) -> tuple[int, str]:
        import docker
        from docker.types import DeviceRequest

        work_dir = ctx.work_dir
        step = ctx.step
        config_path = work_dir / f".{step}.config.json"
        config_path.write_text(json.dumps(ctx.step_cfg, ensure_ascii=False, indent=2))

        host_dir = self._host_path(work_dir)
        # 命令与 subprocess 同构,故 StepBase.cli_main 不改。
        command = [
            "python3", "-m", ctx.module,
            "--job-dir", "/job",
            "--step-config", f"/job/.{step}.config.json",
        ]
        environment = {"STEP_EXEC_ID": ctx.exec_id}

        device_requests = None
        if ctx.use_gpu:
            device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]

        labels = {
            "mnemo.job": ctx.job_id,
            "mnemo.step": step,
            "mnemo.worker": self._worker_id,
        }

        def _create_start():
            return self._client.containers.run(
                image=ctx.image,
                command=command,
                working_dir="/job",
                volumes={host_dir: {"bind": "/job", "mode": "rw"}},
                environment=environment,
                device_requests=device_requests,
                labels=labels,
                detach=True,
                auto_remove=False,
            )

        container = await asyncio.to_thread(_create_start)
        timed_out = False
        returncode = 1
        stderr_tail = ""
        try:
            log_task = asyncio.create_task(self._stream_logs(container, work_dir, step))
            monitor = asyncio.create_task(
                self._progress_monitor(
                    ctx, on_progress, on_tick, lambda: _alive(container),
                )
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(container.wait), timeout=ctx.timeout_sec,
                )
                returncode = int(result.get("StatusCode", 1))
            except asyncio.TimeoutError:
                timed_out = True
                await asyncio.to_thread(container.kill)
            finally:
                log_task.cancel()
                try:
                    await log_task
                except asyncio.CancelledError:
                    pass
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

            if timed_out:
                self._append_timeout_marker(work_dir, step, ctx.timeout_sec)
            else:
                stderr_tail = self._tail_log(work_dir, step, n_chars=4000)
        finally:
            config_path.unlink(missing_ok=True)
            try:
                await asyncio.to_thread(container.remove, force=True)
            except docker.errors.APIError:
                logger.warning("container_remove_failed", job_id=ctx.job_id, step=step)

        if timed_out:
            raise asyncio.TimeoutError()
        return returncode, stderr_tail

    async def _progress_monitor(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
        proc_alive: Callable[[], bool],
    ) -> None:
        progress_file = ctx.work_dir / f".{ctx.step}.progress"

        while proc_alive():
            await asyncio.sleep(10)
            await on_tick()

            progress_data: dict = {}
            if progress_file.exists():
                try:
                    progress_data = json.loads(progress_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

            progress_data["worker_heartbeat_at"] = time.time()
            progress_file.write_text(json.dumps(progress_data))

            if "current" in progress_data and "total" in progress_data:
                await on_progress("step_progress", {
                    "step": ctx.step,
                    "current": progress_data["current"],
                    "total": progress_data["total"],
                    "pct": progress_data.get("pct", 0),
                    "message": progress_data.get("message", ""),
                })

    async def _stream_logs(self, container, work_dir: Path, step: str) -> None:
        """把容器 stdout/stderr 合流 tee 到 logs/{step}.log,运行中即可见。"""
        log_dir = work_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{step}.log"

        def _tee() -> None:
            with log_path.open("wb") as f:
                for chunk in container.logs(stream=True, follow=True):
                    f.write(chunk)
                    f.flush()

        await asyncio.to_thread(_tee)

    def _tail_log(self, work_dir: Path, step: str, n_chars: int) -> str:
        log_path = work_dir / "logs" / f"{step}.log"
        if not log_path.is_file():
            return ""
        try:
            return log_path.read_text(errors="replace")[-n_chars:]
        except OSError:
            return ""

    def _append_timeout_marker(self, work_dir: Path, step: str, timeout: int) -> None:
        log_dir = work_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        with (log_dir / f"{step}.log").open("a", encoding="utf-8") as f:
            f.write(f"\n--- TIMEOUT after {timeout}s ---\n")

    def reap_orphans(self) -> None:
        """清理本 worker 上一进程残留的步骤容器(按 label 过滤)。"""
        for c in self._client.containers.list(
            all=True, filters={"label": f"mnemo.worker={self._worker_id}"},
        ):
            try:
                c.remove(force=True)
            except Exception:
                pass


def _alive(container) -> bool:
    try:
        container.reload()
        return container.status == "running"
    except Exception:
        return False


def create_step_runner(worker_id: str) -> StepRunner:
    runtime = os.environ.get("STEP_RUNTIME", "subprocess").lower()
    if runtime == "docker":
        return DockerStepRunner(
            worker_id,
            host_work_root=os.environ.get("HOST_WORK_DIR"),
        )
    return SubprocessStepRunner()
