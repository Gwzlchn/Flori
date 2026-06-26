import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// RadarView 直接走 useApi.get('/radar') + useApi.post('/digest')。mock useApi 与 vue-router。
const route = { params: { domain: 'finance' }, query: {} }
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push, replace: vi.fn() }),
}))

const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import RadarView from './RadarView.vue'

const RADAR = {
  rising_concepts: [
    { term: '量化交易', recent: 3, prior: 1, delta: 2 },
    { term: '高频量化', recent: 1, prior: 0, delta: 1 },
  ],
  new_concepts: [
    { term: 'JEPQ', definition: '主动型高股息 ETF', first_seen: '2026-06-22T00:00:00+00:00' },
  ],
  recent_jobs: [
    { job_id: 'r1', title: '量化交易入门', published_at: '2026-06-22T00:00:00+00:00', content_type: 'video' },
    { job_id: 'r2', title: '高频量化 vs 散户', published_at: '2026-06-21T00:00:00+00:00', content_type: 'video' },
  ],
  top_recent_concepts: [
    { term: '量化交易', recent: 3 },
    { term: '高频量化', recent: 1 },
  ],
  window: { days: 7, since: '2026-06-20T00:00:00+00:00', until: '2026-06-26T00:00:00+00:00' },
}

function mountView() {
  return mount(RadarView, {
    global: {
      // MarkdownViewer 内部用 vue-router/markdown-it,与本视图测试无关 → stub。
      stubs: { MarkdownViewer: { props: ['content'], template: '<div class="md-stub">{{ content }}</div>' } },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  route.params = { domain: 'finance' }
})

describe('RadarView', () => {
  it('挂载即按 domain 拉取雷达数据', async () => {
    api.get.mockResolvedValue(RADAR)
    mountView()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/domains/finance/radar?window_days=7')
  })

  it('渲染窗口摘要(新增/新概念计数) + 各板块', async () => {
    api.get.mockResolvedValue(RADAR)
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('本周知识雷达')
    expect(t).toContain('新增 2 篇')
    expect(t).toContain('新概念 1')
    // 飙升 + delta
    expect(t).toContain('量化交易')
    expect(t).toContain('+2')
    // 新出现 + 定义
    expect(t).toContain('JEPQ')
    expect(t).toContain('主动型高股息 ETF')
    // 热点
    expect(t).toContain('高频量化')
    // 最近内容
    expect(t).toContain('量化交易入门')
  })

  it('空数据渲染空态', async () => {
    api.get.mockResolvedValue({
      rising_concepts: [], new_concepts: [], recent_jobs: [], top_recent_concepts: [],
      window: { days: 7, since: '2026-06-20T00:00:00+00:00', until: '2026-06-26T00:00:00+00:00' },
    })
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('本周这个知识库还没有新动静')
  })

  it('点击「生成本周摘要」走 POST digest 并渲染 markdown', async () => {
    api.get.mockResolvedValue(RADAR)
    api.post.mockResolvedValue({ markdown: '## 本周摘要\n量化交易是焦点。', window: RADAR.window })
    const w = mountView()
    await flushPromises()
    const btn = w.findAll('button').find((b) => b.text().includes('生成本周摘要'))!
    expect(btn).toBeTruthy()
    await btn.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/domains/finance/digest?window_days=7')
    expect(w.text()).toContain('本周摘要')
    expect(w.text()).toContain('量化交易是焦点')
  })

  it('点击概念跳转概念页;点击最近内容跳转内容详情', async () => {
    api.get.mockResolvedValue(RADAR)
    const w = mountView()
    await flushPromises()
    // 第一个飙升行 → 概念页(term 经 encodeURIComponent,与全站约定一致)
    await w.find('.rising-row').trigger('click')
    expect(push).toHaveBeenCalledWith(`/kb/finance/concepts/${encodeURIComponent('量化交易')}`)
    // 最近内容行(非概念行)→ 内容详情
    const jobRow = w.findAll('.list .row').find((r) => r.text().includes('量化交易入门'))!
    await jobRow.trigger('click')
    expect(push).toHaveBeenCalledWith('/content/r1')
  })

  it('加载失败渲染错误态并可重试', async () => {
    api.get.mockRejectedValueOnce(new Error('加载知识雷达失败'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('加载知识雷达失败')
    api.get.mockResolvedValueOnce(RADAR)
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('量化交易入门')
  })

  it('摘要生成失败渲染错误文案', async () => {
    api.get.mockResolvedValue(RADAR)
    api.post.mockRejectedValueOnce(new Error('生成摘要失败'))
    const w = mountView()
    await flushPromises()
    const btn = w.findAll('button').find((b) => b.text().includes('生成本周摘要'))!
    await btn.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('生成摘要失败')
  })
})
