// 面包屑段:TopBar 渲染 + store.crumbOverride 共用同一形状。
export interface BreadcrumbSeg {
  t: string        // 文案
  to?: string      // 可点跳转目标(末段通常无 to)
}

// 内容类型是后端 registry 的开放集合;展示标签由 /api/sources 动态下发。
export type ContentType = string

export interface JobSummary {
  job_id: string
  content_type: ContentType
  status: string
  created_at: string
  title: string | null
  progress_pct: number
  source: string | null
  domain: string
  collection_id: string | null
  versions?: number   // 同源(lineage)快照总数;>1 表示有历史版本
}

export interface StepInfo {
  name: string
  label: string | null          // 步骤中文名(来自 pipelines.yaml)
  status: string
  started_at: string | null
  finished_at: string | null
  duration_sec: number | null
  meta: Record<string, any>
  error: string | null
  worker_id?: string | null     // 执行本步的 worker(「由 xxx 完成」)
}

// 逐次 AI 调用明细(GET /api/jobs/{id}/usage;job 详情按步展示)。
export interface StepUsage {
  step: string | null
  worker_id: string | null
  provider: string
  model: string
  input_tokens: number
  output_tokens: number
  cache_creation_tokens: number
  cache_read_tokens: number
  cost_usd: number
  duration_sec: number
  num_turns: number
  cache_hit_rate_pct: number
}

// 完整 AI 审计日志(prompt 白盒化):每次 LLM 调用一条。字段尽量全,UI 可只用子集。
export interface AiLogAttempt {
  tier?: string; provider?: string; model?: string; ok?: boolean
  error_class?: string; error?: string
}
export interface AiLogCall {
  job_id?: string
  step?: string
  content_type?: string | null
  domain?: string | null
  call_index?: number
  exec_id?: string
  session_id?: string | null
  ts_start?: string
  ts_end?: string
  routing?: { requested_ai?: any; tier_used?: string | null; provider?: string | null; model?: string | null; attempts?: AiLogAttempt[] }
  latency?: { ttft_ms?: number | null; api_ms?: number | null; duration_total_sec?: number | null }
  call_meta?: { max_tokens?: number; temperature?: number; response_format?: string | null; allowed_tools?: string[] | null; max_turns?: number | null; images_count?: number }
  prompt?: { rendered?: { system?: string | null; user?: string }; template?: { source?: string }; values?: any; images?: { path: string; hash?: string; bytes?: number }[] }
  output?: { content?: string | null; num_turns?: number | null; finish_reason?: string | null }
  output_processed?: { json_parse?: { ok?: boolean; salvaged?: boolean }; parse_failed?: boolean; extracted?: any } | null
  usage?: { input_tokens?: number; output_tokens?: number; cache_creation_input_tokens?: number; cache_read_input_tokens?: number }
  cost?: { cost_usd?: number; basis?: string }
  raw?: any
  injected?: { domain_profile?: { name?: string | null; hash?: string | null }; style_tags?: string[]; terminology_snapshot?: string[] | null }
  input_hashes?: Record<string, string>
  flori?: { image_tag?: string | null; version?: string | null; git_commit?: string | null }
  env?: { worker_id?: string | null; host?: string | null; pool?: string | null }
  links?: { source?: { job_url?: string | null; collection?: string | null; published_at?: string | null } }
  ok?: boolean
  error?: string | null
}
export interface AiLogStep { step: string; calls: AiLogCall[] }
export interface AiLogsResponse { job_id: string; steps: AiLogStep[] }

