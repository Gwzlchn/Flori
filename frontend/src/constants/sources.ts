// 来源列表和文案从 /api/sources 动态装载;configs/sources.yaml 是跨前后端唯一事实源。
import { reactive } from 'vue'
import type { Component } from 'vue'
import {
  Rss, Youtube, FolderInput, Star, ListVideo, BookOpen,
} from 'lucide-vue-next'
import { useApi } from '../composables/useApi'
import type { SourceCatalogWire } from '../types/wire'

type Icon = Component

export interface SourceTypeMeta {
  type: string          // source_type(后端枚举)
  label: string         // 选择器里的人类名
  group: string         // 派生 source_label(徽标/配色键)
  icon: Icon            // 列表/卡片图标
  idLabel: string       // source_id 输入框的字段名
  placeholder: string   // 输入占位
  hint: string          // 输入说明
  homeUrlTemplate?: string
}

const ICONS: Record<string, Icon> = {
  rss: Rss, youtube: Youtube, 'folder-input': FolderInput,
  star: Star, 'list-video': ListVideo, 'book-open': BookOpen,
}

export const SOURCE_TYPES = reactive<SourceTypeMeta[]>([])
export const JOB_SOURCE_LABELS = reactive<Record<string, string>>({})
export const CONTENT_TYPE_LABELS = reactive<Record<string, string>>({})
export const CONTENT_TYPE_CATALOG = reactive<{ type: string; label: string; uploadExtensions: string[] }[]>([])
const BY_TYPE = reactive<Record<string, SourceTypeMeta>>({})
let loading: Promise<void> | null = null

export function installSourceCatalog(catalog: SourceCatalogWire): void {
  SOURCE_TYPES.splice(0, SOURCE_TYPES.length)
  CONTENT_TYPE_CATALOG.splice(0, CONTENT_TYPE_CATALOG.length)
  for (const key of Object.keys(BY_TYPE)) delete BY_TYPE[key]
  for (const key of Object.keys(JOB_SOURCE_LABELS)) delete JOB_SOURCE_LABELS[key]
  for (const key of Object.keys(CONTENT_TYPE_LABELS)) delete CONTENT_TYPE_LABELS[key]

  for (const raw of catalog.subscription_sources || []) {
    const meta: SourceTypeMeta = {
      type: raw.type, label: raw.label, group: raw.group,
      icon: ICONS[raw.icon] ?? Rss,
      idLabel: raw.id_label, placeholder: raw.placeholder, hint: raw.hint,
      homeUrlTemplate: raw.home_url_template ?? undefined,
    }
    SOURCE_TYPES.push(meta)
    BY_TYPE[meta.type] = meta
  }
  for (const raw of catalog.job_sources || []) JOB_SOURCE_LABELS[raw.type] = raw.label
  for (const raw of catalog.content_types || []) {
    CONTENT_TYPE_LABELS[raw.type] = raw.label
    CONTENT_TYPE_CATALOG.push({
      type: raw.type, label: raw.label, uploadExtensions: raw.upload_extensions || [],
    })
  }
}

export async function ensureSourceCatalog(): Promise<void> {
  if (SOURCE_TYPES.length) return
  if (!loading) {
    const api = useApi()
    loading = api.get<SourceCatalogWire>('/api/sources')
      .then(installSourceCatalog)
      .catch(() => undefined)
      .finally(() => { loading = null })
  }
  await loading
}

export function uploadAccept(): string {
  return CONTENT_TYPE_CATALOG.flatMap((item) => item.uploadExtensions).join(',')
}

export function contentTypeForUpload(filename: string): string | undefined {
  const lower = filename.toLowerCase()
  return CONTENT_TYPE_CATALOG.find((item) =>
    item.uploadExtensions.some((extension) => lower.endsWith(extension.toLowerCase())),
  )?.type
}

export function jobSourceLabel(s: string | null | undefined): string {
  return s ? (JOB_SOURCE_LABELS[s] || s) : '—'
}

// 给订阅集合取来源标签(优先后端 source_label,回退按 source_type 派生)。
export function sourceLabelOf(sub: { source_type: string; source_label?: string } | null | undefined): string {
  if (!sub) return ''
  if (sub.source_label) return sub.source_label
  return BY_TYPE[sub.source_type]?.group ?? sub.source_type
}

export function sourceBadge(label: string) {
  const icon = SOURCE_TYPES.find((source) => source.group === label)?.icon ?? Rss
  const cls = ({ bilibili: 'b-info', youtube: 'b-bad', rss: 'b-warn' } as Record<string, string>)[label] ?? 'b-mut'
  return { text: label, icon, cls }
}

export function sourceMeta(type: string): SourceTypeMeta | undefined {
  return BY_TYPE[type]
}

// 订阅同步状态(单一事实源,侧栏/列表/详情共用)。
// 后端 subscription 增量字段:last_sync_status(ok|error|syncing|null)+ last_sync_error(text|null)。
// 注:这里参数用宽松可选类型,避免与 types/index.ts CollectionSubscription 耦合(后者新增字段为可选)。
export interface SubStateInput {
  enabled?: boolean
  last_synced_at?: string | null
  last_sync_status?: 'ok' | 'error' | 'syncing' | null
  last_sync_error?: string | null
}

// 由订阅推 5 态(优先级:暂停 > 同步中 > 出错 > 从未同步 > 订阅中)。无订阅返回 ''。
export function subState(sub: SubStateInput | null | undefined): string {
  if (!sub) return ''
  if (!sub.enabled) return 'paused'
  if (sub.last_sync_status === 'syncing') return 'syncing'
  if (sub.last_sync_status === 'error') return 'error'
  if (!sub.last_synced_at) return 'never'
  return 'active'
}

// 每态 → CSS class 后缀 + 默认 tooltip 文案。class 名与状态名一致,供 .sub-dot.<cls> 上色。
export const SUB_STATE_META: Record<string, { cls: string; tip: string }> = {
  active: { cls: 'active', tip: '订阅中' },
  paused: { cls: 'paused', tip: '已暂停追更' },
  never: { cls: 'never', tip: '尚未同步' },
  error: { cls: 'error', tip: '上次同步出错' },
  syncing: { cls: 'syncing', tip: '同步中…' },
}

// tooltip 文案:出错态追加真实错误摘要(last_sync_error)。
export function subTip(sub: SubStateInput | null | undefined): string {
  const st = subState(sub)
  const meta = SUB_STATE_META[st]
  if (!meta) return ''
  if (st === 'error' && sub?.last_sync_error) return `${meta.tip}:${sub.last_sync_error}`
  return meta.tip
}

// 订阅源主页/原始链接,集合详情页「来源地址」外链用。尽力而为,拿不到返回 null。
export function sourceHomeUrl(sub: { source_type: string; source_id: string }): string | null {
  const { source_type: type, source_id: id } = sub
  if (!id) return null
  if (/^https?:\/\//.test(id)) return id
  const template = BY_TYPE[type]?.homeUrlTemplate
  if (!template) return null
  const resolved = template.replace('{source_id}', encodeURIComponent(id))
  return /^https?:\/\//.test(resolved) ? resolved : null
}
