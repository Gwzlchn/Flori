"""数据模型 + 枚举 + ID 生成。"""

from __future__ import annotations

import enum
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class StepStatus(str, enum.Enum):
    WAITING = "waiting"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskKind(str, enum.Enum):
    """队列里 task 的两类:pipeline 步骤(载荷/结果走 storage)vs 独立 AI 任务(内联载荷/结果)。
    step task 的 JSON 不带 kind 字段(向后兼容,缺省即 STEP);ai task 带 kind='ai'。"""
    STEP = "step"   # pipeline 步骤 task:{job_id, step},载荷/结果走 storage,job:{id}:steps 状态机驱动
    AI = "ai"       # 独立 AI task:{task_id, request},无 job、不走 storage,结果内联回 airesult:{task_id}


@dataclass
class Job:
    id: str
    content_type: str
    pipeline: str
    status: JobStatus = JobStatus.PENDING
    collection_id: str | None = None
    url: str | None = None
    title: str | None = None
    domain: str = "general"
    source: str | None = None
    style_tags: list[str] = field(default_factory=list)
    current_step: str | None = None  # 派生字段(不存 DB),API 返回时动态填充
    progress_pct: int = 0
    meta: dict = field(default_factory=dict)
    published_at: datetime | None = None  # 源内容在平台的发布/更新时间(01_download 写入 metadata.json)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    error: str | None = None
    # 快照 / lineage
    # lineage_key=同源基础 id(去时间戳),同一内容的所有快照共用,快照来自重投/来源更新/pipeline 重建;
    # is_current=该 lineage 当前展示版(列表/KB 默认只显 current,历史版可跳转对比);
    # source_digest=抓回源内容 hash、pipeline_digest=各步定义指纹聚合(供"重建过期"判断);
    # parent_job_id=fork 自哪个快照。
    lineage_key: str | None = None
    is_current: bool = True
    source_digest: str | None = None
    pipeline_digest: str | None = None
    parent_job_id: str | None = None


@dataclass
class Step:
    job_id: str
    name: str
    status: StepStatus = StepStatus.WAITING
    pool: str = ""
    input_hash: str | None = None
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_sec: float | None = None
    meta: dict = field(default_factory=dict)
    error: str | None = None
    retries: int = 0


@dataclass
class Worker:
    id: str
    type: str
    pools: list[str] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    reject_tags: set[str] = field(default_factory=set)
    status: str = "offline"
    # 管理员暂停叠加位,取值 "" / "paused",与运行时 status(idle/busy) 解耦:
    # 暂停态只由 API 写,worker 认领/心跳永不覆盖,故 busy worker 暂停后跑完当前步不会丢暂停。
    admin_status: str = ""
    hostname: str | None = None
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
    concurrency: int = 1   # per-worker 并发(认领并行度);worker 启动 WORKER_CONCURRENCY 自报。
    remote_addr: str | None = None   # 连接来源:网关 worker 注册时的客户端 IP;直连(本机)= None。
    current_job: str | None = None
    current_step: str | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_duration_sec: float = 0.0
    first_seen: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    last_heartbeat: datetime | None = None
    admin_note: str | None = None


@dataclass
class Collection:
    """集合 = job 的归属分组。订阅是集合的属性(非独立实体):
    source_type/source_id 非空 = 订阅集合(自动从某来源追更);为空 = 手动集合。"""
    id: str
    name: str
    domain: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    job_count: int = 0
    # 订阅属性(手动集合为 None/默认)
    # 支持: bilibili_up/bilibili_fav/bilibili_collection/youtube_channel/rss/local_dir(见 shared/sources.py)
    source_type: str | None = None
    source_id: str | None = None        # 来源标识(B站 mid / 收藏夹 id / 频道URL / feed URL / 目录路径)
    sync_enabled: bool = True           # 自动追更开关(仅订阅集合有意义)
    last_synced_at: datetime | None = None
    last_sync_status: str | None = None  # 上次同步结果: ok | error | syncing | None(从未同步)
    last_sync_error: str | None = None   # 上次同步失败的错误摘要(status=error 时有值)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def is_subscription(self) -> bool:
        return bool(self.source_type and self.source_id)


