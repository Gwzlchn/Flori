import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'

// TermDetailView: useDomainStore().term(domain, term) 拉详情(内部走 useApi.get),
// 标为主题走 useApi.post。route.params 提供 domain/term。
const route = { params: { domain: 'ml', term: 'gradient' }, query: {} }
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push, replace: vi.fn() }),
}))

const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import TermDetailView from './TermDetailView.vue'

function makeTerm(over: Record<string, any> = {}) {
  return {
    domain: 'ml',
    term: 'gradient',
    zh_name: '梯度',
    aliases: [],
    definition: '梯度是函数的偏导数向量',
    related: [
      { term: 'backprop', rel: 'related' },
      { term: 'loss', rel: 'prerequisite' },
    ],
    occurrences: [
      { job_id: 'jobA', content_type: 'paper', location: 'p.3' },
      { job_id: 'jobB', content_type: 'video', location: '' },
    ],
    status: 'accepted',
    is_topic: false,
    ...over,
  }
}

function mountView() {
  return mount(TermDetailView, {
    global: {
      plugins: [createTestingPinia({ createSpy: vi.fn, stubActions: false })],
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  route.params = { domain: 'ml', term: 'gradient' }
})

describe('TermDetailView', () => {
  it('挂载即按 domain/term 拉取详情', async () => {
    api.get.mockResolvedValue(makeTerm())
    mountView()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/domains/ml/terms/gradient')
  })

  it('加载成功渲染概念名、定义、关联与出现处', async () => {
    api.get.mockResolvedValue(makeTerm())
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('gradient')
    expect(t).toContain('梯度是函数的偏导数向量')
    expect(t).toContain('backprop')
    expect(t).toContain('jobA')
    expect(t).toContain('2 处出现')
    expect(t).toContain('2 个关联')
  })

  it('is_topic 时显示主题概念徽标且按钮为取消主题', async () => {
    api.get.mockResolvedValue(makeTerm({ is_topic: true }))
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('主题概念')
    expect(t).toContain('取消主题')
  })

  it('非主题时按钮为标为主题', async () => {
    api.get.mockResolvedValue(makeTerm({ is_topic: false }))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('标为主题')
  })

  it('无定义与无关联时显示占位文案', async () => {
    api.get.mockResolvedValue(makeTerm({ definition: '', related: [], occurrences: [] }))
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('暂无定义')
    expect(t).toContain('暂无关联概念')
    expect(t).toContain('还没有内容提到这个概念')
  })

  it('404 错误渲染概念不存在态', async () => {
    api.get.mockRejectedValueOnce(new Error('API 404: not found'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('概念不存在或已删除')
  })

  it('非 404 错误渲染错误态并可重试', async () => {
    api.get.mockRejectedValueOnce(new Error('API 500: boom'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('API 500: boom')
    api.get.mockResolvedValueOnce(makeTerm())
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('梯度是函数的偏导数向量')
  })

  it('点击标为主题调用 POST topic 接口并用返回值刷新', async () => {
    api.get.mockResolvedValue(makeTerm({ is_topic: false }))
    api.post.mockResolvedValue(makeTerm({ is_topic: true }))
    const w = mountView()
    await flushPromises()
    const btn = w.findAll('button').find((b) => b.text().includes('标为主题'))!
    await btn.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(
      '/api/glossary/ml/gradient/topic',
      { is_topic: true },
    )
    expect(w.text()).toContain('取消主题')
  })

  it('点击关联概念跳转到对应概念页', async () => {
    api.get.mockResolvedValue(makeTerm())
    const w = mountView()
    await flushPromises()
    await w.find('.chip').trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/ml/concepts/backprop')
  })

  it('精确 evidence 关系落库前,出现处只保留普通 job 导航', async () => {
    api.get.mockResolvedValue(makeTerm())
    const w = mountView()
    await flushPromises()
    expect(w.find('.occ a.evidence-locator').exists()).toBe(false)
    await w.findAll('.occ')[1].trigger('click')
    expect(push).toHaveBeenCalledWith('/content/jobB')
  })
})
