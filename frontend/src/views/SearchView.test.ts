import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// SearchView 用 useRouter(跳详情) + useApi(打 /api/search)。两者按 view 内的导入路径精确 mock。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

const api = {
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  upload: vi.fn(),
  getText: vi.fn(),
}
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import SearchView from './SearchView.vue'
import { installSourceCatalog } from '../constants/sources'

function makeItem(over: Record<string, any> = {}) {
  return {
    job_id: 'job-1',
    title: '深度学习基础',
    note_type: 'smart',
    snippet: '一段关于<mark>神经网络</mark>的摘要',
    content_type: 'video',
    document_kind: null,
    domain: 'ai',
    collection_id: null,
    canonical_evidence: [{
      evidence_id: `ce_${'2'.repeat(64)}`, status: 'valid', reason: null,
      job_id: 'job-1', note_type: 'smart', chunk_id: 'job-1:smart:0', section: '第一节',
      evidence_fingerprint: 'a'.repeat(64), source_fingerprint: 'b'.repeat(64),
      locator: { kind: 'text', exact: '神经网络', prefix: '关于', suffix: '的摘要', dom_path: null },
      link: { kind: 'text', href: `/api/evidence/ce_${'2'.repeat(64)}/open`, label: '第一节' },
      validated_at: '2026-07-14T14:00:00Z',
    }],
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  installSourceCatalog({
    content_types: [
      { type: 'video', label: '视频', pipeline: 'video', upload_extensions: [] },
      { type: 'document', label: '文档', pipeline: 'document', upload_extensions: ['.pdf'] },
      { type: 'audio', label: '播客', pipeline: 'audio', upload_extensions: [] },
    ],
    document_kinds: [
      { kind: 'research_paper', label: '论文', description: '', note_profile: 'paper', review_profile: 'paper' },
      { kind: 'article', label: '文章', description: '', note_profile: 'article', review_profile: 'article' },
      { kind: 'whitepaper', label: '白皮书', description: '', note_profile: 'whitepaper', review_profile: 'whitepaper' },
    ],
    source_profiles: [], job_sources: [], subscription_sources: [],
  } as any)
})

afterEach(() => {
  vi.useRealTimers()
})

// 触发输入 + 跑完 300ms 防抖 + flush 微任务,统一封装。
async function typeAndSettle(w: any, value: string) {
  await w.find('input[placeholder^="搜索"]').setValue(value)
  await vi.advanceTimersByTimeAsync(300)
  await flushPromises()
}

describe('SearchView 初始/短词态', () => {
  it('未搜索时显示初始提示，不打 API', async () => {
    const w = mount(SearchView)
    expect(w.text()).toContain('输入关键词开始搜索')
    expect(api.get).not.toHaveBeenCalled()
  })

  it('输入 <3 字符显示字数不足提示且不打 API', async () => {
    const w = mount(SearchView)
    await typeAndSettle(w, 'ab')
    expect(w.text()).toContain('输入需 ≥ 3 字才会搜索')
    expect(api.get).not.toHaveBeenCalled()
  })
})

describe('SearchView 搜索流程', () => {
  it('≥3 字符触发 GET /api/search 并渲染结果列表', async () => {
    api.get.mockResolvedValue({ total: 1, items: [makeItem()] })
    const w = mount(SearchView)
    await typeAndSettle(w, 'neural')

    expect(api.get).toHaveBeenCalledTimes(1)
    const url = api.get.mock.calls[0][0] as string
    expect(url).toMatch(/^\/api\/search\?/)
    expect(url).toContain('q=neural')

    const t = w.text()
    expect(t).toContain('共 1 条结果')
    expect(t).toContain('深度学习基础')
    expect(t).toContain('智能笔记') // note_type=smart 映射
  })

  it('buildQuery 把 类型/知识库/集合 过滤拼进 query', async () => {
    api.get.mockResolvedValue({ total: 0, items: [] })
    const w = mount(SearchView)
    await w.findAll('select')[0].setValue('document')
    await w.findAll('select')[1].setValue('research_paper')
    await w.findAll('input.input')[0].setValue('  ml  ') // domain,前后空格应被 trim
    await w.findAll('input.input')[1].setValue('col-9')  // collection_id
    await typeAndSettle(w, 'transformer')

    const calls = api.get.mock.calls
    const url = calls[calls.length - 1][0] as string
    expect(url).toContain('q=transformer')
    expect(url).toContain('content_type=document')
    expect(url).toContain('document_kind=research_paper')
    expect(url).toContain('domain=ml')      // 已 trim
    expect(url).not.toContain('domain=+')
    expect(url).toContain('collection_id=col-9')
  })

  it('结果为空时显示空状态', async () => {
    api.get.mockResolvedValue({ total: 0, items: [] })
    const w = mount(SearchView)
    await typeAndSettle(w, 'zzz')
    expect(w.text()).toContain('没有匹配的笔记')
  })

  it('API 抛错时显示错误态与重试按钮', async () => {
    api.get.mockRejectedValue(new Error('搜索后端炸了'))
    const w = mount(SearchView)
    await typeAndSettle(w, 'boom')
    expect(w.text()).toContain('搜索后端炸了')
    expect(w.findAll('button').some((b) => b.text() === '重试')).toBe(true)
  })
})

describe('SearchView 交互', () => {
  it('卡片保留普通 job 导航,定位链接只使用 resolver 投影且不冒泡', async () => {
    const item = makeItem({ job_id: 'a b/c' })
    api.get.mockResolvedValue({ total: 1, items: [item] })
    const w = mount(SearchView)
    await typeAndSettle(w, 'click')
    expect(w.get('.list .card a.evidence-locator').attributes('href')).toBe(item.canonical_evidence[0].link.href)
    w.get('.list .card a.evidence-locator').element.addEventListener('click', (event) => event.preventDefault())
    await w.get('.list .card a.evidence-locator').trigger('click')
    expect(push).not.toHaveBeenCalled()
    await w.get('.list .card').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/a%20b%2Fc')
  })

  it('safeSnippet 转义正文但保留 <mark> 高亮（杜绝注入）', async () => {
    api.get.mockResolvedValue({
      total: 1,
      items: [makeItem({ snippet: '<img src=x onerror=1><mark>hit</mark>' })],
    })
    const w = mount(SearchView)
    await typeAndSettle(w, 'inject')
    const html = w.find('.search-snippet').html()
    expect(html).toContain('<mark>hit</mark>')   // 高亮保留
    expect(html).not.toContain('<img')           // 危险标签被转义
    expect(html).toContain('&lt;img')            // 转义为实体
  })

  it('stale 证据禁用深链但仍保留普通 job 导航', async () => {
    const base = makeItem().canonical_evidence[0]
    api.get.mockResolvedValue({
      total: 1,
      items: [makeItem({ canonical_evidence: [{ ...base, status: 'stale', reason: 'source_changed', locator: null, link: null }] })],
    })
    const w = mount(SearchView)
    await typeAndSettle(w, 'stale')
    const unavailable = w.get('.evidence-unavailable')
    expect(unavailable.text()).toContain('证据已过期')
    await unavailable.trigger('click')
    expect(push).not.toHaveBeenCalled()
    await w.find('.list .card').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/job-1')
  })
})
