"""Selected HTTP JSON wire 的精确响应模型。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from api.schemas import GlossaryOccurrenceResponse, GlossaryTermResponse, WorkerResponse


DateTimeString = Annotated[str, Field(json_schema_extra={"format": "date-time"})]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorResponse(WireModel):
    error: str
    message: str


API_ERROR_RESPONSES = {
    code: {"model": ErrorResponse}
    for code in (400, 401, 403, 404, 409, 413, 416, 422, 429, 502, 503)
}


class SourceContentType(WireModel):
    type: str
    label: str
    pipeline: str
    upload_extensions: list[str]


class SourceJobSource(WireModel):
    type: str
    label: str
    content_types: list[str]
    document_kinds: list[str]
    default_document_kind: str | None
    default_source_profile: str | None
    creatable: bool


class SourceDocumentKind(WireModel):
    kind: str
    label: str
    description: str
    note_profile: str
    review_profile: str


class SourceProfile(WireModel):
    profile: str
    label: str
    capabilities: list[str]


class SourceSubscription(WireModel):
    type: str
    label: str
    group: str
    icon: str
    id_label: str
    placeholder: str
    hint: str
    home_url_template: str | None = None


class SourceCatalogResponse(WireModel):
    content_types: list[SourceContentType]
    job_sources: list[SourceJobSource]
    subscription_sources: list[SourceSubscription]
    document_kinds: list[SourceDocumentKind]
    source_profiles: list[SourceProfile]


class JobCreatedResponse(WireModel):
    job_id: str
    content_type: str
    document_kind: str | None
    pipeline: str
    status: str
    created_at: DateTimeString


class JobFacetsResponse(WireModel):
    source: dict[str, int]
    domain: dict[str, int]
    status: dict[str, int]


class JobStatusResponse(WireModel):
    job_id: str
    status: str


class JobRetryResponse(JobStatusResponse):
    retried_from: str | None = None


class JobRerunResponse(JobStatusResponse):
    from_step: str


class JobRerunSmartResponse(JobRerunResponse):
    provider: str
    review_step: str


class JobRebuildResponse(JobStatusResponse):
    parent_job_id: str | None
    lineage_key: str | None
    from_step: str | None = None
    processing_mode: Literal["full", "mechanical_only"]


class JobRebuiltItem(WireModel):
    parent_job_id: str
    job_id: str
    from_step: str | None


class JobsRebuiltResponse(WireModel):
    rebuilt: int
    items: list[JobRebuiltItem]


class JobsRetriedResponse(WireModel):
    retried: int


class LineageVersion(WireModel):
    job_id: str
    created_at: DateTimeString
    is_current: bool
    status: str
    title: str | None
    pipeline_digest: str | None


class LineageVersionsResponse(WireModel):
    versions: list[LineageVersion]


class JobUsageRow(WireModel):
    step: str | None
    worker_id: str | None
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    duration_sec: float
    num_turns: int
    cache_hit_rate_pct: float


class JobUsageResponse(WireModel):
    usage: list[JobUsageRow]


class JobConceptResponse(GlossaryTermResponse):
    job_occurrences: list[GlossaryOccurrenceResponse]


class AiLogStep(WireModel):
    step: str
    calls: list[dict[str, Any]]


class AiLogsResponse(WireModel):
    job_id: str
    steps: list[AiLogStep]


class NoteVersion(WireModel):
    file: str
    provider: str
    model: str
    version: str
    review_file: str | None
    overall: int | float | None
    review_state: Literal["reliable", "unreliable", "legacy_unverified"] | None


class NoteVersionsResponse(WireModel):
    versions: list[NoteVersion]


class ArtifactFile(WireModel):
    path: str
    kind: str
    size: int


class ArtifactGroup(WireModel):
    step: str
    label: str
    total_bytes: int
    files: list[ArtifactFile]


class ArtifactsResponse(WireModel):
    groups: list[ArtifactGroup]
    total_bytes: int


class ReviewProjectionResponse(WireModel):
    schema_version: int | None
    reliability_state: Literal["reliable", "unreliable", "legacy_unverified"]
    review_reliable: bool
    reliability_reasons: list[str]
    score_keys: list[str]
    overall: int | float | None
    diagnostic_overall: None
    key_terms: list[dict[str, str]]
    missing_concepts: list[str]
    top3_improvements: list[str]
    issues: list[dict[str, Any]]
    review_input: dict[str, Any]
    completion: dict[str, Any]
    parse: dict[str, Any]
    citation_validation: dict[str, Any]
    review_coverage: dict[str, Any]
    note_file: str | None
    provider: str | None
    model: str | None
    generated_at: str | None
    completeness: int | float | None
    accuracy: int | float | None
    structure: int | float | None
    terminology: int | float | None
    visual_integration: int | float | None
    readability: int | float | None
    formula_integrity: int | float | None
    visual_references: int | float | None
    traceability: int | float | None
    conciseness: int | float | None


class EvidenceMatch(WireModel):
    anchor: str
    offset: int


class EvidenceProjectionItem(WireModel):
    id: str | None
    title: str | None
    publisher: str | None
    source_tier: str | None
    confidence: str | None
    eligible: bool
    eligibility_reasons: list[str]
    matches: list[EvidenceMatch]
    retrieved_at: str | None
    artifact: str | None
    original_url: str | None
    final_url: str | None
    url: None
    link_safe: bool
    verification_state: Literal["verified", "invalid"]
    verification_reasons: list[str]


class EvidenceProjectionResponse(WireModel):
    schema_version: int | None
    job_id: str | None
    manifest_state: Literal["legacy", "invalid", "partial", "verified"]
    reliability_state: Literal["verified", "legacy_unverified", "unreliable"]
    manifest_errors: list[str]
    evidence: list[EvidenceProjectionItem]


class GlossaryBatchResponse(WireModel):
    updated: int
    skipped: int


class WorkerTaskResponse(WireModel):
    job_id: str
    title: str | None
    content_type: str | None
    domain: str | None
    step: str
    status: str
    started_at: str | None
    finished_at: str | None
    duration_sec: float | None
    error: str | None


class WorkerRegistrationTokenResponse(WireModel):
    token: str
    expires_in_sec: int | None


class WorkerRegistrationStatusResponse(WireModel):
    exists: bool
    expires_in_sec: int | None


class WorkerUpdatedResponse(WireModel):
    id: str
    status: Literal["updated"]


class WorkerConfigResponse(WireModel):
    cfg_rev: int
    desired_config: dict[str, int]


class PoolLimitItem(WireModel):
    default: int
    override: int | None


class PoolLimitsResponse(RootModel[dict[str, PoolLimitItem]]):
    pass


class StatusUpdatedResponse(WireModel):
    status: Literal["updated"]


class SystemComponentResponse(WireModel):
    name: str
    kind: Literal["api", "scheduler", "redis", "minio"]
    status: Literal["up", "degraded", "down", "unknown"]
    version: str | None
    last_heartbeat: str | None
    uptime_sec: int | None
    detail: str | None
    extra: dict[str, Any]


class HealthCheckResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Literal["ok", "degraded", "error"]
    required: bool
    detail: str | None
    recovery: str | None


class ReadinessReasonResponse(WireModel):
    code: str
    severity: Literal["blocking", "degraded"]
    message: str
    recovery: str | None


class ReadinessResponse(WireModel):
    version: str
    status: Literal["ready", "degraded", "not_ready"]
    ready: bool
    degraded: bool
    checks: dict[str, HealthCheckResponse]
    reasons: list[ReadinessReasonResponse]


class HealthLiveResponse(WireModel):
    status: Literal["alive"]
    alive: Literal[True]
    version: str


class WorkerCountResponse(WireModel):
    online: int
    busy: int
    paused: int


class PoolStatResponse(WireModel):
    capacity: int
    used: int
    queue: int


class JobCountsResponse(WireModel):
    total: int
    done: int
    processing: int
    failed: int
    pending: int


class DiskInfoResponse(WireModel):
    used_gb: float
    available_gb: float
    total_gb: float
    used_pct: float


class ThroughputResponse(WireModel):
    done: int
    failed: int


class TrafficResponse(WireModel):
    pull_bytes: int
    push_bytes: int


class LinkTrafficResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    ts: float
    gateway: dict[str, Any]
    tunnel: dict[str, Any]


class FullStatusResponse(WireModel):
    version: str
    components: list[SystemComponentResponse]
    health: ReadinessResponse
    workers: dict[str, WorkerCountResponse]
    pools: dict[str, PoolStatResponse]
    jobs: JobCountsResponse
    disk: DiskInfoResponse
    throughput_1h: ThroughputResponse
    traffic: TrafficResponse
    link_traffic: LinkTrafficResponse | None


class LinkTrafficHistoryResponse(WireModel):
    samples: list[dict[str, Any]]


class SystemEventResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    ts: float
    kind: str


class SystemEventsResponse(WireModel):
    events: list[SystemEventResponse]


class UsageByModelResponse(WireModel):
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    cache_hit_rate_pct: float


class UsageAggregateResponse(WireModel):
    calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_creation_tokens: int
    total_cache_read_tokens: int
    total_cost_usd: float
    total_num_turns: int
    total_duration_sec: float
    cache_hit_rate_pct: float
    by_model: list[UsageByModelResponse]


class PricingStatusResponse(WireModel):
    ready: bool
    model_count: int
    fetched_at: str | None
    source_url: str


class PipelineStepResponse(WireModel):
    key: str
    label: str | None
    pool: str | None
    needs: list[str]
    is_ai: bool
    has_override: bool
    prompt_locked: bool


class PipelineResponse(WireModel):
    name: str
    key: str
    label: str
    content_types: list[str]
    document_kinds: list[str]
    source_profiles: list[str]
    steps: list[PipelineStepResponse]


class PipelinesResponse(WireModel):
    pipelines: list[PipelineResponse]


class PromptOverrideScope(WireModel):
    scope: str
    domain: str | None
    document_kind: str | None


class PromptListStep(WireModel):
    pipeline: str
    step: str
    label: str | None
    pool: str | None
    is_ai: bool
    locked: bool
    has_template: bool
    overrides: list[PromptOverrideScope]


class PromptListResponse(WireModel):
    steps: list[PromptListStep]


class PromptTemplateResponse(WireModel):
    name: str
    content: str
    bytes: int
    sha256: str
    source: str
    version: str | None


class PromptOverrideResponse(WireModel):
    scope: str
    domain: str
    pipeline: str
    document_kind: str
    step: str
    content: str
    version: str
    updated_at: DateTimeString


class PromptVersionMetaResponse(WireModel):
    version: str
    note: str | None
    created_at: DateTimeString


class PromptDetailResponse(WireModel):
    pipeline: str
    step: str
    label: str | None
    pool: str | None
    is_ai: bool
    locked: bool
    default_template: str | None
    default_templates: list[PromptTemplateResponse]
    default_system: str | None
    override: PromptOverrideResponse | None
    active_version: str | None
    versions: list[PromptVersionMetaResponse]


class PromptVersionResponse(WireModel):
    version: str
    content: str
    note: str | None
    created_at: DateTimeString


class PromptMutationResponse(WireModel):
    status: Literal["saved", "deleted", "activated", "deactivated"]
    pipeline: str
    step: str
    scope: str | None = None
    domain: str | None = None
    document_kind: str | None = None
    active_version: str | None = None


class AiTaskCitationValidationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class AiTaskResultResponse(WireModel):
    status: Literal["pending", "error", "done"]
    task_id: str
    error: str | None = None
    content: str | None = None
    answer_markdown: str | None = None
    markdown: str | None = None
    provider: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    source_manifest: dict[str, Any] | None = None
    citation_validation: dict[str, Any] | None = None


class AiTaskLogCall(WireModel):
    task_id: str | None
    exec_id: str | None
    step: str | None
    domain: str | None
    provider: str | None
    model: str | None
    ok: bool
    error: str | None
    created_at: DateTimeString | None
    record: dict[str, Any]


class AiTaskLogResponse(WireModel):
    task_id: str
    count: int
    calls: list[AiTaskLogCall]


WorkerListResponse = list[WorkerResponse]
WorkerTasksResponse = list[WorkerTaskResponse]
GlossaryListResponse = list[GlossaryTermResponse]
