"""Pydantic request/response models。"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.db import PROMPT_VERSION_EXCLUSIVE_MAX, PROMPT_VERSION_MIN
from shared.source_registry import CONTENT_TYPE_NAMES, SUBSCRIPTION_SOURCE_NAMES


ContentType = Enum(
    "ContentType", {name: name for name in CONTENT_TYPE_NAMES}, type=str, module=__name__,
)
SubscriptionSourceType = Enum(
    "SubscriptionSourceType",
    {name: name for name in SUBSCRIPTION_SOURCE_NAMES},
    type=str,
    module=__name__,
)


PromptVersion = Annotated[
    int,
    # 2^63 可被 JSON number 精确表示.用排他上界避免 OpenAPI 把 2^63-1
    # 转成浮点数后舍入为 2^63,语义仍等价于最大值 2^63-1.
    Field(strict=True, ge=PROMPT_VERSION_MIN, lt=PROMPT_VERSION_EXCLUSIVE_MAX),
]


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    url: str | None = None
    content_type: ContentType | None = None
    domain: str = "general"
    style_tags: list[str] = Field(default_factory=list)
    collection_id: str | None = None
    # 投递开关:是否生成 AI 智能笔记(及随附评审)。None=按内容类型默认
    # (article 默认 False 走轻链路;video/paper/audio 默认 True)。概念提取+摘要始终跑。
    smart_note: bool | None = None


class JobResponse(BaseModel):
    job_id: str
    content_type: str
    status: str
    created_at: str
    updated_at: str | None = None
    published_at: str | None = None   # 源内容在 B 站等平台的发布时间(「上传于」)
    title: str | None = None
    url: str | None = None
    progress_pct: int = 0
    source: str | None = None
    domain: str = "general"
    collection_id: str | None = None
    versions: int = 1   # 同 lineage(同源内容)快照总数;>1 表示有历史版本可跳转


class JobDetailResponse(JobResponse):
    collection_name: str | None = None   # 由 collection_id join 出的集合名(无则 null)
    media: dict = Field(default_factory=dict)  # 源媒体元信息(resolution/duration_sec/file_size_mb/has_subtitle/word_count),来自 metadata.json / parsed.json
    artifacts: list[str] = Field(default_factory=list)  # 可见产物文件路径(元信息标签页"产物路径")
    meta: dict = Field(default_factory=dict)
    steps: list[StepResponse] = Field(default_factory=list)
    # 本任务各 AI 步派发时用的 prompt 覆盖版本号快照,从 job.json.prompt_overrides[step].version 读,
    # 无覆盖的步不出现。前端与当前激活版本(GET /api/prompts)比,不一致提示「重跑该步」,见 docs/03-contracts.md §1.14。
    prompt_versions: dict = Field(default_factory=dict)
    # 论文源类型(intermediate/parsed.json.source_kind,best-effort null):"arxiv-html"=有干净 HTML 源
    # (原文变体直接渲染 original.md);"pdf-only"=只有 PDF(原文=内嵌 PDF,AI 步直喂)。非论文恒 null。
    source_kind: str | None = None


class StepResponse(BaseModel):
    name: str
    label: str | None = None          # 步骤中文名(来自 pipelines.yaml);前端展示用
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_sec: float | None = None
    meta: dict = Field(default_factory=dict)
    error: str | None = None
    worker_id: str | None = None      # 执行本步的 worker(前端「由 xxx 完成」)


class CanonicalEvidenceLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["media", "pdf", "text", "image"]
    href: str
    label: str


class CanonicalMediaLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["media"]
    start_ms: int
    end_ms: int


class CanonicalPdfLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["pdf"]
    page: int
    bbox: tuple[float, float, float, float] | None = None


class CanonicalTextLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"]
    exact: str
    prefix: str
    suffix: str
    dom_path: str | None = None


class CanonicalImageLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["image"]
    bbox: tuple[float, float, float, float]
    start_ms: int | None = None
    end_ms: int | None = None
    page: int | None = None


CanonicalEvidenceLocator = Annotated[
    CanonicalMediaLocator
    | CanonicalPdfLocator
    | CanonicalTextLocator
    | CanonicalImageLocator,
    Field(discriminator="kind"),
]


class CanonicalEvidenceProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    status: Literal["valid", "stale", "missing"]
    reason: str | None = None
    job_id: str | None = None
    note_type: str | None = None
    chunk_id: str | None = None
    section: str | None = None
    evidence_fingerprint: str | None = None
    source_fingerprint: str | None = None
    locator: CanonicalEvidenceLocator | None = None
    link: CanonicalEvidenceLink | None = None
    validated_at: str | None = None


class CanonicalEvidenceResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class CanonicalEvidenceResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CanonicalEvidenceProjection]


class CanonicalEvidenceJobResponse(CanonicalEvidenceResolveResponse):
    total: int


JobDetailResponse.model_rebuild()


class JobListResponse(BaseModel):
    total: int
    items: list[JobResponse]


class RerunRequest(BaseModel):
    from_step: str


class RerunSmartRequest(BaseModel):
    provider: str


class WorkerResponse(BaseModel):
    id: str
    type: str
    pools: list[str]
    tags: list[str] = Field(default_factory=list)
    reject_tags: list[str] = Field(default_factory=list)
    hostname: str | None = None
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
    concurrency: int = 1
    remote_addr: str | None = None
    spec: dict = Field(default_factory=dict)   # 版本/机器配置(worker 自报);前端详情展示
    desired_config: dict | None = None          # 中心期望配置(None=未配置,尊重自报)
    cfg_rev: int = 0                            # 期望配置版本(单调)
    applied_cfg_rev: int = 0                    # worker 已生效版本(心跳回报;=cfg_rev 即已同步)
    load: dict = Field(default_factory=dict)   # live 负载(worker 心跳自报 cpu%/mem%/loadavg);redis-only
    traffic: dict = Field(default_factory=dict)  # 网关中转流量字节 {pull,push};redis-only(产物代理累计)
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


class WorkerConfigRequest(BaseModel):
    """中心下发 worker 运行配置(当前仅 concurrency;worker 心跳热应用,docs/03 §1.7.2)。"""
    model_config = {"extra": "forbid"}

    concurrency: int | None = Field(default=None, ge=1, le=64)


class WorkerUpdateRequest(BaseModel):
    status: str | None = None
    admin_note: str | None = None
    tags: list[str] | None = None
    reject_tags: list[str] | None = None


class DomainCreateRequest(BaseModel):
    """新建知识库(领域)。domain=键(slug,用于 URL/过滤);display_name/icon/color/role/description=展示元数据。"""
    domain: str
    display_name: str | None = None
    icon: str | None = None
    color: str | None = None
    role: str | None = None
    description: str | None = None


class DomainRenameRequest(BaseModel):
    """改领域英文标识(domain key):把 URL 里的旧 domain 迁到 new_domain,事务迁移所有引用。"""
    new_domain: str


class ProfileUpdateRequest(BaseModel):
    role: str | None = None
    domain_context: str | None = None
    output_style: dict | None = None
    terminology: list[str] | None = None
    do_not: list[str] | None = None
    # 知识库展示元数据,持久化在 profile
    display_name: str | None = None
    icon: str | None = None
    color: str | None = None
    description: str | None = None


class TermAddRequest(BaseModel):
    term: str


# Prompt 白盒:网页编辑每步 system prompt 覆盖


class PromptOverrideRequest(BaseModel):
    # scope 取 'global' 或 'domain':前者忽略 domain 字段,后者要求 domain 非空。
    # content=覆盖正文,空串会被当删除处理。
    scope: str = "global"
    domain: str | None = None
    content: str = ""
    # 版本管理类 Grafana save。mode='overwrite' 为默认,改当前激活版本内容;
    # mode='new' 另存为新版本(version=max+1 并设为激活)。note=该版本一行备注,可空。
    # 空 content 仍走删除:恢复默认,清全部版本。
    mode: str = "overwrite"
    note: str | None = None


class PromptActivateRequest(BaseModel):
    # 激活指针操作。version=数字:设该历史版本为当前激活,派发用它。
    # version=null:停用覆盖回内置默认,非破坏,保留全部历史版本。scope/domain 同 PromptOverrideRequest。
    scope: str = "global"
    domain: str | None = None
    version: PromptVersion | None = None


# 集合


class CollectionCreateRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    # 手动集合 name 必填;订阅集合可留空(""),首次同步后自动命名为来源真实名。
    name: str = ""
    domain: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    # 订阅集合:给定 source_type/source_id 即创建订阅集合(自动从该来源追更)。
    # source_type 取值由 configs/sources.yaml 派生,OpenAPI 会给出完整 enum。
    source_type: SubscriptionSourceType | None = None
    source_id: str | None = None        # 来源 id / URL / 容器内目录,具体语义由 registry 元数据给出
    sync_now: bool = True               # 建后立即首次同步


class CollectionUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    sync_enabled: bool | None = None    # 订阅集合:自动追更开关


class CollectionSubscriptionInfo(BaseModel):
    """集合的订阅源信息(订阅是集合属性)。同步/开关端点用集合自身 id。"""
    source_type: SubscriptionSourceType
    source_id: str            # 来源自身 id / URL / 容器内目录
    source_label: str = ""    # 由 registry group 派生的短标签;前端 = name + 来源徽标
    enabled: bool             # 自动同步开关 = collection.sync_enabled
    last_synced_at: str | None = None
    last_sync_status: str | None = None   # ok | error | syncing | None(从未同步)
    last_sync_error: str | None = None    # status=error 时的错误摘要(供前端 tooltip)


class CollectionResponse(BaseModel):
    id: str
    name: str
    domain: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    job_count: int = 0
    created_at: str
    subscription: CollectionSubscriptionInfo | None = None
    # 各状态 job 计数(仅集合详情端点填,列表端点为 None):{done, processing, failed, pending, ...}
    status_counts: dict[str, int] | None = None


# 术语表


class GlossaryOccurrenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    content_type: str = ""
    location: str | None = None
    title: str | None = None


class GlossaryRelationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    rel: str = "related"


class GlossaryTermRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    definition: str | None = None
    # 元素可为字符串(视为 rel='related')或 {term, rel};落库前经 norm_related 归一。
    related: list[str | GlossaryRelationResponse] | None = None
    expected_current_version_id: str | None = Field(default=None, min_length=1)
    expected_lock_revision: int | None = Field(default=None, strict=True, ge=0)


class GlossaryTermResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    term: str
    definition: str = ""
    zh_name: str = ""                                        # 标准中文译名(实体双语名)
    aliases: list[str] = Field(default_factory=list)         # 归并进本实体的变体名
    occurrences: list[GlossaryOccurrenceResponse] = Field(default_factory=list)
    related: list[GlossaryRelationResponse] = Field(default_factory=list)
    status: str = "accepted"
    watched: bool = False                                     # 概念订阅标记(单用户)
    is_topic: bool = False
    definition_locked: bool = False
    current_definition_version_id: str | None = None
    lock_revision: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "GlossaryTermResponse":
        """把 db._row_to_glossary 的 dict 转成响应模型:created_at/updated_at 从 datetime|None
        转 ISO str|None。所有返回单条术语的端点统一走它,保证字段形态一致。"""
        def _iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else (v or None)
        return cls(
            domain=row["domain"], term=row["term"],
            definition=row.get("definition") or "",
            zh_name=row.get("zh_name") or "",
            aliases=row.get("aliases") or [],
            occurrences=row.get("occurrences") or [],
            related=row.get("related") or [],
            status=row.get("status") or "accepted",
            watched=bool(row.get("watched")),
            is_topic=bool(row.get("is_topic")),
            definition_locked=bool(row.get("definition_locked")),
            current_definition_version_id=row.get("current_definition_version_id"),
            lock_revision=int(row.get("lock_revision") or 0),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class ConceptDefinitionVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_version_id: str
    domain: str
    term: str
    version: int = Field(ge=1)
    definition: str
    source_evidence_ids: list[str]
    source_set_fingerprint: str
    strategy: str
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    input_hash: str | None = None
    supersedes_version_id: str | None = None
    actor: str
    created_at: str


class ConceptEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    job_id: str
    content_type: str
    source_fingerprint: str | None = None
    note_type: str | None = None
    chunk_id: str | None = None
    section: str | None = None
    excerpt: str | None = None
    reason: str | None = None
    locator: CanonicalEvidenceLocator | None = None
    link: CanonicalEvidenceLink | None = None


class ConceptAttestationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    term: str
    level: Literal["none", "supported", "corroborated", "strong"]
    evidence_count: int = Field(ge=0)
    job_count: int = Field(ge=0)
    source_fingerprint_count: int = Field(ge=0)
    content_type_count: int = Field(ge=0)
    source_set_fingerprint: str
    included: list[ConceptEvidenceResponse]
    excluded: list[ConceptEvidenceResponse]


class ConceptTermDetailResponse(GlossaryTermResponse):
    occurrences: list[GlossaryOccurrenceResponse] = Field(
        default_factory=list, max_length=100,
    )
    occurrence_total: int = Field(ge=0)
    occurrence_limit: Literal[100] = 100
    current_definition: ConceptDefinitionVersionResponse
    definition_history: list[ConceptDefinitionVersionResponse] = Field(max_length=100)
    definition_history_total: int = Field(ge=1)
    definition_history_limit: Literal[100] = 100
    attestation: ConceptAttestationResponse


class ConceptCasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_version_id: str = Field(min_length=1)
    expected_lock_revision: int = Field(strict=True, ge=0)


class ConceptLockResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_definition_version_id: str
    lock_revision: int = Field(ge=0)
    locked: bool
    changed: bool


class ConceptResynthesizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: bool
    reason: Literal[
        "locked",
        "no_quorum",
        "source_set_unchanged",
        "input_too_large",
    ] | None = None
    current: ConceptDefinitionVersionResponse | None = None
    version: ConceptDefinitionVersionResponse | None = None
    attestation: ConceptAttestationResponse | None = None


# 搜索


class SearchResultItem(BaseModel):
    job_id: str
    title: str | None = None
    note_type: str
    snippet: str
    content_type: str = ""
    domain: str = ""
    collection_id: str | None = None
    canonical_evidence: list[CanonicalEvidenceProjection] = Field(default_factory=list)


class SearchResponse(BaseModel):
    total: int
    items: list[SearchResultItem]


# Worker-gateway 认领/上报


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
    worker_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    num_turns: int = 0
    cached: bool = False
