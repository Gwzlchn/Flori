// 内容类型(content_type)与笔记类型(note_type)的展示映射,前端单一事实源。
// 各视图统一从这里 import,不要在视图里各写一份:分散实现的回退值会漂移。
import type { Component } from 'vue'
import { Play, FileText, Newspaper, Headphones } from 'lucide-vue-next'
import { CONTENT_TYPE_LABELS } from '../types'

export { CONTENT_TYPE_LABELS }

const CONTENT_TYPE_ICONS: Record<string, Component> = {
  video: Play, paper: FileText, article: Newspaper, audio: Headphones,
}
const CONTENT_TYPE_PILLS: Record<string, string> = {
  video: 't-video', paper: 't-paper', article: 't-article', audio: 't-audio',
}

// 统一回退:未知/缺省类型一律按文章样式呈现,勿在视图另设回退。
export function contentTypeIcon(t: string | null | undefined): Component {
  return CONTENT_TYPE_ICONS[t ?? ''] ?? Newspaper
}
export function contentTypePill(t: string | null | undefined): string {
  return CONTENT_TYPE_PILLS[t ?? ''] ?? 't-article'
}
export function contentTypeLabel(t: string | null | undefined): string {
  return CONTENT_TYPE_LABELS[t ?? ''] ?? (t ?? '')
}

// 笔记类型(note_type)徽章文案,与后端取值对齐(smart|mechanical|transcript)。
export const NOTE_TYPE_LABELS: Record<string, string> = {
  smart: '智能笔记',
  mechanical: '机械稿',
  transcript: '逐字稿',
}
export function noteTypeLabel(t: string | null | undefined): string {
  return NOTE_TYPE_LABELS[t ?? ''] ?? (t ?? '')
}
