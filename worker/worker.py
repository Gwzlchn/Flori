"""Worker：从资源池队列自取任务，执行步骤脚本，上报结果。

worker 只依赖 WorkerTransport(协调/状态后端)与 StorageBackend(产物),不直连
redis/db。注入 RedisTransport(单机直连)或 GatewayTransport(出站 HTTPS)。
"""

from __future__ import annotations

import asyncio
import hashlib
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
from shared.runner_ops import parse_style_tags
from shared.storage import StorageBackend
from worker.step_runner import StepContext, create_step_runner
from worker.transport import WorkerTransport, default_worker_id_file

logger = structlog.get_logger(component="worker")

# worker 类型 → 订阅的池(拓扑权威,不在 pools.yaml;新增/重命名池在此维护)。
# "io" 类型 = 纯下载/出网 worker(只订 io 池);类型名即其唯一订阅的池,语义诚实。
WORKER_POOLS: dict[str, list[str]] = {
    "io": ["io"],
    "cpu": ["scene", "cpu", "io"],
    "ai": ["ai", "io"],
    "gpu": ["gpu", "scene", "cpu", "io"],
}


def _resolve_worker_id(worker_type: str) -> str:
    """解析 worker 稳定身份。

    1) 设了 WORKER_NAME → 确定性派生 id = {type}-{sha256(WORKER_NAME)[:8]}。重装/删缓存/重注册
       永远同一 id(不依赖缓存文件),同名同 id、不同名不撞——同机多 worker 各给一个唯一名即可。
    2) 否则回退缓存:读 id 文件(默认 /data/workers/worker.id),无则随机 {type}-{8hex} 写回,
       靠缓存文件跨重启稳定。

    为何要稳定:重启被当全新 worker → 监控刷幽灵行、docker reap_orphans(label flori.worker={id})
    无法跨重启命中残留容器。gateway 模式 register 仍可返回另一 id 覆盖(以服务端为准)。

    无状态部署:gateway 模式(只设 GATEWAY_URL)+ WORKER_NAME 时,id 确定性派生、不依赖任何
    本地文件,可【不挂 /data 卷】纯出站 HTTPS 跑(configs 在镜像、work_dir 在 /tmp、产物经网关)。
    此时缓存文件写不了是预期的,降级为 debug 不报 warning。"""
    id_file = Path(default_worker_id_file())
    name = os.environ.get("WORKER_NAME", "").strip()
    if name:
        worker_id = f"{worker_type}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"
    else:
        try:
            cached = id_file.read_text().strip()
            if cached:
                return cached
        except OSError:
            pass
        worker_id = generate_worker_id(worker_type)
    try:
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(worker_id)
    except OSError:
        # WORKER_NAME 下 id 确定性,缓存文件可选——写不了是无状态部署的常态,不算错(debug);
        # 随机 id 模式写不了才会每次重启换 id,故 warn。
        if name:
            logger.debug("worker_id_cache_skipped", worker_id=worker_id)
        else:
            logger.warning("worker_id_persist_failed", worker_id=worker_id)
    return worker_id


def _claude_logged_in() -> bool:
    """claude-cli 是否【真有可用凭证】(订阅登录态)。token 落在 $HOME/.claude/.credentials.json
    (claude-cli 用 refreshToken 自动续期就地回写)。仅判二进制在不在会误标,见 auto_discover_tags。"""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    cred = Path(home) / ".claude" / ".credentials.json"
    try:
        return cred.is_file() and cred.stat().st_size > 0
    except OSError:
        return False