@dataclass
class AIUsage:
    exec_id: str
    provider: str
    model: str
    job_id: str | None = None
    step: str | None = None
    worker_id: str | None = None      # 哪个 worker 执行(AI 用量归因到节点)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0   # 写缓存 token
    cache_read_input_tokens: int = 0       # 读缓存 token;命中率=read/(input+read+creation)
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    num_turns: int = 0                # claude -p agentic 轮数
    cached: bool = False
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class LLMRequest:
    messages: list[dict]
    model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.7
    images: list[Path] = field(default_factory=list)
    system: str | None = None
    # 注:目前仅 OpenAICompatibleProvider 把它转成 {"type":"json_object"};AnthropicProvider 与
    # claude-cli 不读此项(由 step_base._extract_json/_salvage_scores 兜底解析 JSON)。
    response_format: str | None = None
    # 取证等需联网/工具的步骤:放开指定工具(如 ["WebSearch","Bash"])。仅 claude-cli provider 读,
    # 转为 --allowedTools <tools> --max-turns;其它 provider 忽略。None=用默认两档(images→Read / 否则禁工具)。
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    # 放行目录(claude-cli --add-dir):allowed_tools 含 Read 且要读 prompt 里引用的本地文件
    # (如 pdf-only 直喂 input/source.pdf)时必须给,否则 Read 出沙箱失败。仅 claude-cli 读。
    add_dirs: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        """JSON 安全序列化,images 的 Path 转 str;供 AI task 内联投递,见 AITask。"""
        return {
            "messages": self.messages,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "images": [str(p) for p in self.images],
            "system": self.system,
            "response_format": self.response_format,
            "allowed_tools": self.allowed_tools,
            "add_dirs": list(self.add_dirs or []),
            "max_turns": self.max_turns,
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "LLMRequest":
        return cls(
            messages=list(d.get("messages", [])),
            model=d.get("model"),
            max_tokens=int(d.get("max_tokens", 4096)),
            temperature=float(d.get("temperature", 0.7)),
            images=[Path(s) for s in d.get("images", [])],
            system=d.get("system"),
            response_format=d.get("response_format"),
            allowed_tools=d.get("allowed_tools"),
            max_turns=d.get("max_turns"),
            add_dirs=list(d.get("add_dirs") or []),
        )


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    num_turns: int = 0
    cached: bool = False
    # AI 审计字段(prompt 白盒化 / ai_logs)
    session_id: str | None = None        # provider 会话 id(claude-cli 有,可溯/可续)
    api_ms: float | None = None          # 服务端 API 耗时(claude-cli duration_api_ms / SDK 测得)
    ttft_ms: float | None = None         # 首 token 延迟(provider 提供时)
    finish_reason: str | None = None     # stop_reason / finish_reason
    tier_used: str | None = None         # 实际命中的 tier(primary/fallback/text_fallback),由 gateway 写
    attempts: list[dict] = field(default_factory=list)   # 逐 tier 尝试链,由 gateway 写
    raw: dict | None = None              # provider 原始返回(尽量保真),供审计 raw
    # claude CLI 会话 transcript 本地路径(agentic 中间轮全轨迹;provider 按 session_id 定位,
    # 非 CLI provider / 定位失败为 None)。消费方:step_base 拷 sidecar、worker AI task 内嵌审计。
    transcript_path: str | None = None

    def to_jsonable(self) -> dict:
        """JSON 安全序列化(全字段本就 json-safe);供 AI task 结果回写 airesult:{task_id}。"""
        return asdict(self)

    @classmethod
    def from_jsonable(cls, d: dict) -> "LLMResponse":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AITask:
    """独立 AI 任务:不挂 job、不走 storage —— 内联 LLMRequest 载荷,结果内联回 airesult:{task_id}。
    供 /api/ask、/digest 把单次 claude 调用交给 ai-worker(claude-cli tag)异步执行。
    入队 queue:ai(kind='ai',require_tags=['claude-cli']);无 job_id,故与 pipeline-step task 区分。"""
    task_id: str
    request: LLMRequest
    step_name: str = "ai"          # gateway 路由步名(如 synthesis/digest),也作 ai_usage.step
    domain: str | None = None      # 观测/归因(可空)
    tags: list[str] = field(default_factory=list)                            # 软标签(reject 过滤用)
    require_tags: list[str] = field(default_factory=lambda: ["claude-cli"])  # 硬门控:仅有凭证 ai-worker 认领

    def to_task_payload(self) -> dict:
        """序列化为 queue:ai 的 task JSON dict(kind='ai',无 job_id)。"""
        return {
            "kind": TaskKind.AI.value,
            "task_id": self.task_id,
            "step": self.step_name,
            "domain": self.domain,
            "request": self.request.to_jsonable(),
            "tags": sorted(self.tags),
            "require_tags": sorted(self.require_tags),
            "pool": "ai",
        }

    @classmethod
    def from_task_payload(cls, d: dict) -> "AITask":
        return cls(
            task_id=d["task_id"],
            request=LLMRequest.from_jsonable(d.get("request", {})),
            step_name=d.get("step", "ai"),
            domain=d.get("domain"),
            tags=list(d.get("tags", [])),
            require_tags=list(d.get("require_tags", ["claude-cli"])),
        )


# id 生成单一来源在 shared.ids;以下保留同名再导出/薄包装,既有 `from shared.models import ...` 调用点不破。
from shared.ids import (  # noqa: E402
    content_job_id as _content_job_id,
    generate_worker_id,  # noqa: F401
    generate_collection_id,  # noqa: F401
    subscription_collection_id as collection_id_for_subscription,  # noqa: F401
)


def derive_job_id(url: str | None, content_type: str | None = None, source: str | None = None) -> str:
    """Job ID(统一规则,见 shared.ids):jobs_{前缀}_{原生id}_{时间戳}。同一内容重投/重建=同 lineage 新快照。"""
    return _content_job_id(url, content_type, source)
