import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import AboutView from './AboutView.vue'

// AboutView 是纯静态页(无 store/router/请求),验证 view 能在 jsdom 下挂载并渲染关键文案。
describe('AboutView', () => {
  it('渲染标题与三层心智模型关键文案', () => {
    const w = mount(AboutView)
    const t = w.text()
    expect(t).toContain('关于 Mnemo')
    expect(t).toContain('机械版')
    expect(t).toContain('智能版')
    expect(t).toContain('核心循环')
  })
})
