// 通用数值格式化(前端各处统一引用,避免重复实现 byte→KB/MB/GB 逻辑)。

// 字节 → 人类可读(B/KB/MB/GB/TB)。null/undefined/<0 → '—';0 → '0 B'。
// 进位 1024;<100 保留 1 位小数,≥100 或整字节取整(与 JobDetailView.fmtSize 同口径)。
export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes == null || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const u = ['KB', 'MB', 'GB', 'TB']
  let v = bytes / 1024, i = 0
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`
}
