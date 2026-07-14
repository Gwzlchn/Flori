"""Worker: 从资源池队列自取任务,执行步骤脚本,上报结果。

worker 只依赖 WorkerTransport(协调/状态后端)与 StorageBackend(产物),不直连
redis/db。注入 RedisTransport(单机直连)或 GatewayTransport(出站 HTTPS)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import redis.exceptions
import structlog

from shared.ai_gateway import AIGateway, collect_usage_from_file
from shared.config import AppConfig, build_step_config
from shared.models import AIUsage, DEFAULT_AI_MODEL, DEFAULT_AI_PROVIDER, LLMRequest, generate_worker_id
from shared.runner_ops import parse_style_tags
from shared.storage import StorageBackend
from shared.sysload import collect_node_load
from shared.version import FLORI_VERSION
from worker.step_runner import StepContext, create_step_runner
from worker.transport import (
    WorkerAuthRejected,
    WorkerContractError,
    WorkerFatalError,
    WorkerTransport,
    default_worker_id_file,
)

logger = structlog.get_logger(component="worker")


def compute_effective_timeout(
    base: int, per_min: int | None, duration_sec: float | None, cap: int | None = None,
) -> int:
    """步超时随媒体时长伸缩(纯函数,便于测).

    有 per_min 且能读到 duration 时,返回 max(base, ceil(minutes)*per_min),再按 cap 截断;
    否则原样返回 base.用于长音频/视频 whisper:固定 1800s 会把无 GPU 的长集硬杀."""
    import math
    if not per_min or not duration_sec or duration_sec <= 0:
        return base
    scaled = math.ceil(duration_sec / 60.0) * int(per_min)
    eff = max(int(base), scaled)
    if cap and cap > 0:
        eff = min(eff, int(cap))
    return eff


def _read_media_duration(work_dir: Path) -> float | None:
    """从 input/metadata.json(01_download 写)读 duration_sec.缺文件/字段返回 None."""
    meta = work_dir / "input" / "metadata.json"
    if not meta.is_file():
        return None
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    dur = d.get("duration_sec")
    return float(dur) if isinstance(dur, (int, float)) else None


def _worker_spec() -> dict:
    """worker 自报版本 + 机器配置.版本取构建时注入的 FLORI_VERSION,便于查代码漂移."""
    from shared.version import FLORI_VERSION
    spec: dict = {
        "version": FLORI_VERSION,
        "cpu": os.cpu_count(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    spec["mem_mb"] = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    return spec

# 能力用 --pools 显式表达(worker/main.py),路由按 pool 走.
# 多池强机直接 `--pools gpu cpu`,无主次,无隐式 fallback.


def _resolve_worker_id(worker_type: str) -> str:
    """解析 worker 稳定身份。

    1. 设了 WORKER_NAME 时,确定性派生 id = {type}-{sha256(WORKER_NAME)[:8]}.
       重装,删缓存或重注册仍是同一 id,不依赖缓存文件.同名同 id,不同名不撞,同机多 worker 各给一个唯一名即可.
    2) 否则回退缓存:读 id 文件(默认 /data/workers/worker.id),无则随机 {type}-{8hex} 写回,
       靠缓存文件跨重启稳定.

    为何要稳定:重启若被当成全新 worker,监控会刷幽灵行,docker reap_orphans(label flori.worker={id})
    无法跨重启命中残留容器.gateway 模式 register 仍可返回另一 id 覆盖(以服务端为准).

    Gateway 模式仍先用 WORKER_NAME 派生首启身份,但 register/resume 成功后必须能持久化
    worker.id 和 worker.token。长期 token 是后续启动的唯一凭证,不能依赖 registration token 复活。"""
    id_file = Path(default_worker_id_file())
    # worker_type 多池派生时形如 "cpu+gpu";id 会进 redis key / 容器 label,前缀里 '+' 换 '-' 保守。
    safe_type = worker_type.replace("+", "-")
    name = os.environ.get("WORKER_NAME", "").strip()
    if name:
        worker_id = f"{safe_type}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"
    else:
        try:
            cached = id_file.read_text().strip()
            if cached:
                return cached
        except OSError:
            pass
        worker_id = generate_worker_id(safe_type)
    try:
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(worker_id)
    except OSError:
        # WORKER_NAME 下这里仍可确定性派生首启 id。GatewayTransport 注册成功后会再强制持久化
        # id/token;随机 id 模式写不了才会每次重启换 id,故 warn.
        if name:
            logger.debug("worker_id_cache_skipped", worker_id=worker_id)
        else:
            logger.warning("worker_id_persist_failed", worker_id=worker_id)
    return worker_id


def _claude_logged_in() -> bool:
    """claude-cli 是否真有可用凭证(CLI 登录态)。token 落在 $HOME/.claude/.credentials.json
    (claude-cli 用 refreshToken 自动续期就地回写)。仅判二进制在不在会误标,见 auto_discover_tags。"""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    cred = Path(home) / ".claude" / ".credentials.json"
    try:
        return cred.is_file() and cred.stat().st_size > 0
    except OSError:
        return False


def _codex_logged_in() -> bool:
    """codex-cli 是否有可用凭证.file storage 凭证在 `$CODEX_HOME/auth.json` 或 `$HOME/.codex/auth.json`."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    codex_home = os.environ.get("CODEX_HOME") or str(Path(home) / ".codex")
    cred = Path(codex_home) / "auth.json"
    try:
        return cred.is_file() and cred.stat().st_size > 0
    except OSError:
        return False