export interface JobMedia {
  resolution?: string           // 视频:如 "1920x1080"
  width?: number
  height?: number
  duration_sec?: number         // 源视频/音频时长
  file_size_bytes?: number      // 原始文件精确字节(前端转 KB/MB/GB)
  file_size_mb?: number
  has_subtitle?: boolean
  has_danmaku?: boolean
  word_count?: number           // 文章:字数
  pages?: number                // 论文:页数
  lang?: string                 // 文章/论文:正文主语言(zh / non-zh)
  sitename?: string             // 文章来源:网站名(SemiAnalysis / 华尔街见闻 / 域名)
  venue?: string                // 论文来源:会议/期刊 + 年份(OSDI 2023 / arXiv)
  authors?: string[]            // 文章/论文:作者
  abstract?: string             // 文章/论文:摘要
  tags?: string[]               // 文章:标签
  image?: string                // 文章:封面图;站点占位图后端已剔除
  video_codec?: string          // 视频编码,如 "h264" / "av1"
  audio_codec?: string          // 音频编码,如 "aac" / "opus"
  fps?: number                  // 帧率
  bitrate_kbps?: number         // 总码率(kbps)
  video_bitrate_kbps?: number   // 视频流码率(kbps)
}

export interface JobDetail extends JobSummary {
  url: string | null
  updated_at: string | null
  published_at: string | null   // 源内容发布时间(「上传于」)
  collection_name: string | null // 由 collection_id join 出,无归属/集合已删则 null
  media: JobMedia               // 源媒体元信息(视频→分辨率/时长/大小、文章→字数),来自 metadata.json/parsed.json
  artifacts: string[]           // 可见产物文件路径
  meta: Record<string, any>
  steps: StepInfo[]
  prompt_versions?: Record<string, string>  // 十进制字符串,避免 int64 在 JavaScript 中丢精度
  source_kind?: 'arxiv-html' | 'pdf-only' | null  // 论文源类型:arxiv-html=原文渲染 original.md;pdf-only=内嵌 PDF
}

export interface JobListResponse {
  total: number
  items: JobSummary[]
}

export type WorkerStatus =
  | 'online-idle'
  | 'online-busy'
  | 'offline'
  | 'paused'
  | 'stale'

export interface WorkerSpec {
  version?: string              // 代码版本(构建时注入的 git sha;'dev'=未注入)
  cpu?: number                  // 逻辑核数
  mem_mb?: number               // 内存(MB)
  platform?: string             // OS/架构
  python?: string               // Python 版本
}

// worker 心跳自报的 live 负载(纯 /proc 采;各项可缺=未采集)。
export interface WorkerLoad {
  cpu_pct?: number | null       // 瞬时 CPU 占用率(%)
  mem_pct?: number | null       // 已用内存(%)
  loadavg?: number | null       // 1 分钟平均负载
}

export interface Worker {
  // 中心配置(desired=期望;cfg_rev/applied_cfg_rev 比对显示"待同步/已生效")
  desired_config?: WorkerDesiredConfig | null
  cfg_rev?: number
  applied_cfg_rev?: number
  id: string
  type: string
  pools: string[]
  tags: string[]
  reject_tags: string[]
  hostname: string | null
  gpu_name: string | null
  gpu_memory_mb: number | null
  concurrency: number
  remote_addr: string | null
  spec?: WorkerSpec             // worker 自报:版本/机器配置
  load?: WorkerLoad             // worker 心跳自报:live 负载(cpu%/mem%/loadavg)
  traffic?: { pull?: number; push?: number }  // 网关中转流量字节(产物代理累计;redis-only)
  status: WorkerStatus
  current_job: string | null
  current_step: string | null
  tasks_completed: number
  tasks_failed: number
  total_duration_sec: number
  first_seen: string
  started_at: string | null
  last_heartbeat: string | null
  admin_note: string | null
}

export interface WorkerDesiredConfig {
  concurrency?: number
}

export interface WorkerRegistrationToken {
  token: string
  expires_in_sec: number | null
}

// Task = worker 认领执行的最小单元(某作业 job 的某步骤 step 的一次执行);每条对应一个 step 记录。
export interface WorkerTask {
  job_id: string
  title?: string | null         // enrich:作业标题(主显),空则前端退类型/流水线/job_id
  content_type?: string | null  // enrich:来源类型(图标/类型名)
  domain?: string | null        // enrich:所属领域
  step: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_sec: number | null
  error: string | null
}

