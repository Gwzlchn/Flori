// 全站统一的日期时间格式:YYYY/MM/DD HH:MM:SS(补零)。接受 ISO 串 / 毫秒数 / Date。
export function fmtDateTime(v: string | number | Date | null | undefined): string {
  if (v == null || v === '') return '—'
  const d = new Date(v)
  if (isNaN(d.getTime())) return '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
