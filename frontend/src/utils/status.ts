// 状态枚举 → 中文文案,全站统一(StatusBadge / StepWorkbench 等共用),杜绝文案漂移。
// 视觉配色(badge/pill 类)仍由各组件按自身样式体系决定;这里只管「文案」单一来源。
export const STATUS_LABELS: Record<string, string> = {
  // job
  pending: '等待', downloading: '下载中', processing: '处理中',
  done: '已完成', failed: '失败',
  // step
  waiting: '等待', ready: '就绪', running: '运行中', skipped: '跳过',
  // worker(idle/busy 为旧态兼容)
  idle: '空闲', busy: '忙碌',
  'online-idle': '空闲', 'online-busy': '忙碌', paused: '暂停',
  offline: '离线', stale: '失联',
  // 系统组件四态(api/scheduler/redis/minio;专用,与 worker 态分开)
  up: '在线', degraded: '降级', down: '离线', unknown: '采集失败',
  // concept
  suggested: '候选', accepted: '已采纳',
}

export function statusLabel(s: string): string {
  return STATUS_LABELS[s] ?? s
}