// 任务队列(/system/queue)。
// 排队中(queued)/运行中(running)的 task,与 WorkerTask(已完成)同源:统一 TaskRow 渲染。
export interface QueueTask {
  state: 'queued' | 'running'
  job_id: string
  title?: string | null
  content_type?: string | null
  domain?: string | null
  pipeline?: string | null
  step: string
  pool?: string | null
  priority?: number | null
  enqueued_at?: number | null   // epoch 秒(排队时入队时刻);旧 task 可能缺
  started_at?: string | null    // ISO(运行中开始时刻)
  worker_id?: string
  worker_type?: string
  worker_hostname?: string | null
  tags?: string[]
  require_tags?: string[]
}

export interface QueuePool {
  name: string
  queued_count: number   // 队列总数(可能 > 列出条数)
  queued_shown: number   // 实际列出条数
  running: QueueTask[]
  queued: QueueTask[]
}

export interface QueueStatus {
  pools: QueuePool[]
  limit: number
}

// 系统健康总览页(/system)。
export type ComponentKind = 'api' | 'scheduler' | 'redis' | 'minio'
export type ComponentStatus = 'up' | 'degraded' | 'down' | 'unknown'

export interface SystemComponent {
  name: string
  kind: ComponentKind
  status: ComponentStatus
  version: string | null
  last_heartbeat: string | null    // ISO8601 UTC;勿前端自算时区
  uptime_sec: number | null
  detail: string | null
  extra: Record<string, any>       // 按 kind 有约定字段;前端渲染已知、忽略未知
}

export interface PoolStat    { capacity: number; used: number; queue: number }
export interface WorkerCount { online: number; busy: number; paused?: number }
export interface JobCounts   { total: number; done: number; processing: number; failed: number; pending: number }
export interface DiskInfo    { used_gb: number; available_gb: number; total_gb: number; used_pct: number }
export interface Throughput  { done: number; failed: number }
export type ReadinessStatus = 'ready' | 'degraded' | 'not_ready'
export type HealthCheckStatus = 'ok' | 'degraded' | 'error'
export interface HealthCheck {
  status: HealthCheckStatus
  required: boolean
  detail: string | null
  recovery: string | null
  [key: string]: unknown
}
export interface ReadinessReason {
  code: string
  severity: 'blocking' | 'degraded'
  message: string
  recovery: string | null
}
export interface ReadinessState {
  version: string
  status: ReadinessStatus
  ready: boolean
  degraded: boolean
  checks: Record<string, HealthCheck>
  reasons: ReadinessReason[]
}

// GET /api/status 完整形状(进页 1 次 + 每 15s 轮询拿全量)。
export interface FullStatus {
  version: string
  components: SystemComponent[]
  health: ReadinessState
  workers: Record<string, WorkerCount>
  pools: Record<string, PoolStat>
  jobs: JobCounts
  disk: DiskInfo
  throughput_1h?: Throughput
  traffic?: { pull_bytes: number; push_bytes: number }  // 网关中转流量累计(出库/入库字节)
  link_traffic?: LinkTraffic | null  // 通联:ECS↔NAS 隧道 + 网关 + 速率(tunnel_stats 上报;无边缘=null)
}

// 一条隧道(autossh)的累计字节;name=api/minio/redis/dozzle/mcp。
export interface TunnelStat { name: string; rx: number; tx: number; fwd: string }
// 链路流量快照(tunnel_stats 上报器周期写,/api/status 透出)。bps=上一采样周期的字节/秒速率。
export interface LinkTraffic {
  ts: number
  gateway: { pull: number; push: number; pull_bps: number; push_bps: number }  // 远程 worker ↔ ECS 网关(产物代理)
  tunnel: { rx: number; tx: number; rx_bps: number; tx_bps: number; up: boolean; tunnels: TunnelStat[] }  // ECS ↔ NAS 隧道
  // 注:按节点时间趋势走单独端点 /api/link-traffic/history(富时间线),快照不内嵌。
}

// WS /api/ws/global 每 2s 推 live 子集;本页只可靠消费这四段。
export type SystemStatus = Pick<FullStatus, 'jobs' | 'workers' | 'pools' | 'disk'>

