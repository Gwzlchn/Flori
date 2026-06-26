// 系统事件 kind → 中文标签 / 严重度点色 / 摘要。SystemView(概览摘要)与 EventsView(全量页)共用,避免重复。
import type { SystemEvent } from '../types'

const EVENT_LABELS: Record<string, string> = {
  orphan_reclaimed: '孤儿回收', step_stuck: '卡住步', no_worker: '无 worker',
  worker_cleaned: 'worker 清理', job_failed: '作业失败',
}
const EVENT_DOT: Record<string, string> = {
  orphan_reclaimed: 'd-warn', step_stuck: 'd-warn', no_worker: 'd-bad',
  worker_cleaned: 'd-mut', job_failed: 'd-bad',
}
export function eventLabel(k: string): string { return EVENT_LABELS[k] ?? k }
export function eventDot(k: string): string { return EVENT_DOT[k] ?? 'd-mut' }
export function eventSummary(e: SystemEvent): string {
  const parts: string[] = []
  if (e.job_id) parts.push(e.job_id)
  if (e.step) parts.push(e.step)
  if (e.pool) parts.push(`池 ${e.pool}`)
  if (e.reason) parts.push(e.reason)
  if (e.error) parts.push(String(e.error).slice(0, 80))
  if (e.worker_id) parts.push(e.worker_id)
  return parts.join(' · ')
}
// 已知事件类型(供 EventsView 类型筛选下拉)。
export const EVENT_KINDS = Object.keys(EVENT_LABELS)
