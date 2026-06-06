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

export interface Worker {
  id: string
  type: string
  pools: string[]
  hostname: string | null
  status: string
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