// 系统事件流(GET /api/events)
export interface SystemEvent {
  ts: number
  kind: string
  job_id?: string
  step?: string
  reason?: string
  error?: string
  worker_id?: string
  pool?: string
  [k: string]: any
}

// AI 用量聚合(GET /api/usage)
export interface UsageByModel {
  provider: string
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  cache_creation_tokens: number
  cache_read_tokens: number
  cost_usd: number
  cache_hit_rate_pct: number
}
export interface UsageAggregate {
  calls: number
  total_input_tokens: number
  total_output_tokens: number
  total_cache_creation_tokens: number
  total_cache_read_tokens: number
  total_cost_usd: number
  total_num_turns: number
  total_duration_sec: number
  cache_hit_rate_pct: number
  by_model: UsageByModel[]
}
// LiteLLM 价表状态(GET /api/pricing)。fetched_at 为 ISO 串或 null(从未拉取)。
export interface PricingStatus {
  ready: boolean
  model_count: number
  fetched_at: string | null
  source_url: string
}

export const COMPONENT_KIND_LABELS: Record<ComponentKind, string> = {
  api: 'API 服务', scheduler: '调度器', redis: 'Redis', minio: '对象存储',
}
export const COMPONENT_STATUS_LABELS: Record<ComponentStatus, string> = {
  up: '在线', degraded: '降级', down: '离线', unknown: '采集失败',
}

export interface AuthStatus {
  bilibili: { has_cookies: boolean; status: string }
  youtube: { has_cookies: boolean; status: string }
}

// B站扫码登录契约:与后端 /api/bili/* 严格对齐。
export interface BiliStatus {
  logged_in: boolean
  uname: string | null
}

export interface BiliLoginStart {
  qrcode_key: string
  qr_png: string
  url: string
}

export type BiliLoginState = 'waiting' | 'scanned' | 'confirmed' | 'expired'

export interface BiliLoginPoll {
  state: BiliLoginState
  logged_in: boolean
  uname: string | null
}

export interface ProfileSummary {
  domain: string
  role: string
  terminology_count: number
}

export interface ProfileDetail {
  domain: string
  role?: string
  domain_context?: string
  output_style?: Record<string, any>
  terminology?: string[]
  do_not?: string[]
}

// 领域总览卡片(派生聚合)。与后端 GET /api/domains 对齐。
export interface DomainOverview {
  domain: string
  collection_count: number
  job_count: number
  concept_count: number
  subscription_count: number
  last_active_at: string | null
  // 展示元数据(来自 profile,未设则缺省;前端回退按 domain 名派生)
  display_name?: string
  icon?: string
  color?: string
  description?: string
  role?: string
}

// POST /api/domains 请求体(新建知识库)
export interface CreateDomainPayload {
  domain: string
  display_name?: string
  icon?: string
  color?: string
  role?: string
  description?: string
}

// GET /api/jobs/facets —— 后端聚合的分面计数
export interface JobFacets {
  source: Record<string, number>
  domain: Record<string, number>
  status: Record<string, number>
}

// GET /api/domains/{domain}/concept-timeline
export type TimelineGranularity = 'day' | 'week' | 'month'
export interface ConceptTimeline {
  granularity: TimelineGranularity
  buckets: string[]
  totals: Record<string, number>
  concepts: { term: string; buckets: Record<string, number>; total: number }[]
}

// 概念间类型化关系边(P2):prerequisite 有方向,其余无向;'cooccur' 仅图谱边用。
export type ConceptRel = 'prerequisite' | 'is_a' | 'part_of' | 'related'
export interface RelatedEdge {
  term: string
  rel: ConceptRel
}

