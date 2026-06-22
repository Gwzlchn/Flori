import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import StatusBadge from './StatusBadge.vue'

describe('StatusBadge', () => {
  it('已知状态 → 中文 + 对应徽章色', () => {
    const w = mount(StatusBadge, { props: { status: 'done' } })
    expect(w.text()).toBe('已完成')
    expect(w.classes()).toContain('badge')
    expect(w.classes()).toContain('b-ok')
  })

  it('未知状态 → 原样文本 + b-mut 回退', () => {
    const w = mount(StatusBadge, { props: { status: 'weird-unknown' } })
    expect(w.text()).toBe('weird-unknown')
    expect(w.classes()).toContain('b-mut')
  })

  it('skipped → 虚线类、且不带失败色', () => {
    const w = mount(StatusBadge, { props: { status: 'skipped' } })
    expect(w.classes()).toContain('b-dashed')
    expect(w.classes()).not.toContain('b-bad')
  })
})
