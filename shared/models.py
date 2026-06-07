"""数据模型 + 枚举 + ID 生成。"""

from __future__ import annotations

import enum
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    id: str
    name: str
    domain: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    job_count: int = 0
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


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


def generate_job_id() -> str:
    """生成 Job ID: j_{YYYYMMDD}_{6 hex chars}"""
    d = date.today().strftime("%Y%m%d")
    r = secrets.token_hex(3)
    return f"j_{d}_{r}"


def generate_worker_id(worker_type: str) -> str:
    """生成 Worker ID: {type}-{8 hex chars}"""
    r = secrets.token_hex(4)
    return f"{worker_type}-{r}"