// 概念图谱:节点=概念,边=related 真边(kind=rel)+ 共现降噪边(kind='cooccur',
// 仅共享 job 数≥min_cooccur)。后端 GET /api/domains/{d}/concept-graph。
export interface ConceptGraphNode {
  id: string
  term: string
  zh_name: string
  definition: string          // 短定义(首句/截断)
  status: string              // 'suggested' | 'accepted'
  is_topic: boolean
  occurrence_count: number
}
export interface ConceptGraphEdge {
  source: string              // term
  target: string              // term
  weight: number              // 共享 job 数(真边无共现时为 1)
  kind: ConceptRel | 'cooccur'
}
export interface ConceptGraph {
  nodes: ConceptGraphNode[]
  edges: ConceptGraphEdge[]
  stats: { node_count: number; edge_count: number; typed_edge_count: number; isolated_count: number }
}

// 集合的订阅源(自动追更)。无订阅则为 null。同步/开关端点用集合自身 id。
export interface CollectionSubscription {
  source_type: string        // 订阅来源 enum 由 /api/sources 和 OpenAPI 动态给出
  source_id: string          // B站 mid / 频道URL / feed URL / 目录路径 / 收藏夹id ...
  source_label?: string      // 后端派生来源短标签(bilibili/youtube/rss/local);前端=name+徽标
  enabled: boolean           // 自动同步开关 = collection.sync_enabled
  last_synced_at: string | null
  last_sync_status?: 'ok' | 'error' | 'syncing' | null  // 上次同步结果;驱动侧栏/详情状态点
  last_sync_error?: string | null                       // 同步出错时的错误摘要(error 态 tooltip/红字)
}

// 集合:与后端 CollectionResponse 严格对齐。
export interface Collection {
  id: string
  name: string
  domain: string
  description: string
  tags: string[]
  job_count: number
  created_at: string
  subscription: CollectionSubscription | null
  status_counts?: Record<string, number>  // 集合详情:job 各状态计数(done/processing/failed/pending…)
}

// 术语出现处(类型化):概念出现在哪条内容、什么类型、什么位置。
// title 仅详情端点 enrich(job 标题,缺则前端回退显示 job_id)。
export interface TermOccurrence {
  job_id: string
  content_type: string
  location: string | null
  title?: string | null
}

export type CanonicalEvidenceStatus = 'valid' | 'stale' | 'missing'
export type CanonicalEvidenceLinkKind = 'media' | 'pdf' | 'text' | 'image'

export type EvidenceBoundingBox = [number, number, number, number]

// locator 只是服务端的安全投影,不包含原始文件路径。前端不从它拼接 URL。
export type CanonicalEvidenceLocator =
  | { kind: 'media'; start_ms: number; end_ms: number }
  | { kind: 'pdf'; page: number; bbox: EvidenceBoundingBox | null }
  | { kind: 'text'; exact: string; prefix: string; suffix: string; dom_path: string | null }
  | { kind: 'image'; bbox: EvidenceBoundingBox; start_ms: number | null; end_ms: number | null; page: number | null }

export interface CanonicalEvidenceLink {
  kind: CanonicalEvidenceLinkKind
  href: string
  label: string
}

// GET/POST /api/evidence/* 的唯一消费者投影。stale/missing 的 locator/link 必须为 null。
export interface CanonicalEvidenceProjection {
  evidence_id: string
  status: CanonicalEvidenceStatus
  reason: string | null
  job_id: string | null
  note_type: string | null
  chunk_id: string | null
  section: string | null
  evidence_fingerprint: string | null
  source_fingerprint: string | null
  locator: CanonicalEvidenceLocator | null
  link: CanonicalEvidenceLink | null
  validated_at: string | null
}

// 概念主题:域内被标为主题(is_topic=1)的概念。与后端 GET /api/domains/{domain}/topic-concepts 对齐。
export interface TopicConcept {
  term: string
  definition: string
  occurrence_count: number
  related: RelatedEdge[]
  is_topic: boolean
}