def _probe_reachable(url: str, timeout: float = 6.0, retries: int = 2) -> bool:
    """试连 URL(走本机网络,含自带代理)。拿到任何 HTTP 响应(含 4xx/5xx)= 可达;
    仅网络层失败(连不上/超时/DNS)= 不可达。用于自动判定 net-zone。"""
    if not url:
        return False
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "flori-netprobe"})
    for _ in range(max(1, retries)):
        try:
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except urllib.error.HTTPError:
            return True   # 有 HTTP 响应(403/404 等)= 到得了
        except Exception:
            continue
    return False


def _probe_net_zones() -> set[str]:
    """自动探测本 worker 可达的网络区域(net-cn / net-global).
    探针 URL 不写死,读取 env(base.Dockerfile 设默认,部署可覆盖);
    NET_ZONES 显式覆盖(如香港 worker 设 NET_ZONES=global)则跳过探测,防误判/离线."""
    override = os.environ.get("NET_ZONES", "").strip()
    if override:
        return {f"net-{z.strip()}" for z in override.split(",") if z.strip()}
    zones: set[str] = set()
    if _probe_reachable(os.environ.get("NET_PROBE_CN", "https://api.bilibili.com/x/web-interface/nav")):
        zones.add("net-cn")
    if _probe_reachable(os.environ.get("NET_PROBE_GLOBAL", "https://github.com")):
        zones.add("net-global")
    return zones


