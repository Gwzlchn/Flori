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
