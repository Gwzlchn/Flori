import { describe, it, expect } from 'vitest'
import { fmtDateTime } from './datetime'

describe('fmtDateTime', () => {
  it('null/undefined/空串 → —', () => {
    expect(fmtDateTime(null)).toBe('—')
    expect(fmtDateTime(undefined)).toBe('—')
    expect(fmtDateTime('')).toBe('—')
  })

  it('非法日期 → —', () => {
    expect(fmtDateTime('not-a-date')).toBe('—')
    expect(fmtDateTime(NaN)).toBe('—')
  })

  it('合法 Date 按 YYYY/MM/DD HH:MM:SS 补零(本地时区)', () => {
    // 用本地构造 + 本地读取,规避容器时区差异(fmtDateTime 走 getFullYear 等本地方法)
    const d = new Date(2026, 0, 5, 3, 7, 9) // 2026-01-05 03:07:09
    expect(fmtDateTime(d)).toBe('2026/01/05 03:07:09')
  })

  it('接受毫秒数', () => {
    const ms = new Date(2026, 11, 31, 23, 59, 59).getTime()
    expect(fmtDateTime(ms)).toBe('2026/12/31 23:59:59')
  })
})
