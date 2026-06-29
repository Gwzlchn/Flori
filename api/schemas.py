"""Pydantic request/response models。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobCreateRequest(BaseModel):
    url: str | None = None
    content_type: str | None = None
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
    versions: int = 1   # 同 lineage(同源内容)快照总数;>1 表示有历史版本可跳转(P2b)


class JobDetailResponse(JobResponse):
    collection_name: str | None = None   # 由 collection_id join 出的集合名(无则 null)
    media: dict = Field(default_factory=dict)  # 源媒体元信息(resolution/duration_sec/file_size_mb/has_subtitle/word_count),来自 metadata.json / parsed.json
    artifacts: list[str] = Field(default_factory=list)  # 可见产物文件路径(元信息标签页"产物路径")
    meta: dict = Field(default_factory=dict)
    steps: list[StepResponse] = Field(default_factory=list)


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
    # 知识库展示元数据(随 #1/#2:icon/color 持久化在 profile)
    display_name: str | None = None
    icon: str | None = None
    color: str | None = None
    description: str | None = None


class TermAddRequest(BaseModel):
    term: str


# ── Prompt 白盒(Phase 2:网页编辑每步 system prompt 覆盖)──


class PromptOverrideRequest(BaseModel):
    # scope='global'(忽略 domain)或 'domain'(需非空 domain);content=覆盖正文(空串会被当删除处理)
    scope: str = "global"
    domain: str | None = None
    content: str = ""
    # 版本管理(类 Grafana save):mode='overwrite'(默认)改当前激活版本内容;'new'=另存为新版本
    # (version=max+1 并设为激活)。note=该版本一行备注(可空)。空 content 仍走删除(恢复默认,清全部版本)。
    mode: str = "overwrite"
    note: str | None = None


# ── 集合 ──


class CollectionCreateRequest(BaseModel):
    # 手动集合 name 必填;订阅集合可留空(""),首次同步后自动命名为 <来源名>-<来源>。
    name: str = ""
    domain: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    # 订阅集合：给定 source_type/source_id 即创建订阅集合(自动从该来源追更)。
    # source_type 取值: bilibili_up / bilibili_fav / bilibili_collection /
    # youtube_channel / rss / local_dir(适配器见 shared/subscriptions/)。
    source_type: str | None = None
    source_id: str | None = None        # 来源 id：B站 mid / YouTube 频道 / RSS url / 目录路径
    sync_now: bool = True               # 建后立即首次同步


class CollectionUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    sync_enabled: bool | None = None    # 订阅集合：自动追更开关


class CollectionSubscriptionInfo(BaseModel):
    """集合的订阅源信息(订阅是集合属性)。同步/开关端点用集合自身 id。"""
    source_type: str          # bilibili_up/fav/collection · youtube_channel · rss · local_dir
    source_id: str            # B站 mid / 频道URL / feed URL / 目录路径 / 收藏夹id ...
    source_label: str = ""    # 由 source_type 派生的来源短标签(bilibili/youtube/rss/local);前端 = name + 该徽标
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


# ── 术语表 ──


class GlossaryTermRequest(BaseModel):
    term: str
    definition: str | None = None
    related: list[str] | None = None


class GlossaryTermResponse(BaseModel):
    domain: str
    term: str
    definition: str = ""
    occurrences: list[dict] = Field(default_factory=list)   # [{job_id, content_type, location}]
    related: list[str] = Field(default_factory=list)
    status: str = "accepted"
    is_topic: bool = False
    definition_locked: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "GlossaryTermResponse":
        """db._row_to_glossary 的 dict(created_at/updated_at 为 datetime|None)→ 响应模型
        (ISO str|None)。所有返回单条术语的端点统一走它,保证字段形态一致——
        此前 /api/glossary/{d}/{t} 与 /api/domains/{d}/terms/{t} 字段不一致(updated_at 有无、
        created_at 缺失 '' vs null)。"""
        def _iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else (v or None)
        return cls(
            domain=row["domain"], term=row["term"],
            definition=row.get("definition") or "",
            occurrences=row.get("occurrences") or [],
            related=row.get("related") or [],
            status=row.get("status") or "accepted",
            is_topic=bool(row.get("is_topic")),
            definition_locked=bool(row.get("definition_locked")),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


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
    worker_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    num_turns: int = 0
    cached: bool = False