// 术语:与后端 GlossaryTermResponse 严格对齐。
export interface GlossaryTerm {
  domain: string
  term: string
  definition: string
  zh_name: string             // 标准中文译名(实体双语名,可为空串)
  aliases: string[]           // 归并进本实体的变体名
  occurrences: TermOccurrence[]
  related: RelatedEdge[]      // 类型化关系边(后端读出时把存量字符串归一为 rel='related')
  status: string
  is_topic: boolean
  watched: boolean            // 概念订阅:关注后雷达/工作台优先呈现新动向
  definition_locked: boolean
  current_definition_version_id: string | null
  lock_revision: number
  created_at: string | null
  updated_at: string | null
}

export interface ConceptDefinitionVersion {
  definition_version_id: string
  domain: string
  term: string
  version: number
  definition: string
  source_evidence_ids: string[]
  source_set_fingerprint: string
  strategy: string
  provider: string | null
  model: string | null
  prompt_hash: string | null
  input_hash: string | null
  supersedes_version_id: string | null
  actor: string
  created_at: string
}

export interface ConceptEvidence {
  evidence_id: string
  job_id: string
  content_type: string
  source_fingerprint: string | null
  note_type: string | null
  chunk_id: string | null
  section: string | null
  excerpt: string | null
  reason: string | null
  locator: CanonicalEvidenceLocator | null
  link: CanonicalEvidenceLink | null
}

export type ConceptAttestationLevel = 'none' | 'supported' | 'corroborated' | 'strong'

export interface ConceptAttestation {
  domain: string
  term: string
  level: ConceptAttestationLevel
  evidence_count: number
  job_count: number
  source_fingerprint_count: number
  content_type_count: number
  source_set_fingerprint: string
  included: ConceptEvidence[]
  excluded: ConceptEvidence[]
}

export interface ConceptTermDetail extends GlossaryTerm {
  occurrence_total: number
  occurrence_limit: 100
  current_definition: ConceptDefinitionVersion
  definition_history: ConceptDefinitionVersion[]
  definition_history_total: number
  definition_history_limit: 100
  attestation: ConceptAttestation
}

export interface ConceptCasRequest {
  expected_current_version_id: string
  expected_lock_revision: number
}

export interface ConceptLockResponse {
  current_definition_version_id: string
  lock_revision: number
  locked: boolean
  changed: boolean
}

export type ConceptResynthesisReason = 'locked' | 'no_quorum' | 'source_set_unchanged' | 'input_too_large'

export interface ConceptResynthesizeResponse {
  created: boolean
  reason: ConceptResynthesisReason | null
  current: ConceptDefinitionVersion | null
  version: ConceptDefinitionVersion | null
  attestation: ConceptAttestation | null
}

// GET /api/jobs/{id}/concepts —— 本内容命中的概念(GlossaryTerm + 本 job 命中位置)
export interface JobConcept extends GlossaryTerm {
  job_occurrences: TermOccurrence[]
}

// 搜索结果项:与后端 SearchResultItem 严格对齐。
export interface SearchResultItem {
  job_id: string
  title: string | null
  note_type: string
  snippet: string
  content_type: string
  domain: string
  collection_id: string | null
  canonical_evidence: CanonicalEvidenceProjection[]
}

export interface SearchResponse {
  total: number
  items: SearchResultItem[]
}

// 跨源综合问答(POST /api/ask):答案 markdown + 引用来源列表。
export interface AskSource {
  job_id: string
  title: string
  domain: string
  content_type: string
  evidence: {
    chunk_id?: string | null
    note_type?: string | null
    section?: string | null
    snippet?: string | null
    chunk_index?: number | null
    char_start?: number | null
    char_end?: number | null
    timestamp_sec?: number | null
    page?: number | null
    frame_path?: string | null
    image_path?: string | null
  }
  canonical_evidence: CanonicalEvidenceProjection[]
}

export interface AskResponse {
  question: string
  // 异步:命中则投 AI task 给 ai-worker,task_id 用于轮询 result;无命中/投递失败 task_id=null。
  task_id: string | null
  answer_markdown: string | null  // 仅无命中/投递失败时直接给消息;有 task 时 null(答案走 result 端点)
  sources: AskSource[]
  retrieved_count: number
}

