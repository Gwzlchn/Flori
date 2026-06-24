// 面包屑段:TopBar 渲染 + store.crumbOverride 共用同一形状。
export interface BreadcrumbSeg {
  t: string        // 文案
  to?: string      // 可点跳转目标(末段通常无 to)
}

// 内容类型:与后端 pipeline 一一对应(video/paper/article/audio)。
export type ContentType = 'video' | 'paper' | 'article' | 'audio'

// 内容类型中文徽章:前端各处展示统一引用。
export const CONTENT_TYPE_LABELS: Record<string, string> = {
  video: '视频',
  paper: '论文',
  article: '文章',
  audio: '播客',
}

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

export interface WorkerJob {
  job_id: string
  step: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_sec: number | null
  error: string | null
}

// ── 系统健康总览页(/system)──
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
export interface WorkerCount { online: number; busy: number }
export interface JobCounts   { total: number; done: number; processing: number; failed: number; pending: number }
export interface DiskInfo    { used_gb: number; available_gb: number; total_gb: number; used_pct: number }
export interface Throughput  { done: number; failed: number }

// GET /api/status 完整形状(进页 1 次 + 每 15s 轮询拿全量)。
export interface FullStatus {
  version: string
  components: SystemComponent[]
  workers: Record<string, WorkerCount>
  pools: Record<string, PoolStat>
  jobs: JobCounts
  disk: DiskInfo
  throughput_1h?: Throughput
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

// B站扫码登录契约：与后端 /api/bili/* 严格对齐。
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

// 领域总览卡片（派生聚合）。与后端 GET /api/domains 对齐。
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

// 集合的订阅源（自动追更）。无订阅则为 null。同步/开关端点用集合自身 id。
export interface CollectionSubscription {
  source_type: string        // bilibili_up/fav/collection · youtube_channel · rss · local_dir
  source_id: string          // B站 mid / 频道URL / feed URL / 目录路径 / 收藏夹id ...
  source_label?: string      // 后端派生来源短标签(bilibili/youtube/rss/local);前端=name+徽标
  enabled: boolean           // 自动同步开关 = collection.sync_enabled
  last_synced_at: string | null
  last_sync_status?: 'ok' | 'error' | 'syncing' | null  // 上次同步结果(二期);驱动侧栏/详情状态点
  last_sync_error?: string | null                       // 同步出错时的错误摘要(error 态 tooltip/红字)
}

// 集合：与后端 CollectionResponse 严格对齐。
export interface Collection {
  id: string
  name: string
  domain: string
  description: string
  tags: string[]
  job_count: number
  created_at: string
  subscription: CollectionSubscription | null
  status_counts?: Record<string, number>  // 集合详情:job 各状态计数(done/processing/failed/pending…)(二期)
}

// 术语出现处（类型化）：概念出现在哪条内容、什么类型、什么位置。
export interface TermOccurrence {
  job_id: string
  content_type: string
  location: string | null
}

// 概念主题：域内被标为主题(is_topic=1)的概念。与后端 GET /api/domains/{domain}/topic-concepts 对齐。
export interface TopicConcept {
  term: string
  definition: string
  occurrence_count: number
  related: string[]
  is_topic: boolean
}

// 术语：与后端 GlossaryTermResponse 严格对齐。
export interface GlossaryTerm {
  domain: string
  term: string
  definition: string
  occurrences: TermOccurrence[]
  related: string[]
  status: string
  is_topic: boolean
  definition_locked: boolean
  created_at: string
}

// GET /api/jobs/{id}/concepts —— 本内容命中的概念(GlossaryTerm + 本 job 命中位置)
export interface JobConcept extends GlossaryTerm {
  job_occurrences: TermOccurrence[]
}

// 搜索结果项：与后端 SearchResultItem 严格对齐。
export interface SearchResultItem {
  job_id: string
  title: string | null
  note_type: string
  snippet: string
  content_type: string
  domain: string
  collection_id: string | null
}

export interface SearchResponse {
  total: number
  items: SearchResultItem[]
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
