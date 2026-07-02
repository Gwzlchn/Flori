import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AboutView from './AboutView.vue'

// AboutView 用 store 拉 /api/pipelines 动态渲染「四条内容流水线」(= pipelines.yaml 单一源)。
// 测试环境无后端:onMounted 的请求被 try/catch 吞掉、流水线区留空,验静态关键文案仍正常渲染 + 不报错挂载。
describe('AboutView', () => {
  it('渲染标题与三层心智模型关键文案', () => {
    setActivePinia(createPinia())
    const w = mount(AboutView)
    const t = w.text()
    expect(t).toContain('关于 Flori')
    expect(t).toContain('机械版')
    expect(t).toContain('智能版')
    expect(t).toContain('核心循环')
    expect(t).toContain('四条内容流水线')
  })
})