// 独立 AI task 结果(GET /api/ai-tasks/{task_id}/result):/ask、/digest 异步答案的轮询载体。
export interface AiTaskResult {
  status: 'pending' | 'error' | 'done'
  task_id: string
  content?: string
  answer_markdown?: string  // = content(ask 读这个)
  markdown?: string         // = content(digest 读这个)
  provider?: string
  model?: string
  cost_usd?: number
  error?: string
}

// 独立 AI task 白盒审计(GET /api/ai-tasks/{task_id}/log):每次 claude 调用一条。
export interface AiTaskLogCall {
  task_id: string
  exec_id?: string
  step?: string
  domain?: string | null
  provider?: string
  model?: string
  ok: boolean
  error?: string | null
  created_at?: string
  record?: any  // {routing:{requested,tier_used,attempts}, prompt:{system,messages,...}, output, raw, usage, flori}
}
export interface AiTaskLogResponse {
  task_id: string
  count: number
  calls: AiTaskLogCall[]
}

export interface StudyReviewState {
  due_at: string
  interval_days: number
  ease: number
  repetitions: number
  lapses: number
  last_grade: string | null
  last_reviewed_at: string | null
  updated_at: string
}

export interface StudyCard {
  card_id: string
  domain: string
  job_id: string | null
  concept_term: string | null
  card_type: string
  front: string
  back: string
  explanation: string
  evidence: any
  status: string
  source: string
  revision: number
  created_at: string
  updated_at: string
  review: StudyReviewState | null
}

export interface StudyCardListResponse {
  total: number
  items: StudyCard[]
}

export interface StudyStats {
  total: number
  statuses: {
    suggested: number
    active: number
    suspended: number
    rejected: number
  }
  due: number
  reviewed_cards: number
  reviews_total: number
  grades: {
    again: number
    hard: number
    good: number
    easy: number
  }
  retained_reviews: number
  retention_rate: number
}

export type StudySuggestionStatus = 'suggested' | 'accepted' | 'rejected'

export interface StudySuggestionBatch {
  batch_id: string
  domain: string
  status: 'pending_enqueue' | 'queued' | 'ready' | 'failed'
  revision: number
  attempt: number
  task_id: string
  provider: string
  model: string
  max_cards: number
  error_code: string | null
  error_message: string | null
  deadline_at: string
  evidence_count: number
  suggestion_count: number
  created_at: string
  updated_at: string
}

export interface StudySuggestionEvidence {
  evidence_id: string
  job_id: string
  chunk_id: string
  note_type: string
  source_domain: string
  current_domain: string
  title: string
  section: string
  quote: string
  quote_sha256: string
  body_sha256: string
  locator: Record<string, unknown>
  status: 'valid' | 'stale' | 'unavailable'
  invalid_reason: string | null
}

export interface StudySuggestion {
  suggestion_id: string
  batch_id: string
  ordinal: number
  status: StudySuggestionStatus
  revision: number
  domain: string
  concept_term: string | null
  knowledge_key: string
  card_type: 'basic' | 'cloze' | 'qa'
  front: string
  back: string
  explanation: string
  accepted_card_id: string | null
  rejection_reason: string | null
  evidence: StudySuggestionEvidence[]
  created_at: string
  updated_at: string
}

export interface StudySuggestionListResponse {
  total: number
  items: StudySuggestion[]
}

export interface StudySuggestionOperationsResponse {
  batch_id: string
  items: StudySuggestion[]
  cards: StudyCard[]
}

export interface StudyMasteryItem {
  domain: string
  concept_term: string
  score: number
  level: 'fragile' | 'learning' | 'mastered'
  reviewed_cards: number
  reviews_total: number
  last_reviewed_at: string
}

export interface StudyMasteryResponse {
  total: number
  items: StudyMasteryItem[]
}

export interface WsEvent {
  event: string
  step?: string
  worker?: string
  current?: number
  total?: number
  pct?: number
  message?: string
  duration_sec?: number
  meta?: Record<string, any>
  error?: string
  retries?: number
  reason?: string
  progress_pct?: number
}
