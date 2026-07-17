"""StepRunner: 把步骤执行底座抽成可换实现,对齐 StorageBackend 的分流模式。

SubprocessStepRunner 是默认实现;DockerStepRunner 为每步一容器,
由 STEP_RUNTIME=docker 启用。runner 只读写 work_dir,不连 Redis/DB/对象存储;
控制面交互(状态续约、日志推送、事件发布)全经 worker 注入的回调。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

import structlog

logger = structlog.get_logger(component="step_runner")

# 进度发布回调:(event_name, payload) -> 让 runner 对控制面无知。
ProgressPublisher = Callable[[str, dict], Awaitable[None]]
# 周期回调:每 10s 一次,worker 用它续约状态 + 推送运行中日志。
TickCallback = Callable[[], Awaitable[None]]

# 需要出网的资源池:下载与 AI 调用。其余(scene/cpu/gpu)离线,文件是接口。
_NETWORKED_POOLS = frozenset({"io", "ai"})
# AI step 才注入的密钥白名单:仅注入 env 里实际存在的那几个。
_AI_KEY_ENV = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OLLAMA_URL")
# 控制面密钥:步骤只读写本地 work_dir,绝不直连 MinIO/Redis/Gateway,故全程剥离。
_CONTROL_PLANE_SECRETS = (
    "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_URL",
    "WORKER_REGISTRATION_TOKEN", "WORKER_TOKEN", "GATEWAY_URL", "REDIS_URL",
)

_DEFAULT_STEP_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_TRUNCATION_MARKER = b"...(older step log truncated by FLORI_STEP_LOG_MAX_BYTES)...\n"
_LOG_LOW_WATERMARK_RATIO = 0.75
_DOCKER_LOG_MAX_SIZE = "10m"
_DOCKER_LOG_MAX_FILE = "3"
_DEFAULT_DOCKER_CONTROL_TIMEOUT_SEC = 5.0


def _step_log_max_bytes() -> int:
    try:
        value = int(os.environ.get("FLORI_STEP_LOG_MAX_BYTES", _DEFAULT_STEP_LOG_MAX_BYTES))
    except ValueError:
        value = _DEFAULT_STEP_LOG_MAX_BYTES
    return max(1024, value)


def _docker_control_timeout_sec() -> float:
    try:
        value = float(
            os.environ.get(
                "FLORI_DOCKER_CONTROL_TIMEOUT_SEC", _DEFAULT_DOCKER_CONTROL_TIMEOUT_SEC,
            )
        )
    except ValueError:
        value = _DEFAULT_DOCKER_CONTROL_TIMEOUT_SEC
    return max(0.05, value)


class _BoundedLogWriter:
    """追加写步骤日志,超过硬上限时原子保留尾部到低水位."""

    def __init__(self, path: Path):
        self.path = path
        self.max_bytes = _step_log_max_bytes()
        self._file = path.open("ab")
        self._size = path.stat().st_size
        self._low_watermark = max(
            len(_LOG_TRUNCATION_MARKER),
            int(self.max_bytes * _LOG_LOW_WATERMARK_RATIO),
        )
        if self._size > self.max_bytes:
            self._compact()

    def write(self, data: str | bytes) -> None:
        raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        if not raw:
            return
        # 不能先把超大 chunk 全写进真实路径再旋转:即使 write() 返回时已压回,
        # 采集器仍可能在瞬间看到超限文件.越界前直接用旧尾+新 chunk 生成低水位文件.
        if self._size + len(raw) > self.max_bytes:
            self._compact(raw)
            return
        self._file.write(raw)
        self._size += len(raw)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.flush()
        self._file.close()

    def _compact(self, incoming: bytes = b"") -> None:
        self._file.flush()
        self._file.close()
        keep = max(0, self._low_watermark - len(_LOG_TRUNCATION_MARKER))
        if len(incoming) >= keep:
            tail = incoming[-keep:] if keep else b""
        else:
            old_keep = keep - len(incoming)
            with self.path.open("rb") as source:
                source.seek(max(0, self._size - old_keep))
                tail = source.read(old_keep) + incoming
        tmp = self.path.with_name(f".{self.path.name}.rotate.tmp")
        try:
            with tmp.open("wb") as target:
                target.write(_LOG_TRUNCATION_MARKER)
                target.write(tail)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
        self._file = self.path.open("ab")
        self._size = self.path.stat().st_size


@dataclass
class StepContext:
    job_id: str
    step: str
    work_dir: Path
    exec_id: str
    step_cfg: dict
    module: str
    scope_key: str = "job"
    part_id: str | None = None
    image: str = "flori/step-base"
    timeout_sec: int = 600
    pool: str = ""
    use_gpu: bool = False
    # 步骤专属注入 env(如下载步的中心分发凭证):只进子进程环境,随进程结束消亡,
    # 不碰 os.environ(worker 并发跑多任务,进程级 env 互斥会串台)。
    extra_env: dict = field(default_factory=dict)


class StepRunner(Protocol):
    async def run_step(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
    ) -> tuple[int, str]:
        """跑一个 step,返回 (returncode, stderr_tail)。
        超时写完含 TIMEOUT 标记的日志后抛 asyncio.TimeoutError。
        只读写 ctx.work_dir,不碰 .done/.meta/.error 语义,不连 Redis/对象存储。"""
        ...


class SubprocessStepRunner:
    """子进程执行:边读管道边落盘,每 10s 续约 + 转发进度。"""

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
        # 按需下放:子进程需继承系统 env(PATH/HOME/LANG/LD_LIBRARY_PATH)才能 exec
        # python/ffmpeg,故用 DENYLIST 而非白名单,仅剥离步骤永不需要的敏感密钥。
        env = _build_subprocess_env(ctx)

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
        # append 而非 truncate:幂等跳过(只输出一行 skip: up-to-date)不覆盖上次真跑的处理日志;
        # 重跑在已有日志后追加分隔头,保留历史,避免出现有产物没日志。
        had_content = log_path.exists() and log_path.stat().st_size > 0
        log_file = _BoundedLogWriter(log_path)
        if had_content:
            log_file.write(f"\n===== re-run {step} =====\n")
            log_file.flush()
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
            _run_progress_monitor(ctx, on_progress, on_tick, lambda: proc.returncode is None)
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

class DockerStepRunner:
    """每步一容器:work_dir bind-mount 到 /job,GPU 经 DeviceRequest,container.wait
    + kill 复刻超时,labels 防泄漏。由 STEP_RUNTIME=docker 启用。"""

    def __init__(self, worker_id: str, host_work_root: str | None = None,
                 container_work_root: str | None = None,
                 registry: str | None = None):
        import docker  # 延迟导入:subprocess 模式不强依赖 docker SDK。

        self._client = docker.from_env()
        self._worker_id = worker_id
        # DooD:bind-mount 必须用宿主路径,非 worker 容器内路径。None 时退化为原路径。
        self._host_work_root = host_work_root
        self._container_work_root = Path(container_work_root or "/tmp/flori-work").resolve()
        # 镜像仓库前缀:把 pipelines 里的逻辑名 flori/step-X 解析成实仓名。
        self._registry = (registry or "").rstrip("/")

    def _host_path(self, work_dir: Path) -> str:
        # DooD bind-mount 必须用宿主路径。HOST_WORK_DIR 缺失时若退化为容器内路径,
        # 会把错误目录挂进 /job(读不到上一步产物)且无报错,因此直接 fail-fast。
        if not self._host_work_root:
            raise ValueError(
                "DockerStepRunner requires HOST_WORK_DIR (DooD 需宿主侧 work 目录路径)"
            )
        resolved = work_dir.resolve()
        if resolved != self._container_work_root and self._container_work_root not in resolved.parents:
            raise ValueError("step work_dir escapes configured WORK_DIR")
        return str(Path(self._host_work_root) / resolved.relative_to(self._container_work_root))

    def _resolve_image(self, image: str) -> str:
        # 逻辑名 flori/step-X 解析为 {registry}/flori-step-X(ghcr 扁平命名);
        # 未设 registry 或已是带 host 的全名则原样用(本机自建镜像直接命中)。
        if self._registry and image.startswith("flori/"):
            return f"{self._registry}/{image.replace('/', '-')}"
        return image

    def _build_environment(self, ctx: StepContext) -> dict:
        """白名单注入:始终给 STEP_EXEC_ID + HTTPS_PROXY(若有);
        仅 ai 池补 env 里实际存在的 AI 密钥。非 ai 池绝不见 AI key,杜绝全量透传。
        PYTHONPATH=/app:步骤镜像代码在 /app,而容器 working_dir=/job,缺它则
        python3 -m steps.* 找不到模块(子进程模式靠 cwd=/app,docker 模式必须显式给)。"""
        env = {
            "STEP_EXEC_ID": ctx.exec_id,
            "STEP_JOB_ID": ctx.job_id,
            "STEP_SCOPE_KEY": ctx.scope_key,
            "STEP_PART_ID": ctx.part_id or "",
            "PYTHONPATH": "/app",
            **ctx.extra_env,
        }
        proxy = os.environ.get("HTTPS_PROXY")
        if proxy:
            env["HTTPS_PROXY"] = proxy
        if ctx.pool == "ai":
            for key in _AI_KEY_ENV:
                val = os.environ.get(key)
                if val:
                    env[key] = val
        return env

    async def run_step(
        self,
        ctx: StepContext,
        on_progress: ProgressPublisher,
        on_tick: TickCallback,
    ) -> tuple[int, str]:
        from docker.types import DeviceRequest, LogConfig

        work_dir = ctx.work_dir
        step = ctx.step
        config_path = work_dir / f".{step}.config.json"
        config_path.write_text(json.dumps(ctx.step_cfg, ensure_ascii=False, indent=2))

        host_dir = self._host_path(work_dir)
        # 命令与 subprocess 同构,故 StepBase.cli_main 不改。--step-config 经 bind-mount
        # 跨界,绝不进 env / Cmd,避免明文配置落入 docker inspect。
        command = [
            "python3", "-m", ctx.module,
            "--job-dir", "/job",
            "--step-config", f"/job/.{step}.config.json",
        ]
        environment = self._build_environment(ctx)
        # 出网池(io/ai)走默认网络;离线计算池(scene/cpu/gpu)断网,文件是接口。
        network_mode = None if ctx.pool in _NETWORKED_POOLS else "none"

        device_requests = None
        if ctx.use_gpu:
            device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]

        labels = {
            "flori.job": ctx.job_id,
            "flori.step": step,
            "flori.worker": self._worker_id,
        }

        def _create_start():
            return self._client.containers.run(
                image=self._resolve_image(ctx.image),
                command=command,
                working_dir="/job",
                volumes={host_dir: {"bind": "/job", "mode": "rw"}},
                environment=environment,
                network_mode=network_mode,
                device_requests=device_requests,
                labels=labels,
                log_config=LogConfig(
                    type="local",
                    config={
                        "max-size": _DOCKER_LOG_MAX_SIZE,
                        "max-file": _DOCKER_LOG_MAX_FILE,
                        "compress": "true",
                    },
                ),
                detach=True,
                auto_remove=False,
            )

        container = await asyncio.to_thread(_create_start)
        timed_out = False
        returncode = 1
        stderr_tail = ""
        wait_task: asyncio.Task | None = None
        log_task: asyncio.Task | None = None
        log_stream: dict[str, object] = {}
        cleanup_state = {"removed": False}
        try:
            log_task = asyncio.create_task(
                self._stream_logs(container, work_dir, step, log_stream)
            )
            monitor = asyncio.create_task(
                _run_progress_monitor(
                    ctx, on_progress, on_tick, lambda: _alive(container),
                )
            )
            wait_task = asyncio.create_task(asyncio.to_thread(container.wait))
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(wait_task), timeout=ctx.timeout_sec,
                )
                returncode = int(result.get("StatusCode", 1))
            except asyncio.TimeoutError:
                timed_out = True
                await self._bounded_container_call(
                    container.kill, ctx=ctx, action="kill",
                )
                # wait_for 只取消 asyncio 等待,不能终止后台 Docker SDK 线程.复用同一
                # wait_task 有界 join;卡死时继续走日志关闭与 force-remove 兜底.
                try:
                    await self._join_task_bounded(wait_task)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "container_wait_after_kill_failed",
                        job_id=ctx.job_id,
                        step=step,
                        error_type=type(e).__name__,
                    )
            except BaseException:
                try:
                    await self._bounded_container_call(
                        container.kill, ctx=ctx, action="kill-after-wait-error",
                    )
                except Exception:
                    pass
                raise
            finally:
                # 容器退出后 Docker log stream 才会自然送完尾部并 EOF.必须 join 日志线程,
                # 再写 TIMEOUT marker / 读 tail / remove,否则末尾日志会丢且取消 to_thread
                # 只取消 await,后台线程仍会写已被移除容器.
                try:
                    await self._finish_log_task(
                        log_task, log_stream, container, cleanup_state, ctx,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "container_log_stream_failed",
                        job_id=ctx.job_id,
                        step=step,
                        error_type=type(e).__name__,
                    )
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
            if not cleanup_state["removed"]:
                await self._bounded_container_call(
                    container.remove, force=True, ctx=ctx, action="remove",
                )
            # force-remove 通常会释放仍卡在 Docker wait HTTP 调用的线程.再给一次
            # 有界 join,仍不返回就取消 asyncio 包装,不能让 worker 永久挂住.
            if wait_task is not None and not wait_task.done():
                try:
                    joined = await self._join_task_bounded(wait_task)
                except Exception:
                    joined = True
                if not joined:
                    wait_task.cancel()
                    try:
                        await wait_task
                    except asyncio.CancelledError:
                        pass

        if timed_out:
            raise asyncio.TimeoutError()
        return returncode, stderr_tail

    async def _bounded_container_call(
        self,
        func,
        *args,
        ctx: StepContext,
        action: str,
        **kwargs,
    ) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=_docker_control_timeout_sec(),
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "container_control_timeout",
                job_id=ctx.job_id,
                step=ctx.step,
                action=action,
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "container_control_failed",
                job_id=ctx.job_id,
                step=ctx.step,
                action=action,
                error_type=type(e).__name__,
            )
            return False

    async def _join_task_bounded(self, task: asyncio.Task) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=_docker_control_timeout_sec(),
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def _finish_log_task(
        self,
        task: asyncio.Task,
        stream_ref: dict[str, object],
        container,
        cleanup_state: dict[str, bool],
        ctx: StepContext,
    ) -> None:
        if await self._join_task_bounded(task):
            return
        stream = stream_ref.get("stream")
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                await self._bounded_container_call(
                    close, ctx=ctx, action="close-log-stream",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "container_log_close_failed",
                    job_id=ctx.job_id,
                    step=ctx.step,
                    error_type=type(e).__name__,
                )
        if await self._join_task_bounded(task):
            return
        # 日志流自身 close 仍不返回时,force-remove 动态容器以释放 daemon 侧
        # follow/wait 连接,再做最后一次 join.只有这一层仍超时才取消包装 task.
        cleanup_state["removed"] = await self._bounded_container_call(
            container.remove, force=True, ctx=ctx, action="remove-for-log-drain",
        )
        if await self._join_task_bounded(task):
            return
        logger.warning("container_log_join_timeout", job_id=ctx.job_id, step=ctx.step)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _stream_logs(
        self,
        container,
        work_dir: Path,
        step: str,
        stream_ref: dict[str, object],
    ) -> None:
        """把容器 stdout/stderr 合流 tee 到 logs/{step}.log,运行中即可见。"""
        log_dir = work_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{step}.log"

        def _tee() -> None:
            writer = _BoundedLogWriter(log_path)
            stream = None
            try:
                stream = container.logs(stream=True, follow=True)
                stream_ref["stream"] = stream
                for chunk in stream:
                    writer.write(chunk)
                    writer.flush()
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                writer.close()

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
        writer = _BoundedLogWriter(log_dir / f"{step}.log")
        try:
            writer.write(f"\n--- TIMEOUT after {timeout}s ---\n")
        finally:
            writer.close()

    def reap_orphans(self) -> None:
        """清理本 worker 上一进程残留的步骤容器(按 label 过滤)。"""
        for c in self._client.containers.list(
            all=True, filters={"label": f"flori.worker={self._worker_id}"},
        ):
            try:
                c.remove(force=True)
            except Exception:
                pass


async def _run_progress_monitor(
    ctx: StepContext,
    on_progress: ProgressPublisher,
    on_tick: TickCallback,
    proc_alive: Callable[[], bool],
) -> None:
    """每 10s 续约 worker 状态 + 推日志(on_tick),写 worker_heartbeat_at,转发步骤进度事件。
    不覆盖步骤自己写的 updated_at,否则 check_stuck 失效。Subprocess/Docker runner 共用。"""
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


def _build_subprocess_env(ctx: StepContext) -> dict:
    """DENYLIST 构造子进程 env:继承全量系统 env,剥离控制面密钥;
    非 ai 池再剥离 AI 密钥;始终补 STEP_EXEC_ID。HTTPS_PROXY 等系统变量自然保留。"""
    env = {
        **os.environ,
        "STEP_EXEC_ID": ctx.exec_id,
        "STEP_JOB_ID": ctx.job_id,
        "STEP_SCOPE_KEY": ctx.scope_key,
        "STEP_PART_ID": ctx.part_id or "",
        **ctx.extra_env,
    }
    for key in _CONTROL_PLANE_SECRETS:
        env.pop(key, None)
    if ctx.pool != "ai":
        for key in _AI_KEY_ENV:
            env.pop(key, None)
    return env


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
            container_work_root=os.environ.get("WORK_DIR", "/tmp/flori-work"),
            registry=os.environ.get("FLORI_STEP_REGISTRY"),
        )
    return SubprocessStepRunner()
