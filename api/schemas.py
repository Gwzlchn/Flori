"""Pydantic request/response models。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobCreateRequest(BaseModel):
    url: str | None = None
    content_type: str | None = None
    domain: str = "general"
    style_tags: list[str] = Field(default_factory=list)
    collection_id: str | None = None


class JobResponse(BaseModel):
    job_id: str
    content_type: str
    status: str
    created_at: str
    title: str | None = None
    progress_pct: int = 0
    source: str | None = None
    domain: str = "general"


class JobDetailResponse(JobResponse):
    meta: dict = Field(default_factory=dict)
    steps: list[StepResponse] = Field(default_factory=list)


class StepResponse(BaseModel):
    name: str
    status: str
    duration_sec: float | None = None
    meta: dict = Field(default_factory=dict)
    error: str | None = None


JobDetailResponse.model_rebuild()


class JobListResponse(BaseModel):
    total: int
    items: list[JobResponse]


class RerunRequest(BaseModel):
    from_step: str


class WorkerResponse(BaseModel):
    id: str
    type: str
    pools: list[str]
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)
    hostname: str | None = None
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
    status: str
    current_job: str | None = None
    current_step: str | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_duration_sec: float = 0.0
    first_seen: str
    started_at: str | None = None
    last_heartbeat: str | None = None
    admin_note: str | None = None


class WorkerUpdateRequest(BaseModel):
    status: str | None = None
    admin_note: str | None = None
    tags: list[str] | None = None
    reject_tags: list[str] | None = None


class ProfileUpdateRequest(BaseModel):
    role: str | None = None
    domain_context: str | None = None
    output_style: dict | None = None
    terminology: list[str] | None = None
    do_not: list[str] | None = None


class TermAddRequest(BaseModel):
    term: str


class HealthResponse(BaseModel):
    status: str
    checks: dict


# ── 集合 ──


class CollectionCreateRequest(BaseModel):
    name: str
    domain: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class CollectionUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class CollectionResponse(BaseModel):
    id: str
    name: str
    domain: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    job_count: int = 0
    created_at: str


# ── 术语表 ──


class GlossaryTermRequest(BaseModel):
    term: str
    definition: str | None = None
    related: list[str] | None = None


class GlossaryTermResponse(BaseModel):
    domain: str
    term: str
    definition: str = ""
    sources: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    status: str = "accepted"
    source_type: str = "manual"
    created_at: str


# ── 搜索 ──


class SearchResultItem(BaseModel):
    job_id: str
    title: str | None = None
    note_type: str
    snippet: str
    content_type: str = ""
    domain: str = ""
    collection_id: str | None = None


class SearchResponse(BaseModel):
    total: int
    items: list[SearchResultItem]


# ── Worker-gateway 认领/上报 ──


class RunnerClaimRequest(BaseModel):
    pools: list[str] = Field(default_factory=list)
    pool_limits: dict[str, int] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)


class RunnerCompleteRequest(BaseModel):
    pool: str
    exec_id: str
    duration: float
    started_at: float


class RunnerFailRequest(BaseModel):
    pool: str
    exec_id: str
    error: str
    error_type: str
    duration: float
    started_at: float
    count_stats: bool = False


class RunnerReleaseRequest(BaseModel):
    pool: str
    exec_id: str


class RunnerProgressRequest(BaseModel):
    payload: dict = Field(default_factory=dict)


class RunnerUsageRequest(BaseModel):
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
