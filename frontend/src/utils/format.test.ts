import { describe, expect, it } from 'vitest'

import { fmtCredits } from './format'

describe('fmtCredits', () => {
  it('显式标准单位并固定四位小数', () => {
    expect(fmtCredits(2.2650132450000005)).toBe('2.2650 credits')
    expect(fmtCredits(0)).toBe('0.0000 credits')
  })

  it('null与非法值显示未上报,不冒充为零', () => {
    expect(fmtCredits(null)).toBe('credits 未上报')
    expect(fmtCredits(undefined)).toBe('credits 未上报')
    expect(fmtCredits(Number.NaN)).toBe('credits 未上报')
  })
})
