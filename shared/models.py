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
    hostname: str | None = None
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
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
    source_type: str | None = None      # 目前: bilibili_up
    source_id: str | None = None        # B站 mid
    sync_enabled: bool = True           # 自动追更开关（仅订阅集合有意义）
    last_synced_at: datetime | None = None
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
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_sec: float = 0.0
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
    response_format: str | None = None


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    cached: bool = False


_CATEGORY = {"video": "bili", "article": "article", "paper": "paper", "podcast": "podcast"}


def derive_job_id(url: str | None, content_type: str | None = None, source: str | None = None) -> str:
    """有意义的 Job ID: jobs_{类别}_{inner}。bilibili 用 BV 号(稳定/唯一/路径安全);
    其余用 url 短哈希;无 url(上传)用随机。撞已存在由调用方加随机后缀消歧。"""
    import hashlib
    import re

    m = re.search(r"(BV[0-9A-Za-z]{8,12})", url or "")
    if m:
        return f"jobs_bili_{m.group(1)}"
    cat = _CATEGORY.get(content_type or "", content_type or "x")
    inner = hashlib.sha1(url.encode()).hexdigest()[:8] if url else secrets.token_hex(4)
    return f"jobs_{cat}_{inner}"


def generate_worker_id(worker_type: str) -> str:
    """生成 Worker ID: {type}-{8 hex chars}"""
    r = secrets.token_hex(4)
    return f"{worker_type}-{r}"


def collection_id_for_subscription(source_type: str, source_id: str) -> str:
    """订阅集合用有含义、稳定的 ID(去重友好,一眼看出来源)。
    B站 UP: col_bili_up_{mid};其余: col_{source_type}_{source_id}(非字母数字归一为 _)。"""
    if source_type == "bilibili_up":
        return f"col_bili_up_{source_id}"
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{source_type}_{source_id}").strip("_")
    return f"col_{safe}"


def generate_collection_id() -> str:
    """手动集合 ID: col_{8 hex}(无日期,简洁;创建时间已单独存 created_at)。"""
    return f"col_{secrets.token_hex(4)}"
