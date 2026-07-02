// Worker 展示派生(状态→点色 / 算力描述),单一来源。
// WorkersView / WorkerDetailView 统一从这里 import,不要在视图各写一份:分散实现的分隔符和兜底文案会漂移。

// dot 颜色跟随 worker 状态。
export function workerDotClass(status: string | null | undefined): string {
  switch (status) {
    case 'online-idle': return 'd-ok'
    case 'online-busy': return 'd-info'
    case 'paused': return 'd-warn'
    case 'stale': return 'd-bad'
    default: return 'd-mut'
  }
}

// 组件四态 → dot 颜色(系统组件卡;复用既有点色,无新颜色)。
export function componentDotClass(status: string | null | undefined): string {
  switch (status) {
    case 'up': return 'd-ok'
    case 'degraded': return 'd-warn'
    case 'down': return 'd-bad'
    default: return 'd-mut'   // unknown / 缺失
  }
}

// 算力描述:GPU 名优先,有显存则一并带上。否则 ai 类型给完整文案,其余类型给大写类型名,列表里仍可辨类型。
export function workerComputeDesc(
  w: { gpu_name?: string | null; gpu_memory_mb?: number | null; type: string },
): string {
  if (w.gpu_name) {
    return w.gpu_memory_mb ? `${w.gpu_name} · ${Math.round(w.gpu_memory_mb / 1024)}GB` : w.gpu_name
  }
  return w.type === 'ai' ? 'AI（Claude / API）' : w.type.toUpperCase()
}
