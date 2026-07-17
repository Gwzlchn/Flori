// 内容类型(content_type)与笔记类型(note_type)的展示映射,前端单一事实源。
// 各视图统一从这里 import,不要在视图里各写一份:分散实现的回退值会漂移。
import type { Component } from 'vue'
import { Play, FileText, Newspaper, Headphones, BookOpen, GraduationCap } from 'lucide-vue-next'
import { CONTENT_TYPE_LABELS, DOCUMENT_KIND_LABELS } from '../constants/sources'

export { CONTENT_TYPE_LABELS }

const CONTENT_TYPE_ICONS: Record<string, Component> = {
  video: Play, document: FileText, audio: Headphones,
}
const CONTENT_TYPE_PILLS: Record<string, string> = {
  video: 't-video', document: 't-document', audio: 't-audio',
}

const DOCUMENT_KIND_ICONS: Record<string, Component> = {
  research_paper: FileText,
  article: Newspaper,
  whitepaper: FileText,
  report: FileText,
  book_chapter: BookOpen,
  documentation: BookOpen,
  standard: FileText,
  thesis: GraduationCap,
  unknown: FileText,
}

// 未知内容族使用中性的文档外观,不能伪装成某个具体文档体裁.
export function contentTypeIcon(t: string | null | undefined): Component {
  return CONTENT_TYPE_ICONS[t ?? ''] ?? FileText
}
export function contentTypePill(t: string | null | undefined): string {
  return CONTENT_TYPE_PILLS[t ?? ''] ?? 't-document'
}

export function documentKindIcon(kind: string | null | undefined): Component {
  return DOCUMENT_KIND_ICONS[kind ?? ''] ?? FileText
}

export function documentKindLabel(kind: string | null | undefined): string {
  return DOCUMENT_KIND_LABELS[kind ?? ''] ?? (kind ?? '')
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