def auto_discover_tags() -> set[str]:
    tags = set()
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    # claude-cli/vision 须【真能用】才标,而非"镜像里有 claude 二进制就标":否则纯 gateway worker
    # (镜像自带 claude 但无凭证)会误标,一旦作 ai worker 就会认领 11_smart/取证/评审再因无登录失败。
    # 判据:二进制在 且 (订阅已登录 或 有 ANTHROPIC_API_KEY)。
    claude_ready = bool(shutil.which("claude")) and (has_anthropic_key or _claude_logged_in())
    if has_anthropic_key or claude_ready:
        tags.add("vision")
    if claude_ready:
        tags.add("claude-cli")
    if os.environ.get("DEEPSEEK_API_KEY"):
        tags.add("text-only")
    from steps.utils.device import has_nvidia_gpu
    if has_nvidia_gpu():  # PATH 感知 + 真实探测,与 steps.utils.device 单一判据(审计 R-L28)
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
        concurrency: int = 1,
    ):
        self.transport = transport
        self.config = config
        self.storage = storage
        self.worker_type = worker_type
        # 稳定身份:重启复用缓存 id(见 _resolve_worker_id);gateway 模式 register 后可能被
        # 服务端返回的 id 覆盖(worker.py:run 里 self.worker_id = await transport.register(...))。
        self.worker_id = _resolve_worker_id(worker_type)
        self.pools = pools
        self.tags = tags
        self.reject_tags = reject_tags
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "0"))
        # 本机并发度:同时在跑几个 step。异构机器据此自报容量(强机调大,弱机=1)。
        # 全局每池槽位(pools.yaml limit)仍是系统级天花板,本数只决定单 worker 的并行上限。
        self.concurrency = max(1, concurrency)
        self._shutdown = False
        self.runner = create_step_runner(self.worker_id)

    # ── 生命周期 ──

    async def run(self) -> None:
        await self.register()
        # runner 在 __init__ 用初始(可能随机)id 创建;register 后可能拿到稳定身份(gateway
        # WORKER_ID_FILE 缓存 id),同步给 runner 使容器 label 与 reap 用同一稳定 id。
        if hasattr(self.runner, "_worker_id"):
            self.runner._worker_id = self.worker_id
        # docker 模式:启动时清一次本 worker 残留容器(崩溃重启遗留)。稳定 id(gateway)下可命中
        # 跨重启残留;非 gateway 模式 id 每次随机,只能清同进程内残留——属已知边界。SubprocessRunner 无此法。
        reap = getattr(self.runner, "reap_orphans", None)
        if reap is not None:
            try:
                await asyncio.to_thread(reap)
            except Exception:
                logger.warning("reap_orphans_failed", worker_id=self.worker_id, exc_info=True)
        logger.info(
            "worker_start", worker_id=self.worker_id,
            type=self.worker_type, pools=self.pools, concurrency=self.concurrency,
            tags=sorted(self.tags), reject_tags=sorted(self.reject_tags),
        )
        try:
            await asyncio.gather(
                self.heartbeat_loop(),
                *[self._claim_loop(i) for i in range(self.concurrency)],
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
        # 心跳节拍读 config(单一事实源);此前硬编码 10s、配置项无人读。
        interval = int((self.config.pools.get("worker_status") or {}).get("heartbeat_interval_sec", 10))
        while not self._shutdown:
            try:
                await self.transport.heartbeat(self.worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 瞬态 redis/网络抖动不应经 gather 杀掉整个 worker(对照 scheduler._event_loop 容错):
                # 记日志后继续,下一拍重试;丢几拍由 worker_status.online_window(30s)容忍。
                logger.warning("heartbeat_failed", worker_id=self.worker_id, exc_info=True)
            await asyncio.sleep(interval)

    # ── 主循环 ──

    async def _claim_loop(self, slot: int = 0) -> None:
        """单条"认领→执行"循环。并发度>1 时 run() 起多条,共享 transport/storage/runner;
        各条独立认领+执行一个 step(全局每池槽位仍是系统级上限,本循环只占其中一个)。
        idle_timeout 由各条独立计时,全部超时退出 → worker 退出。"""
        last_task_time = time.time()
        while not self._shutdown:
            task = await self.transport.request_step(
                self.worker_id, self.pools, self._pool_limits(),
                self.tags, self.reject_tags,
            )
            if task:
                last_task_time = time.time()
                try:
                    await self.execute(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 单任务异常绝不杀主循环:execute 内部已尽量 report_failed/release;此处兜底
                    # 极端情形(如 execute 自身的上报/release 逃逸),记日志后续跑(审计 I-H3)。
                    logger.exception(
                        "execute_escaped_error", worker_id=self.worker_id,
                    )
            else:
                if self.idle_timeout and time.time() - last_task_time > self.idle_timeout:
                    logger.info("idle_timeout_exit", worker_id=self.worker_id, slot=slot)
                    break
                await asyncio.sleep(1)

    def _pool_limits(self) -> dict[str, int]:
        # 每池槽位上限(从 config 算好传给 transport,transport 不持有 config)。
        return {
            pool: cfg.get("limit", 999)
            for pool, cfg in self.config.pools.get("pools", {}).items()
        }

    # ── 任务执行 ──

    async def execute(self, claim: dict) -> None:
        job_id = claim["job_id"]
        step = claim["step"]
        pool = claim["pool"]
        exec_id = claim["exec_id"]

        start = time.time()
        work_dir = None
        try:
            work_dir = await self.storage.pull(job_id, step)

            # pipeline/domain/style_tags:gateway 模式服务端已塞进 claim,直连模式在此回读。
            # 读失败会被本 try 接住转 report_failed,不冲垮主循环(保留旧的故障隔离)。
            pipeline = claim.get("pipeline") or await self.transport.get_job_pipeline(job_id)
            if "domain" in claim:
                domain = claim["domain"]
                style_tags = claim.get("style_tags") or []
            else:
                job_info = await self.transport.get_job_info(job_id)
                domain = job_info.get("domain", "general")
                style_tags = parse_style_tags(job_info.get("style_tags", "[]"))
            if not isinstance(style_tags, list):
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
            image = raw.get("image", "flori/step-base")
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
                # 续约:让 DB/Redis 里的 "当前 task" 秒级新鲜 + 刷步进度心跳 + 推送运行中日志。
                # 步进度心跳每 10s(仅子进程存活时由 monitor 调用),供 scheduler.check_stuck
                # 对远程 job(产物不落调度器盘)判进度停滞。
                await self.transport.update_status(self.worker_id, "busy", job_id, step)
                await self.transport.report_step_alive(job_id, step)
                await self._push_step_log(job_id, step, work_dir)

            try:
                returncode, stderr = await self.runner.run_step(ctx, on_progress, on_tick)
            finally:
                # 不论成功/失败/超时,都把本步产物(含日志)推回存储,失败也能在前端看日志排错。
                await self._push_safe(job_id, step, work_dir)
            duration = time.time() - start

            if returncode == 0:
                await self._collect_usage(job_id, step, work_dir)
                await self.transport.report_done(claim, duration, start)
                logger.info(
                    "step_done", worker_id=self.worker_id,
                    job_id=job_id, step=step, duration=round(duration, 1),
                )
            else:
                error_msg = stderr[-500:] if stderr else "unknown error"
                error_type = self._parse_error_type(work_dir, step)
                await self.transport.report_failed(
                    claim, error_msg, error_type, duration, start, count_stats=True,
                )
                logger.warning(
                    "step_failed", worker_id=self.worker_id,
                    job_id=job_id, step=step, error=error_msg[:200],
                )

        except asyncio.TimeoutError:
            duration = time.time() - start
            await self.transport.report_failed(
                claim, "timeout", "timeout", duration, start, count_stats=False,
            )
            logger.warning(
                "step_timeout", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        except Exception as e:
            duration = time.time() - start
            error_msg = str(e)[:500]
            await self.transport.report_failed(
                claim, error_msg, "processing", duration, start, count_stats=False,
            )
            logger.exception(
                "step_unexpected_error", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        finally:
            if work_dir:
                await self.storage.cleanup(job_id, step, work_dir)
            await self.transport.release(claim)

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
        # usage 仅统计/计费侧效应:解析或上报失败只降级为"统计不准",绝不让 returncode==0 的成功
        # 步骤经 execute 的 except 翻成 failed(审计 I-H2)。record_ai_usage 已在 gateway 侧 best-effort。
        try:
            usages = collect_usage_from_file(work_dir / "logs", step)
            for usage in usages:
                await self.transport.record_ai_usage(usage)
        except Exception:
            logger.warning(
                "collect_usage_failed", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )
