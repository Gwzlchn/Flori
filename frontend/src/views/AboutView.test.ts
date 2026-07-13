import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AboutView from './AboutView.vue'

// AboutView 用 store 拉 /api/pipelines 动态渲染「四条内容流水线」(= pipelines.yaml 单一源)。
// 测试环境无后端:onMounted 的请求被 try/catch 吞掉、流水线区留空,验静态能力口径仍正常渲染。
describe('AboutView', () => {
  it('渲染标题、三层模型与统一成熟度口径', () => {
    setActivePinia(createPinia())
    const w = mount(AboutView)
    const t = w.text()
    expect(t).toContain('关于 Flori')
    expect(t).toContain('原始 / 机械材料')
    expect(t).toContain('智能版')
    expect(t).toContain('核心循环')
    expect(t).toContain('四条内容流水线')
    expect(t).toContain('能力成熟度')
    expect(t).toContain('first-pass')
    expect(t).toContain('未开始')
    expect(t).toContain('证据型自动卡片')
    expect(t).not.toContain('以上为后续里程碑，尚未构建')
  })
})
