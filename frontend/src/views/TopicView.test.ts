import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'

// TopicView 通过 useDomainStore().topic(domain, topic) 拉数据,内部走 useApi.get。
// 这里 mock useApi,让真实 store action 执行;stubActions:false 保留真实 action。
const route = { params: { domain: 'ml', topic: 'transformer' }, query: {} }
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push, replace: vi.fn() }),
}))

const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import TopicView from './TopicView.vue'

function mountView() {
  return mount(TopicView, {
    global: {
      plugins: [createTestingPinia({ createSpy: vi.fn, stubActions: false })],
      stubs: { StatusBadge: true },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  route.params = { domain: 'ml', topic: 'transformer' }
})

describe('TopicView', () => {
  it('渲染头部主题名与面包屑（domain / 条数）', async () => {
    api.get.mockResolvedValue({ jobs: [], total: 0 })
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('transformer')
    expect(t).toContain('ml')
    expect(t).toContain('共 0 条内容')
  })

  it('挂载即按 domain/topic 拉取主题数据', async () => {
    api.get.mockResolvedValue({ jobs: [], total: 0 })
    mountView()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/domains/ml/topics/transformer')
  })

  it('空结果渲染空态文案', async () => {
    api.get.mockResolvedValue({ jobs: [], total: 0 })
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('这个主题还没有内容')
  })

  it('有内容时渲染列表项标题与计数', async () => {
    api.get.mockResolvedValue({
      jobs: [
        { job_id: 'j1', title: 'Attention Is All You Need', content_type: 'document', document_kind: 'research_paper', status: 'done', created_at: '2026-01-01', source: 'arxiv' },
        { job_id: 'j2', title: null, content_type: 'video', status: 'pending', created_at: '2026-01-02' },
      ],
      total: 2,
    })
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('关联内容')
    expect(t).toContain('Attention Is All You Need')
    // title 为 null 时回退展示 job_id
    expect(t).toContain('j2')
    expect(w.findAll('.list .row')).toHaveLength(2)
  })

  it('点击列表项跳转到内容详情', async () => {
    api.get.mockResolvedValue({
      jobs: [{ job_id: 'j1', title: 'X', content_type: 'document', document_kind: 'research_paper', status: 'done', created_at: '2026-01-01' }],
      total: 1,
    })
    const w = mountView()
    await flushPromises()
    await w.find('.list .row').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/j1')
  })

  it('加载失败渲染错误态并可重试', async () => {
    api.get.mockRejectedValueOnce(new Error('加载主题内容失败'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('加载主题内容失败')
    api.get.mockResolvedValueOnce({ jobs: [], total: 0 })
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('这个主题还没有内容')
  })

  it('点击 domain 面包屑跳转知识库；点击查看概念跳转概念页', async () => {
    api.get.mockResolvedValue({ jobs: [], total: 0 })
    const w = mountView()
    await flushPromises()
    await w.find('.term-link').trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/ml')
    const concept = w.findAll('button').find((b) => b.text().includes('查看概念'))!
    await concept.trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/ml/concepts/transformer')
  })
})
