// Worker 展示派生(状态→点色 / 算力描述)。此前 WorkersView 与 WorkerDetailView 各写一份且已漂移
// (分隔符 空格 vs ·、AI 文案、CPU 兜底 type.toUpperCase() vs —);统一到此处单一来源(审计 R-M6)。

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

// 算力描述:GPU 名(+显存)优先;否则 AI 给完整文案、其余类型给大写类型名(列表里仍可辨类型)。
export function workerComputeDesc(
  w: { gpu_name?: string | null; gpu_memory_mb?: number | null; type: string },
): string {
  if (w.gpu_name) {
    return w.gpu_memory_mb ? `${w.gpu_name} · ${Math.round(w.gpu_memory_mb / 1024)}GB` : w.gpu_name
  }
  return w.type === 'ai' ? 'AI（Claude / API）' : w.type.toUpperCase()
}