def auto_discover_tags() -> set[str]:
    from shared.ai_routing import provider_capability_tags, provider_required_tag

    tags = set()
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    # claude-cli/vision 须真能用才标,而非"镜像里有 claude 二进制就标":否则纯 gateway worker
    # (镜像自带 claude 但无凭证)会误标,一旦作 ai worker 就会认领 11_smart/取证/评审再因无登录失败.
    # 判据:二进制在 且 (CLI 已登录 或 有 ANTHROPIC_API_KEY).
    claude_ready = bool(shutil.which("claude")) and (has_anthropic_key or _claude_logged_in())
    if has_anthropic_key or claude_ready:
        tags.add("vision")
    if has_anthropic_key:
        tags.add(provider_required_tag("anthropic"))
    if claude_ready:
        tags.add(provider_required_tag("claude-cli"))
        tags.update(provider_capability_tags("claude-cli"))
    codex_ready = bool(shutil.which("codex")) and _codex_logged_in()
    if codex_ready:
        tags.add(provider_required_tag("codex-cli"))
        tags.add("vision")
    if os.environ.get("DEEPSEEK_API_KEY"):
        tags.add(provider_required_tag("deepseek"))
        tags.add("text-only")
    if os.environ.get("KIMI_API_KEY"):
        tags.add(provider_required_tag("kimi"))
        tags.add("text-only")
    if os.environ.get("OPENAI_API_KEY"):
        tags.add(provider_required_tag("openai"))
        tags.add("vision")
    from steps.utils.device import has_nvidia_gpu
    if has_nvidia_gpu():  # PATH 感知 + 真实探测,与 steps.utils.device 单一判据
        tags.add("gpu")
    if os.environ.get("OLLAMA_URL"):
        tags.add("local")
    # 网络可达区域:worker 自己探出 net-cn / net-global,scheduler 按 URL 区域匹配。
    # 代理/SESSDATA 等都是 worker 本地的事,非路由 tag。
    # B站 SESSDATA 经 per-job 凭证文件传给 worker,下载步 step_01 自读。
    tags |= _probe_net_zones()
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
        # 服务端返回的 id 覆盖,register() 里回写 self.worker_id.
        self.worker_id = _resolve_worker_id(worker_type)
        self.pools = pools
        self.tags = tags
        self.reject_tags = reject_tags
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "0"))
        # 本机并发度:同时在跑几个 step.异构机器据此自报容量(强机调大,弱机=1).
        # 全局每池槽位(pools.yaml limit)仍是系统级天花板,本数只决定单 worker 的并行上限.
        self.concurrency = max(1, concurrency)
        self._shutdown = False
        # Gateway worker 的长期 token 被拒时不能自愈复活;跨 slot 用锁压成一条 fatal 日志。
        self._auth_lock = asyncio.Lock()
        self._fatal_error: WorkerFatalError | None = None
        # 中心配置热应用:已生效的 cfg_rev(注册/心跳带回期望配置,rev 更高才应用,幂等).
        self._cfg_applied_rev = 0
        self.runner = create_step_runner(self.worker_id)

    # 生命周期

    async def run(self) -> None:
        await self.register()
        # 绑身份到 contextvars,本进程后续所有日志自带 worker_id/type/host/version(排障一眼知道哪台/什么版本).
        structlog.contextvars.bind_contextvars(
            worker_id=self.worker_id, worker_type=self.worker_type,
            host=socket.gethostname(), version=FLORI_VERSION,
        )
        # runner 在 __init__ 用初始(可能随机)id 创建;register 后可能拿到稳定身份(gateway
        # WORKER_ID_FILE 缓存 id),同步给 runner 使容器 label 与 reap 用同一稳定 id。
        if hasattr(self.runner, "_worker_id"):
            self.runner._worker_id = self.worker_id
        # docker 模式:启动时清一次本 worker 残留容器(崩溃重启遗留).稳定 id(gateway)下可命中
        # 跨重启残留;非 gateway 模式 id 每次随机,只能清同进程内残留,属已知边界.SubprocessRunner 无此法.
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
                self._claim_supervisor(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self.transport.update_status(self.worker_id, "offline")
            except WorkerAuthRejected as e:
                if self._fatal_error is None:
                    self._fatal_error = e
            logger.info("worker_exit", worker_id=self.worker_id)
        if self._fatal_error is not None:
            raise self._fatal_error

    def shutdown(self) -> None:
        logger.info("worker_shutdown", worker_id=self.worker_id)
        self._shutdown = True

    # 注册 + 心跳

    async def register(self) -> None:
        # gateway 注册可能返回缓存身份(重启复用同一 id);runner 已用旧 id 创建但子进程忽略 worker_id,无碍。
        # 连不上网关/redis(部署时 api 比 worker 晚起几百 ms,启动顺序竞态)时固定间隔 WARN 重试,
        # 不让首拍 ConnectError 抛到 main 崩进程白白重启.
        # 网络层失败与 5xx 可退避重试;4xx/auth/contract/config 直接交入口结构化退出。
        retry_sec = float(os.environ.get("REGISTER_RETRY_SEC", "3"))
        while not self._shutdown:
            try:
                self.worker_id = await self.transport.register(
                    worker_id=self.worker_id, worker_type=self.worker_type,
                    pools=self.pools, tags=self.tags, reject_tags=self.reject_tags,
                    hostname=socket.gethostname(), now=datetime.now(timezone.utc),
                    concurrency=self.concurrency, spec=_worker_spec(),
                )
                # 注册响应携带的中心期望配置(transport 属性侧带,免改 ABC 返回签名):
                # 首拍即齐,claim supervisor 起跑前生效,最小三参数裸启也能吃到中心并发/池.
                self._apply_desired_config(
                    getattr(self.transport, "initial_config", None))
                return
            except WorkerFatalError:
                raise
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status is not None and 500 <= status < 600:
                    logger.warning(
                        "register_http_retry", worker_id=self.worker_id,
                        host=socket.gethostname(), endpoint=str(e.request.url),
                        status=status, error=str(e)[:200], retry_sec=retry_sec,
                    )
                    await asyncio.sleep(retry_sec)
                    continue
                raise WorkerContractError(
                    "worker register/resume rejected by gateway",
                    status_code=status,
                    endpoint=str(e.request.url),
                    reason="worker_register_rejected",
                ) from e
            except (httpx.TransportError, redis.exceptions.ConnectionError,
                    redis.exceptions.TimeoutError) as e:
                # 连不上:WARN 一条摘要而非整屏 traceback,固定间隔重试.不用指数退避,本身差不了几秒.
                logger.warning(
                    "register_connect_retry", worker_id=self.worker_id,
                    host=socket.gethostname(), endpoint="/api/runner/register",
                    error=str(e)[:200], retry_sec=retry_sec,
                )
                await asyncio.sleep(retry_sec)

    async def heartbeat_loop(self) -> None:
        # 心跳节拍读 config(单一事实源),不在此硬编码.
        interval = int((self.config.pools.get("worker_status") or {}).get("heartbeat_interval_sec", 10))
        while not self._shutdown:
            try:
                # 本机 live 负载(cpu%/mem%/loadavg,纯 /proc,便宜非阻塞);采集失败=各项 None,不致命.
                cfg_payload = await self.transport.heartbeat(
                    self.worker_id, load=collect_node_load(),
                    applied_cfg_rev=self._cfg_applied_rev,
                    concurrency=self.concurrency,
                )
                self._apply_desired_config(cfg_payload)
            except asyncio.CancelledError:
                raise
            except WorkerAuthRejected as e:
                await self._handle_auth_failure(e)
                break
            except Exception:
                # 瞬态 redis/网络抖动不应经 gather 杀掉整个 worker(对照 scheduler._event_loop 容错):
                # 记日志后继续,下一拍重试;丢几拍由 worker_status.online_window(30s)容忍。
                logger.warning("heartbeat_failed", worker_id=self.worker_id, exc_info=True)
            await asyncio.sleep(interval)

    def _apply_desired_config(self, payload: dict | None) -> None:
        """中心期望配置热应用(注册响应/每拍心跳带回)。rev 不高于已生效值即跳过(幂等);
        当前只接受 concurrency。扩=即刻补新 slot,缩=超编 slot 跑完当前任务自然退出。"""
        if not payload:
            return
        rev = int(payload.get("cfg_rev") or 0)
        cfg = payload.get("desired_config")
        if not cfg or rev <= self._cfg_applied_rev:
            return
        changed: dict = {}
        conc = cfg.get("concurrency")
        if isinstance(conc, int) and conc >= 1 and conc != self.concurrency:
            self.concurrency = conc
            changed["concurrency"] = conc
        self._cfg_applied_rev = rev
        logger.info(
            "worker_config_applied", worker_id=self.worker_id,
            cfg_rev=rev, **changed,
        )

    async def _claim_supervisor(self) -> None:
        """维持认领并行度 = self.concurrency(中心配置可热调):每 2s 对齐一次 slot 任务集.
        扩容补新 slot;缩容不打断,超编 slot 在 _claim_loop 循环顶自检退出(跑完当前任务).
        idle_timeout 语义保持:所有在编 slot 都闲退且无存活任务时,worker 整体退出(与旧
        gather 行为一致);未配 idle_timeout 时 done 视为意外损耗,原 slot 重生(崩溃兜底)."""
        tasks: dict[int, asyncio.Task] = {}
        idle_exited: set[int] = set()
        try:
            while not self._shutdown:
                want = max(1, self.concurrency)
                for slot, t in list(tasks.items()):
                    if t.done():
                        del tasks[slot]
                        if self.idle_timeout and slot < want:
                            idle_exited.add(slot)
                idle_exited = {x for x in idle_exited if x < want}
                if self.idle_timeout and len(idle_exited) >= want and not tasks:
                    self.shutdown()
                    break
                for slot in range(want):
                    if slot not in tasks and slot not in idle_exited:
                        tasks[slot] = asyncio.create_task(self._claim_loop(slot))
                await asyncio.sleep(2)
        finally:
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)

    async def _handle_auth_failure(self, error: WorkerAuthRejected | None = None) -> None:
        """收到 WorkerAuthRejected 时停机。长期 token 被拒后不得用 registration token 复活。"""
        async with self._auth_lock:
            if self._shutdown:
                return
            self._fatal_error = error or WorkerAuthRejected()
            logger.error(
                "worker_auth_rejected_exit", worker_id=self.worker_id,
                host=socket.gethostname(), version=FLORI_VERSION,
            )
            self._shutdown = True

    # 主循环

    async def _claim_loop(self, slot: int = 0) -> None:
        """单条认领并执行循环.并发度>1 时 run() 起多条,共享 transport/storage/runner;
        各条独立认领+执行一个 step(全局每池槽位仍是系统级上限,本循环只占其中一个).
        idle_timeout 由各条独立计时,全部超时退出时 worker 退出."""
        last_task_time = time.time()
        while not self._shutdown:
            if slot >= max(1, self.concurrency):
                # 中心配置缩并发:超编 slot 跑完当前任务后在此退位,绝不打断在跑步骤.
                logger.info(
                    "claim_slot_retired", worker_id=self.worker_id, slot=slot,
                    concurrency=self.concurrency,
                )
                break
            try:
                task = await self.transport.request_step(
                    self.worker_id, self.pools, self._pool_limits(),
                    self.tags, self.reject_tags,
                )
            except WorkerAuthRejected as e:
                await self._handle_auth_failure(e)
                break
            if task:
                last_task_time = time.time()
                try:
                    await self.execute(task)
                except asyncio.CancelledError:
                    raise
                except WorkerAuthRejected as e:
                    await self._handle_auth_failure(e)
                    break
                except Exception:
                    # 单任务异常绝不杀主循环:execute 内部已尽量 report_failed/release;此处兜底
                    # 极端情形(如 execute 自身的上报/release 逃逸),记日志后续跑.
                    logger.exception(
                        "execute_escaped_error", worker_id=self.worker_id,
                    )
            else:
                if self.idle_timeout and time.time() - last_task_time > self.idle_timeout:
                    logger.info("idle_timeout_exit", worker_id=self.worker_id, slot=slot)
                    break
                await asyncio.sleep(1)

    def _pool_limits(self) -> dict[str, int]:
        # 每池槽位上限(从 config 算好传给 transport,transport 不持有 config).
        return {
            pool: cfg.get("limit", 999)
            for pool, cfg in self.config.pools.get("pools", {}).items()
        }

    async def _download_credentials_env(self, step: str, source: str) -> dict:
        """下载步的中心分发凭证写入子进程 env(docs/03 §1.7.1).仅 01_download 领取;
        按 source 只取所需(减少审计噪声),source 未知则两种都试.领取失败/未配置
        降级匿名;worker token 被拒时停机。"""
        if step != "01_download":
            return {}
        wanted: list[tuple[str, str]] = []
        if source.startswith("bili"):
            wanted = [("bili_sessdata", "BILI_SESSDATA")]
        elif source in ("youtube", "yt"):
            wanted = [("youtube_cookies", "FLORI_YT_COOKIES")]
        elif source in ("arxiv", "pdf", "http_article", "upload", "local"):
            return {}   # 非平台下载,无需凭证
        else:
            wanted = [("bili_sessdata", "BILI_SESSDATA"),
                      ("youtube_cookies", "FLORI_YT_COOKIES")]
        env: dict = {}
        for key, env_name in wanted:
            try:
                value = await self.transport.get_credential(key)
            except WorkerAuthRejected:
                raise
            except Exception as e:
                logger.warning("credential_env_skipped", key=key, error=str(e)[:120])
                continue
            if value:
                env[env_name] = value
        return env

    # 任务执行

    async def execute(self, claim: dict) -> None:
        # 独立 AI task(kind='ai')分流:不挂 job、不走 storage,单独执行(见 _execute_ai_task)。
        # 必须在任何 job-step 处理之前,因 ai claim 没有 job_id/work_dir。
        if claim.get("kind") == "ai":
            await self._execute_ai_task(claim)
            return

        job_id = claim["job_id"]
        step = claim["step"]
        pool = claim["pool"]
        exec_id = claim["exec_id"]

        start = time.time()
        work_dir = None
        auth_failed = False
        try:
            work_dir = await self.storage.pull(job_id, step)

            # pipeline/domain/style_tags/source:gateway 模式服务端已塞进 claim,直连模式在此回读。
            # 读失败会被本 try 接住转 report_failed,不冲垮主循环。
            pipeline = claim.get("pipeline") or await self.transport.get_job_pipeline(job_id)
            if "domain" in claim:
                domain = claim["domain"]
                style_tags = claim.get("style_tags") or []
                source = claim.get("source", "")
            else:
                job_info = await self.transport.get_job_info(job_id)
                domain = job_info.get("domain", "general")
                style_tags = parse_style_tags(job_info.get("style_tags", "[]"))
                source = job_info.get("source", "")
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
            # 超时随媒体时长伸缩(仅 pipeline 给了 timeout_per_min 的步,如 02_whisper):
            # 无 GPU 时长集 whisper 固定 1800s 会被硬杀。缺 metadata/duration 时退回静态 timeout。
            step_node = step_cfg["step"]
            effective_timeout = compute_effective_timeout(
                step_node["timeout_sec"],
                step_node.get("timeout_per_min"),
                _read_media_duration(work_dir),
                step_node.get("timeout_max_sec"),
            )
            if effective_timeout != step_node["timeout_sec"]:
                logger.info(
                    "dynamic_timeout", worker_id=self.worker_id, job_id=job_id, step=step,
                    base=step_node["timeout_sec"], effective=effective_timeout,
                )
            ctx = StepContext(
                job_id=job_id, step=step, work_dir=work_dir, exec_id=exec_id,
                step_cfg=step_cfg, module=module, image=image,
                timeout_sec=effective_timeout,
                pool=pool, use_gpu=use_gpu,
                extra_env=await self._download_credentials_env(step, source),
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

            returncode, stderr = await self.runner.run_step(ctx, on_progress, on_tick)
            duration = time.time() - start

            if returncode == 0:
                await self._collect_usage(job_id, step, work_dir)
                # 产物必须先成功推上中心存储,才报 done。否则上游标了 done 但产物没上去,
                # 下游步拉 work_dir 时 input_missing(如 candidates.json)。push 失败降级为步失败,
                # 重试时重新生成并推送,绝不在产物缺失时标完成。
                try:
                    await self.storage.push(job_id, step, work_dir)
                except WorkerAuthRejected:
                    raise
                except Exception as push_err:
                    await self.transport.report_failed(
                        claim,
                        f"artifact push failed: {type(push_err).__name__}: {push_err}"[:500],
                        "storage", duration, start, count_stats=False,
                    )
                    logger.warning(
                        "step_push_failed", worker_id=self.worker_id,
                        job_id=job_id, step=step, error=str(push_err)[:200],
                    )
                else:
                    await self.transport.report_done(claim, duration, start)
                    logger.info(
                        "step_done", worker_id=self.worker_id,
                        job_id=job_id, step=step, duration=round(duration, 1),
                    )
            else:
                # 步本身失败:失败也记用量(失败前已完成的 LLM 调用是真实开销,审计/计费不能缺账;
                # exec_id UNIQUE 幂等,重试不重复计),再 best-effort 推产物(含日志)便于前端排错,报 failed。
                await self._collect_usage(job_id, step, work_dir)
                await self._push_safe(job_id, step, work_dir)
                error_type, error_json_msg = self._parse_error(work_dir, step)
                # 兜底:子进程 stderr 为空时,用 .{step}.error.json 的 message(真实异常文本),
                # 避免前端只看到 "unknown error" 无从排错。
                error_msg = (stderr[-500:] if stderr else "") or error_json_msg[:500] or "unknown error"
                await self.transport.report_failed(
                    claim, error_msg, error_type, duration, start, count_stats=True,
                )
                logger.warning(
                    "step_failed", worker_id=self.worker_id,
                    job_id=job_id, step=step, error=error_msg[:200],
                )

        except WorkerAuthRejected:
            auth_failed = True
            raise

        except asyncio.TimeoutError:
            duration = time.time() - start
            if work_dir:
                await self._collect_usage(job_id, step, work_dir)  # 失败也记用量(超时前的调用是真实开销)
                await self._push_safe(job_id, step, work_dir)  # best-effort 推日志便于排错
            await self.transport.report_failed(
                claim, "timeout", "timeout", duration, start, count_stats=False,
            )
            logger.warning(
                "step_timeout", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

        except Exception as e:
            duration = time.time() - start
            if work_dir:
                await self._collect_usage(job_id, step, work_dir)  # 失败也记用量
                await self._push_safe(job_id, step, work_dir)  # best-effort 推日志便于排错
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
            if not auth_failed:
                await self.transport.release(claim)

    # 运行中日志推送

    async def _push_step_log(self, job_id: str, step: str, work_dir: Path) -> None:
        """把运行中日志推回存储。网络失败不致命,auth 失败停机。"""
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
        except WorkerAuthRejected:
            raise
        except Exception:
            logger.warning(
                "step_log_push_failed", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

    # 工具方法

    def _parse_error(self, work_dir: Path, step: str) -> tuple[str, str]:
        """从 .{step}.error.json 读 (error_type, message);失败上报在子进程 stderr 为空时据此兜底。"""
        error_file = work_dir / f".{step}.error.json"
        if error_file.exists():
            try:
                data = json.loads(error_file.read_text())
                return data.get("error_type", "unknown"), (data.get("message") or "")
            except (json.JSONDecodeError, OSError):
                pass
        return "unknown", ""

    async def _push_safe(self, job_id: str, step: str, work_dir: Path) -> None:
        """把本步产物推回存储。网络失败不遮蔽步骤错误,auth 失败停机。"""
        try:
            await self.storage.push(job_id, step, work_dir)
        except WorkerAuthRejected:
            raise
        except Exception:
            logger.warning(
                "storage_push_failed", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

    async def _collect_usage(self, job_id: str, step: str, work_dir: Path) -> None:
        # usage 仅统计/计费侧效应:解析或网络失败只降级为"统计不准",绝不让步骤结论翻转。
        # 成功与失败路径都调(失败步在挂之前完成的 LLM 调用是真实开销,必须入账;exec_id UNIQUE 幂等)。
        try:
            usages = collect_usage_from_file(work_dir / "logs", step)
            for usage in usages:
                usage.worker_id = self.worker_id   # 归因到执行节点(直连路径;网关路径 api 据 token 再认定)
                await self.transport.record_ai_usage(usage)
        except WorkerAuthRejected:
            raise
        except Exception:
            logger.warning(
                "collect_usage_failed", worker_id=self.worker_id,
                job_id=job_id, step=step,
            )

    # 独立 AI task(kind='ai')执行

    async def _execute_ai_task(self, claim: dict) -> None:
        """执行独立 AI task:复用 AIGateway 跑 claude,结果回 airesult:{task_id} 并 publish events:{task_id};
        详细 whitebox 审计落 ai_task_logs.失败回 {"error":...},绝不崩 worker.池槽由 finally 的 release 释放
        (release_step 的 ai 分支).不挂 job,不走 storage,claim 已内联 request/domain."""
        task_id = claim["task_id"]
        step_name = claim.get("step", "ai")
        exec_id = claim["exec_id"]
        domain = claim.get("domain")
        start = time.time()
        ts_start = datetime.now(timezone.utc)
        req = LLMRequest.from_jsonable(claim.get("request", {}))
        provider_name = claim.get("provider") or DEFAULT_AI_PROVIDER
        model_name = claim.get("model") or DEFAULT_AI_MODEL
        try:
            try:
                gateway = AIGateway(
                    self.config.providers,
                    {"steps": [{"name": step_name,
                                "ai": {"primary": {"provider": provider_name, "model": model_name}}}]},
                )
                resp = await gateway.call(step_name, req)
                duration = time.time() - start
                await self.transport.set_ai_result(task_id, resp.to_jsonable())
                await self._record_ai_task_usage(task_id, step_name, exec_id, resp)
                await self._write_ai_task_audit(
                    task_id, step_name, domain, exec_id, req, resp, None, ts_start, duration,
                    provider_name, model_name,
                )
                await self.transport.publish_step_event(
                    f"events:{task_id}", {"event": "ai_task_done", "task_id": task_id, "step": step_name})
                logger.info("ai_task_done", worker_id=self.worker_id, task_id=task_id,
                            step=step_name, provider=resp.provider, duration=round(duration, 1))
            except Exception as e:
                duration = time.time() - start
                err = str(e)[:500]
                # 失败:回执 {"error"} + 审计(含尝试链/当时 prompt)+ 完成事件;全 best-effort,绝不崩 worker.
                for op in (
                    lambda: self.transport.set_ai_result(task_id, {"error": err}),
                    lambda: self._write_ai_task_audit(
                        task_id, step_name, domain, exec_id, req, None, e, ts_start, duration,
                        provider_name, model_name,
                    ),
                    lambda: self.transport.publish_step_event(
                        f"events:{task_id}", {"event": "ai_task_failed", "task_id": task_id, "error": err[:200]}),
                ):
                    try:
                        await op()
                    except Exception:
                        pass
                logger.warning("ai_task_failed", worker_id=self.worker_id, task_id=task_id, error=err[:200])
        finally:
            await self.transport.release(claim)

    async def _record_ai_task_usage(self, task_id: str, step_name: str, exec_id: str, resp) -> None:
        """AI task 成本归因(与白盒审计并存):record_ai_usage(job_id=null, step=step_name).失败仅降级统计."""
        try:
            await self.transport.record_ai_usage(AIUsage(
                exec_id=exec_id, provider=resp.provider, model=resp.model,
                job_id=None, step=step_name, worker_id=self.worker_id,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                cache_creation_input_tokens=resp.cache_creation_input_tokens,
                cache_read_input_tokens=resp.cache_read_input_tokens,
                cost_usd=resp.cost_usd, duration_sec=resp.duration_sec,
                num_turns=resp.num_turns, cached=resp.cached,
            ))
        except Exception:
            logger.warning("ai_task_usage_failed", worker_id=self.worker_id, task_id=task_id)

    # AI task transcript 内嵌上限:record_json 是 DB 文本列,个人工具尺寸可接受;超出截断并标记.
    _TRANSCRIPT_CAP = 5 * 1024 * 1024

    def _load_ai_task_transcript(self, resp, attempts) -> dict:
        """agentic 全轨迹白盒(AI task 版):CLI 会话 transcript 全文内嵌 record_json
        (AI task 不挂 job,无 storage 产物区,没有 sidecar 可放).>5MB 截断标记 truncated.
        失败调用经尝试链 transcript_path 同样回收;找不到时返回 jsonl=None + reason,不失败."""
        path = getattr(resp, "transcript_path", None) if resp is not None else None
        if not path:
            for a in reversed(attempts or []):
                if a.get("transcript_path"):
                    path = a["transcript_path"]
                    break
        if not path:
            return {"jsonl": None, "reason": "no transcript (session log unavailable)"}
        try:
            data = Path(path).read_text(encoding="utf-8", errors="replace")
            truncated = len(data) > self._TRANSCRIPT_CAP
            return {"jsonl": data[:self._TRANSCRIPT_CAP], "truncated": truncated,
                    "turns": data.count("\n"), "path": str(path)}
        except Exception as e:
            return {"jsonl": None, "reason": f"read failed: {e}"[:200]}

    async def _write_ai_task_audit(
        self, task_id, step_name, domain, exec_id, req, resp, error, ts_start, duration,
        requested_provider=DEFAULT_AI_PROVIDER, requested_model=DEFAULT_AI_MODEL,
    ) -> None:
        """构建并落一条 AI task 白盒审计,对齐 DAG ai_logs 的路由/尝试链/渲染 prompt/输出/raw/用量/全轨迹."""
        ok = error is None and resp is not None
        if resp is not None:
            attempts, tier_used, raw = resp.attempts, resp.tier_used, resp.raw
        else:
            attempts, tier_used, raw = (getattr(error, "attempts", []) or []), None, None
        record = {
            "task_id": task_id, "kind": "ai", "step": step_name, "domain": domain, "exec_id": exec_id,
            "ok": ok, "error": (str(error)[:1000] if error else None),
            "ts_start": ts_start.isoformat(), "ts_end": datetime.now(timezone.utc).isoformat(),
            "flori": {
                "image_tag": os.environ.get("FLORI_IMAGE_TAG") or os.environ.get("IMAGE_TAG"),
                "version": os.environ.get("FLORI_VERSION"),
                "git_commit": os.environ.get("FLORI_GIT_COMMIT"),
            },
            "routing": {
                "requested": {"provider": requested_provider, "model": requested_model},
                "tier_used": tier_used, "attempts": attempts,
            },
            "prompt": {
                "system": req.system, "messages": req.messages,
                "max_tokens": req.max_tokens, "temperature": req.temperature,
                "allowed_tools": req.allowed_tools,
            },
            "output": (resp.content if resp is not None else None),
            "raw": raw,
            # agentic 全轨迹(中间轮工具轨迹)内嵌:{"jsonl": 全文, "turns", "truncated"} 或 {"jsonl": None, "reason"}。
            "transcript": self._load_ai_task_transcript(resp, attempts),
            "usage": ({
                "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
                "cache_creation_input_tokens": resp.cache_creation_input_tokens,
                "cache_read_input_tokens": resp.cache_read_input_tokens,
                "cost_usd": resp.cost_usd, "duration_sec": resp.duration_sec,
                "num_turns": resp.num_turns, "cached": resp.cached, "session_id": resp.session_id,
            } if resp is not None else None),
        }
        log = {
            "task_id": task_id, "exec_id": exec_id, "step_name": step_name, "domain": domain,
            "provider": (resp.provider if resp is not None else requested_provider),
            "model": (resp.model if resp is not None else requested_model),
            "ok": ok, "error": (str(error)[:1000] if error else None),
            "input_tokens": (resp.input_tokens if resp else 0),
            "output_tokens": (resp.output_tokens if resp else 0),
            "cache_creation_input_tokens": (resp.cache_creation_input_tokens if resp else 0),
            "cache_read_input_tokens": (resp.cache_read_input_tokens if resp else 0),
            "cost_usd": (resp.cost_usd if resp else 0.0),
            "duration_sec": (resp.duration_sec if resp else duration),
            "num_turns": (resp.num_turns if resp else 0),
            "record": record,
            "created_at": ts_start.isoformat(),
        }
        await self.transport.record_ai_task_log(log)
