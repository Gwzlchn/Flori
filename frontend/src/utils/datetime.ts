// 全站统一的日期时间格式:YYYY/MM/DD HH:MM:SS(补零)。接受 ISO 串 / 毫秒数 / Date。
export function fmtDateTime(v: string | number | Date | null | undefined): string {
  if (v == null || v === '') return '—'
  const d = new Date(v)
  if (isNaN(d.getTime())) return '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// 时长(秒 → 人类可读)。全站统一,替代各处手写的 fmtDuration/fmtDur。
//   <60s          → "12s"(decimalSeconds 时 "12.3s",用于步骤耗时)
//   ≥60s 且 <1h   → "5m03s"
//   ≥1h           → "2h05m"
export interface FmtDurationOpts {
  fallback?: string        // null/NaN/负数时的占位,默认 '—'
  decimalSeconds?: boolean // 不足 1 分钟时保留一位小数(步骤耗时用)
}
export function fmtDuration(sec: number | null | undefined, opts: FmtDurationOpts = {}): string {
  const { fallback = '—', decimalSeconds = false } = opts
  if (sec == null || isNaN(sec) || sec < 0) return fallback
  if (sec < 60) return decimalSeconds ? `${sec.toFixed(1)}s` : `${Math.floor(sec)}s`
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h${String(m).padStart(2, '0')}m`
  return `${m}m${String(s % 60).padStart(2, '0')}s`
}

// 相对时间("…前")。全站统一,替代各处手写的 ago/syncAgo/activeAgo。
//   style 'short' → "12s 前 / 5m 前 / 3h 前 / 2d 前"
//   style 'cn'    → "12 秒前 / 5 分钟前 / 3 小时前 / 2 天前"
//   absoluteAfterDay=true 时,超过 1 天回退为绝对时间(fmtDateTime)。
export interface FmtRelativeOpts {
  fallback?: string                 // null/空/NaN 时的占位,默认 '—'
  style?: 'short' | 'cn'            // 单位风格,默认 'short'
  absoluteAfterDay?: boolean        // >24h 用绝对时间替代 "Nd 前"
  suffix?: string                   // 整体后缀(如 '同步'/'活跃');默认空
}
export function fmtRelative(v: string | number | Date | null | undefined, opts: FmtRelativeOpts = {}): string {
  const { fallback = '—', style = 'short', absoluteAfterDay = false, suffix = '' } = opts
  if (v == null || v === '') return fallback
  const diff = Date.now() - new Date(v).getTime()
  if (isNaN(diff)) return fallback
  const sec = Math.floor(diff / 1000)
  const u = style === 'cn'
    ? { s: ' 秒前', m: ' 分钟前', h: ' 小时前', d: ' 天前' }
    : { s: 's 前', m: 'm 前', h: 'h 前', d: 'd 前' }
  const tail = suffix ? suffix : ''
  let core: string
  if (sec < 60) core = `${sec}${u.s}`
  else {
    const min = Math.floor(sec / 60)
    if (min < 60) core = `${min}${u.m}`
    else {
      const hr = Math.floor(min / 60)
      if (hr < 24) core = `${hr}${u.h}`
      else if (absoluteAfterDay) return `${fmtDateTime(v)}${tail ? ' ' + tail : ''}`
      else core = `${Math.floor(hr / 24)}${u.d}`
    }
  }
  return tail ? `${core}${tail}` : core
}

// 字节 → 人类可读(KB/MB/GB/TB,1024 进制)。全站统一,供流量/容量展示。
//   <1KB → "512 B" / ≥1KB → "1.5 KB" / "12.3 MB" / "1.20 GB"。负/NaN/空 → fallback。
export function fmtBytes(n: number | null | undefined, fallback = '—'): string {
  if (n == null || isNaN(n) || n < 0) return fallback
  if (n < 1024) return `${Math.round(n)} B`
  const units = ['KB', 'MB', 'GB', 'TB', 'PB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`
}
