"""数据模型 + 枚举 + ID 生成。"""

from __future__ import annotations

import enum
import re
import secrets
from dataclasses import dataclass, field
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
    current_step: str | None = None  # 派生字段（不存 DB），API 返回时动态填充
    progress_pct: int = 0
    meta: dict = field(default_factory=dict)
    published_at: datetime | None = None  # 源内容在平台的发布/更新时间(01_download 写入 metadata.json)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    error: str | None = None


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
    # 管理员暂停叠加位（"" / "paused"），与运行时 status(idle/busy) 解耦：
    # 暂停态只由 API 写，worker 认领/心跳永不覆盖，故 busy worker 暂停后跑完当前步不会丢暂停。
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
    """集合 = job 的归属分组。订阅是集合的属性(非独立实体)：
    source_type/source_id 非空 = 订阅集合(自动从某来源追更)；为空 = 手动集合。"""
    id: str
    name: str
    domain: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    job_count: int = 0
    # 订阅属性（手动集合为 None/默认）
    # 支持: bilibili_up/bilibili_fav/bilibili_collection/youtube_channel/rss/local_dir(见 shared/sources.py)
    source_type: str | None = None
    source_id: str | None = None        # 来源标识(B站 mid / 收藏夹 id / 频道URL / feed URL / 目录路径)
    sync_enabled: bool = True           # 自动追更开关（仅订阅集合有意义）
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
    # 转为 --allowedTools <tools> --max-turns;其它 provider 忽略。None=沿用原两档(images→Read / 否则禁工具)。
    allowed_tools: list[str] | None = None
    max_turns: int | None = None


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


def derive_job_id(url: str | None, content_type: str | None = None, source: str | None = None) -> str:
    """Job ID(统一规则,见 shared.sources):jobs_{前缀}_{原生id}。撞已存在由调用方加随机后缀消歧。"""
    from shared.sources import content_job_id
    return content_job_id(url, content_type, source)


def generate_worker_id(worker_type: str) -> str:
    """生成 Worker ID: {type}-{8 hex chars}"""
    r = secrets.token_hex(4)
    return f"{worker_type}-{r}"


def collection_id_for_subscription(source_type: str, source_id: str) -> str:
    """Collection ID(统一规则,见 shared.sources):col_{标签}_{slug}。"""
    from shared.sources import subscription_collection_id
    return subscription_collection_id(source_type, source_id)


def generate_collection_id() -> str:
    """手动集合 ID: col_{8 hex}(无日期,简洁;创建时间已单独存 created_at)。"""
    return f"col_{secrets.token_hex(4)}"
