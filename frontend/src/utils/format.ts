// 通用数值格式化(前端各处统一引用,避免重复实现 byte→KB/MB/GB 逻辑)。全站 fmtBytes 唯一来源。

// 字节 → 人类可读(B/KB/MB/GB/TB/PB,1024 进制)。null/undefined/NaN/<0 → fallback(默认 '—');
//   <1KB → "512 B";≥1KB 按量级取小数:≥100→0 位、≥10→1 位、否则 2 位(如 "1.5 KB" / "12.3 MB" / "1.20 GB")。
export function fmtBytes(n: number | null | undefined, fallback = '—'): string {
  if (n == null || isNaN(n) || n < 0) return fallback
  if (n < 1024) return `${Math.round(n)} B`
  const units = ['KB', 'MB', 'GB', 'TB', 'PB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`
}
