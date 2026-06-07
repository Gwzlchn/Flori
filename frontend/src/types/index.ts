export interface JobSummary {
  job_id: string
  content_type: string
  status: string
  created_at: string
  title: string | null
  progress_pct: number
  source: string | null
  domain: string
}

export interface StepInfo {
  name: string
  status: string
  duration_sec: number | null
  meta: Record<string, any>
  error: string | null
}

export interface JobDetail extends JobSummary {
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
  | 'draining'
  | 'stale'

export interface Worker {
  id: string
  type: string
  pools: string[]
  tags: string[]
  reject_tags: string[]
  hostname: string | null
  gpu_name: string | null
  gpu_memory_mb: number | null
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

export interface SystemStatus {
  jobs: {
    total: number
    done: number
    processing: number
    failed: number
  }
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

// 集合：与后端 CollectionResponse 严格对齐。
export interface Collection {
  id: string
  name: string
  domain: string
  description: string
  tags: string[]
  job_count: number
  created_at: string
}

// 术语：与后端 GlossaryTermResponse 严格对齐。
export interface GlossaryTerm {
  domain: string
  term: string
  definition: string
  sources: string[]
  related: string[]
  status: string
  source_type: string
  created_at: string
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
