import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref, reactive } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import type { JobDetail, JobConcept } from '../types'
import { installSourceCatalog } from '../constants/sources'

// 路由 mock: route.params.id 决定加载哪个 job;push 用于跳转(删除/概念)。
// params 用 reactive:测「切 job」时改 routeParams.id,组件的 jobId 才会响应式变化。
const push = vi.fn()
const routeParams = reactive({ id: 'job_BV1abc' })
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: routeParams, query: {} }),
}))

// job store mock: 直接控制各 action 返回,避免真实 action 走 useApi。
const fetchDetail = vi.fn()
const fetchConcepts = vi.fn()
const retryJob = vi.fn()
const rerunJob = vi.fn()
const deleteJob = vi.fn()
vi.mock('../stores/jobs', () => ({
  useJobStore: () => ({ fetchDetail, fetchConcepts, retryJob, rerunJob, deleteJob }),
}))

const setCrumbs = vi.fn()
vi.mock('../stores/global', () => ({
  useGlobalStore: () => ({ setCrumbs }),
}))

// useApi mock: 笔记/版本/provider/评审/概念等附属请求都走它(组件直接调 api.get/getText/post)。
const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

// useJobWs mock: 返回可控响应式 refs(组件解构 steps/jobStatus/connected/setInitialSteps),不连真 WS。
const wsSteps = ref<any[]>([])
const wsJobStatus = ref('processing')
const wsConnected = ref(false)
const setInitialSteps = vi.fn((s: any[]) => { wsSteps.value = s })
vi.mock('../composables/useJobWs', () => ({
  useJobWs: () => ({
    steps: wsSteps,
    jobStatus: wsJobStatus,
    connected: wsConnected,
    setInitialSteps,
  }),
}))

import JobDetailView from './JobDetailView.vue'

function makeDetail(over: Partial<JobDetail> = {}): JobDetail {
  return {
    job_id: 'job_BV1abc',
    content_type: 'video',
    document_kind: null,
    pipeline: 'video',
    status: 'done',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: null,
    published_at: '2026-01-01T00:00:00Z',
    title: '深入理解 Transformer',
    progress_pct: 100,
    source: 'bilibili',
    domain: 'AI',
    collection_id: null,
    collection_name: null,
    url: 'https://example.com/v',
    media: {},
    artifacts: [],
    meta: {},
    steps: [
      { name: 'download', label: '下载', status: 'done', started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:01:00Z', duration_sec: 60, meta: {}, error: null },
    ],
    ...over,
  } as JobDetail
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

const showToast = vi.fn()
function mountView() {
  return mount(JobDetailView, {
    global: {
      provide: { showToast },
      // 子组件 stub:非本测目标,避免其内部依赖
      stubs: {
        MarkdownViewer: true,
        StepWorkbench: true,
        DocumentPdfViewer: {
          name: 'DocumentPdfViewer',
          props: ['url', 'page', 'bboxes'],
          template: '<div class="pdf-viewer-stub" />',
        },
      },
    },
  })
}

beforeEach(() => {
  installSourceCatalog({
    content_types: [{ type: 'video', label: '视频', upload_extensions: ['.mp4'] }],
    job_sources: [{ type: 'bilibili', label: 'Bilibili' }],
    subscription_sources: [],
  })
  vi.clearAllMocks()
  routeParams.id = 'job_BV1abc'
  // 复位 ws refs
  wsSteps.value = []
  wsJobStatus.value = 'processing'
  wsConnected.value = false
  // 默认:详情成功、附属请求空,避免懒加载抛错
  fetchDetail.mockResolvedValue(makeDetail())
  fetchConcepts.mockResolvedValue([])
  api.get.mockResolvedValue([])
  api.getText.mockResolvedValue('')
  api.post.mockResolvedValue({})
  api.del.mockResolvedValue(undefined)
})

describe('JobDetailView 加载/错误态', () => {
  it('初始渲染加载态(loading)', () => {
    fetchDetail.mockReturnValue(new Promise(() => {}))  // 永不 resolve → 保持 loading
    const w = mountView()
    expect(w.text()).toContain('加载中')
  })

  it('404 显示「内容不存在或已删除」并提供返回按钮', async () => {
    fetchDetail.mockRejectedValueOnce(Object.assign(new Error('nf'), { status: 404 }))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('内容不存在或已删除')
    expect(w.text()).toContain('返回所有来源')
  })

  it('非 404 错误显示错误消息', async () => {
    fetchDetail.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('boom')
  })

  it('错误态点「返回所有来源」触发 router.push(/content)', async () => {
    fetchDetail.mockRejectedValueOnce(Object.assign(new Error('nf'), { status: 404 }))
    const w = mountView()
    await flushPromises()
    const back = w.findAll('button').find(b => b.text().includes('返回所有来源'))
    expect(back).toBeTruthy()
    await back!.trigger('click')
    expect(push).toHaveBeenCalledWith('/content')
  })
})

describe('JobDetailView 头部渲染', () => {
  it('加载成功渲染标题/来源/领域/BV 号/类型', async () => {
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('深入理解 Transformer')
    expect(t).toContain('Bilibili')   // sourceLabel 映射
    expect(t).toContain('AI')         // domain
    expect(t).toContain('BV1abc')     // 从 jobId 解析的 BV 号
    expect(t).toContain('视频')        // CONTENT_TYPE_LABELS[video]
  })

  it('title 为空时回退展示 job_id', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ title: null }))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('job_BV1abc')
  })

  it('详情就绪后 setInitialSteps 收到 steps,并写面包屑', async () => {
    mountView()
    await flushPromises()
    expect(setInitialSteps).toHaveBeenCalledTimes(1)
    expect(setInitialSteps.mock.calls[0][0]).toHaveLength(1)
    expect(setCrumbs).toHaveBeenCalled()
  })
})

describe('JobDetailView tab 默认与切换', () => {
  it('done 态默认落「笔记」tab', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'done' }))
    const w = mountView()
    await flushPromises()
    const onBtn = w.find('.tabs').findAll('button').find(b => b.classes().includes('on'))
    expect(onBtn?.text()).toContain('笔记')
  })

  it('未完成态也默认落「笔记」tab(原文不等 AI 步,点开即可读)', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'processing' }))
    const w = mountView()
    await flushPromises()
    const onBtn = w.find('.tabs').findAll('button').find(b => b.classes().includes('on'))
    expect(onBtn?.text()).toContain('笔记')
  })

  it('点「元信息」tab 切换并渲染元信息表格', async () => {
    const w = mountView()
    await flushPromises()
    const infoBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('元信息'))
    await infoBtn!.trigger('click')
    await flushPromises()
    const t = w.text()
    expect(t).toContain('元信息')
    expect(t).toContain('删除内容')   // 元信息 tab 底部按钮
    expect(t).toContain('未归集合')   // collection_name 为 null 的回退文案
  })

  it('Document 图表 tab 展示图表分组与数量', async () => {
    fetchDetail.mockResolvedValue(makeDetail({
      content_type: 'document', pipeline: 'document', document_kind: 'research_paper',
      artifacts: ['intermediate/document.json'],
    }))
    api.get.mockImplementation(async (url: string) => {
      if (url.includes('intermediate%2Fdocument.json')) {
        return {
          blocks: [], assets: [], tables: [],
          figures: [{ figure_id: 'fig1', label: '图 1', caption: '测试图表', order: 0, media: [] }],
        }
      }
      return []
    })
    const w = mountView()
    await flushPromises()
    const figuresBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('图表'))
    await figuresBtn!.trigger('click')
    await flushPromises()

    const panel = w.find('.document-visuals')
    expect(panel.exists()).toBe(true)
    expect(panel.find('.document-visuals-head').text()).toContain('图 1 · 表 0')
    expect(panel.text()).toContain('测试图表')
  })
})

describe('JobDetailView 概念 tab', () => {
  it('空概念列表显示空态文案', async () => {
    fetchConcepts.mockResolvedValue([])
    const w = mountView()
    await flushPromises()
    const conBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('概念'))
    await conBtn!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('这条内容暂未关联任何概念')
  })

  it('概念加载失败(非 404)显示错误并可重试', async () => {
    fetchConcepts.mockRejectedValue(Object.assign(new Error('网络炸了'), { status: 500 }))
    const w = mountView()
    await flushPromises()
    const conBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('概念'))
    await conBtn!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('网络炸了')
  })

  it('有概念时渲染概念项并支持点进跳转', async () => {
    const concept: JobConcept = {
      domain: 'AI', term: '注意力机制', definition: '一种加权机制',
      occurrences: [{ job_id: 'x', content_type: 'video', location: null }],
      related: [], status: 'accepted', is_topic: true, definition_locked: false, created_at: '2026-01-01',
      job_occurrences: [{ job_id: 'job_BV1abc', content_type: 'video', location: '03:21' }],
    }
    fetchConcepts.mockResolvedValue([concept])
    const w = mountView()
    await flushPromises()
    const conBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('概念'))
    await conBtn!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('注意力机制')
    expect(w.text()).toContain('主题概念')   // is_topic
    const item = w.find('.concept')
    expect(item.exists()).toBe(true)
    await item.trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/AI/concepts/%E6%B3%A8%E6%84%8F%E5%8A%9B%E6%9C%BA%E5%88%B6')
  })
})

describe('JobDetailView 笔记 tab', () => {
  it('把当前智能笔记的服务端安全定位投影传给正文组件', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'done' }))
    const canonical = {
      evidence_id: `ce_${'1'.repeat(64)}`, status: 'valid', reason: null,
      job_id: 'job_BV1abc', note_type: 'smart', chunk_id: 'job_BV1abc:smart:0',
      section: '概览', evidence_fingerprint: 'a'.repeat(64), source_fingerprint: 'b'.repeat(64),
      locator: { kind: 'media', start_ms: 1000, end_ms: 2000 },
      link: { kind: 'media', href: '/api/jobs/job_BV1abc/media?path=input.mp4#t=1', label: '跳到 00:01' },
      validated_at: '2026-07-14T14:00:00Z',
    }
    api.get.mockImplementation((url: string) => {
      if (url.includes('note-versions')) return Promise.resolve({ versions: [{
        provider: 'p', model: 'm', version: '20260101-000000', file: 'f.md',
        review_file: null, overall: 4,
      }] })
      if (url.includes('/api/evidence/jobs/job_BV1abc?note_type=smart')) {
        return Promise.resolve({ items: [canonical] })
      }
      return Promise.resolve([])
    })
    api.getText.mockResolvedValue('# 智能笔记')

    const w = mountView()
    await flushPromises()
    expect(w.findComponent({ name: 'MarkdownViewer' }).props('canonicalEvidence')).toEqual([canonical])
  })

  it('笔记 404 显示「笔记尚未生成」', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'done' }))
    api.getText.mockRejectedValue(Object.assign(new Error('nf'), { status: 404 }))
    const w = mountView()
    await flushPromises()  // done 默认即笔记 tab,ensureNotes 已触发
    expect(w.text()).toContain('笔记尚未生成')
  })

  it('有智能笔记时显示 智能版/机械版分段开关', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'done' }))
    // 有 note-versions → hasSmartNote=true → seg 显示
    api.get.mockImplementation((url: string) =>
      url.includes('note-versions')
        ? Promise.resolve({ versions: [{ provider: 'p', model: 'm', version: '20260101-000000', file: 'f.md', review_file: null, overall: 4 }] })
        : Promise.resolve([]))
    const w = mountView()
    await flushPromises()
    const seg = w.find('.seg')
    expect(seg.exists()).toBe(true)
    expect(seg.text()).toContain('智能版')
    expect(seg.text()).toContain('机械版')
  })

  it('Document 无智能笔记也无译文时只显示 HTML 原文', async () => {
    fetchDetail.mockResolvedValue(makeDetail({
      status: 'done', content_type: 'document', pipeline: 'document',
      document_kind: 'article', artifacts: ['input/source.html'],
    }))
    api.get.mockResolvedValue([])
    const w = mountView()
    await flushPromises()
    expect(w.find('.seg').findAll('button').map(button => button.text())).toEqual(['原文'])
    expect(w.find('iframe.document-reader-frame').attributes('src')).toBe('/api/jobs/job_BV1abc/document/source')
    expect(w.text()).not.toContain('原文 PDF')
    expect(w.text()).not.toContain('智能版')
  })

  it('Document 有译文时使用隔离 HTML 阅读面切换', async () => {
    fetchDetail.mockResolvedValue(makeDetail({
      status: 'done', content_type: 'document', pipeline: 'document', document_kind: 'article',
      artifacts: ['input/source.html', 'output/translated.html'],
    }))
    api.get.mockResolvedValue([])
    const w = mountView()
    await flushPromises()
    const tabs = w.find('.tabs')
    expect(tabs.text()).not.toContain('译文')      // 顶层 tab 无译文
    const seg = w.find('.seg')
    expect(seg.exists()).toBe(true)
    expect(seg.text()).toContain('译文')
    const btn = seg.findAll('button').find(b => b.text() === '译文')
    await btn!.trigger('click')
    await flushPromises()
    expect(w.find('iframe.document-reader-frame').attributes('src')).toBe('/api/jobs/job_BV1abc/document/translation')
    const back = seg.findAll('button').find(b => b.text() === '原文')
    await back!.trigger('click')
    await flushPromises()
    expect(w.find('.seg').exists()).toBe(true)
  })

  it.each([
    ['reliable', '可靠'],
    ['unreliable', '不可靠,仅供诊断'],
    ['legacy_unverified', '旧版未验证'],
  ])('评审三态 %s 明确展示', async (state, label) => {
    api.get.mockImplementation((url: string) => {
      if (url.includes('note-versions')) return Promise.resolve({ versions: [{
        provider: 'p', model: 'm', version: '20260101-000000', file: 'f.md',
        review_file: 'output/versions/review_x.json', overall: state === 'reliable' ? 4.5 : null,
      }] })
      if (url.includes('/review')) return Promise.resolve({
        reliability_state: state, review_reliable: state === 'reliable',
        overall: state === 'reliable' ? 4.5 : null, reliability_reasons: ['parse_fallback'],
        key_terms: state === 'reliable' ? [{ term: 'FTS', definition: '全文检索' }] : [],
        review_input: { sources: [{ label: 'E1', artifact: 'output/evidence/evidence-01.md' }] },
        issues: [{
          type: 'traceability', severity: 'warning', dimension: 'accuracy',
          claim: '罚款金额需核验', message: '补充可复核定位',
          evidence_status: 'supported', locator: { source: 'E1', quote: '罚款 123 万元' },
        }],
      })
      return Promise.resolve([])
    })
    api.getText.mockResolvedValue('# 笔记')
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain(label)
    expect(w.text()).toContain('罚款金额需核验')
    if (state === 'reliable') {
      expect(w.text()).toContain('证据 E1')
      expect(w.find('a[href*="evidence-01.md"]').text()).toContain('查看来源')
    } else {
      expect(w.text()).not.toContain('证据 E1')
      expect(w.text()).not.toContain('罚款 123 万元')
      expect(w.find('a[href*="evidence-01.md"]').exists()).toBe(false)
      expect(w.text()).toContain('不会用于术语采纳')
    }
  })

  it.each([
    ['legacy_unverified', {
      reliability_state: 'legacy_unverified', review_reliable: false,
      overall: 4.9, score_keys: ['accuracy'], accuracy: 4.8,
      key_terms: [{ term: 'FORGED_TERM', definition: '不应展示' }],
      reliability_reasons: 'forged', missing_concepts: true, top3_improvements: {},
      review_input: { sources: true },
      issues: [{
        dimension: 'accuracy', claim: '金额需核验', message: '定位未验证',
        evidence_status: 'supported', locator: { source: 'E1', quote: 'FORGED_LOCATOR' },
      }],
    }],
    ['unreliable', {
      reliability_state: 'unreliable', review_reliable: false,
      overall: 4.9, score_keys: ['accuracy'], accuracy: 4.8,
      key_terms: [{ term: 'FORGED_TERM', definition: '不应展示' }],
      reliability_reasons: ['artifact_tampered', { nested: true }],
      missing_concepts: 'forged', top3_improvements: 'forged',
      review_input: { sources: { label: 'E1', artifact: '../../secret' } },
      issues: [null, {
        dimension: 'accuracy', claim: '来源需核验', message: '来源未验证',
        evidence_status: 'supported', locator: { source: 'E1', quote: 'FORGED_LOCATOR' },
      }],
    }],
  ])('恶意 %s 评审集合响应 mount 不抛且不生成产物链接', async (state, response) => {
    api.get.mockImplementation((url: string) => {
      if (url.includes('note-versions')) return Promise.resolve({ versions: [{
        provider: 'p', model: 'm', version: '20260101-000000', file: 'f.md',
        review_file: 'output/versions/review_x.json', overall: null,
      }] })
      if (url.includes('/review')) return Promise.resolve(response)
      return Promise.resolve([])
    })
    api.getText.mockResolvedValue('# 笔记')

    const w = mountView()
    await flushPromises()

    expect(w.text()).toContain(state === 'unreliable' ? '不可靠,仅供诊断' : '旧版未验证')
    expect(w.text()).toContain('未验证')
    expect(w.text()).not.toContain('4.9 / 5')
    expect(w.text()).not.toContain('FORGED_TERM')
    expect(w.text()).not.toContain('FORGED_LOCATOR')
    expect(w.text()).not.toContain('证据 E1')
    expect(w.findAll('a').some(a => a.text().includes('查看来源'))).toBe(false)
    expect(w.find('a[href*="secret"]').exists()).toBe(false)
  })

  it('可靠状态与布尔门不一致时不展示可信评分、术语或定位', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.includes('note-versions')) return Promise.resolve({ versions: [{
        provider: 'p', model: 'm', version: '20260101-000000', file: 'f.md',
        review_file: 'output/versions/review_x.json', overall: 4.9,
      }] })
      if (url.includes('/review')) return Promise.resolve({
        reliability_state: 'reliable', review_reliable: false,
        overall: 4.9, accuracy: 4.8,
        key_terms: [{ term: 'FORGED_TERM', definition: '不应展示' }],
        issues: [{
          dimension: 'accuracy', claim: '仍可诊断', evidence_status: 'supported',
          locator: { source: 'E1', quote: 'FORGED_LOCATOR' },
        }],
      })
      return Promise.resolve([])
    })
    api.getText.mockResolvedValue('# 笔记')

    const w = mountView()
    await flushPromises()

    expect(w.text()).toContain('旧版未验证')
    expect(w.text()).toContain('仍可诊断')
    expect(w.text()).not.toContain('4.9 / 5')
    expect(w.text()).not.toContain('FORGED_TERM')
    expect(w.text()).not.toContain('FORGED_LOCATOR')
  })

  it('恶意 evidence 集合响应 mount 不抛且危险 URL/产物均不可点击', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.includes('/evidence')) return Promise.resolve({
        manifest_state: 'invalid', reliability_state: 'unreliable',
        manifest_errors: 'forged',
        evidence: [{
          id: 'E1', title: '伪造来源', link_safe: true, verification_state: 'verified',
          eligible: true, confidence: 'high', source_tier: '一手官方',
          final_url: 'https://evil.example/forged', artifact: 'output/evidence/forged.md',
          matches: 'forged', eligibility_reasons: { nested: true },
          verification_reasons: true,
        }],
      })
      if (url.includes('note-versions')) return Promise.resolve({ versions: [] })
      return Promise.resolve([])
    })

    const w = mountView()
    await flushPromises()
    const evidenceTab = w.find('.tabs').findAll('button').find(b => b.text().includes('权威来源'))
    await evidenceTab!.trigger('click')
    await flushPromises()

    expect(w.text()).toContain('证据清单校验失败')
    expect(w.text()).toContain('链接不可用')
    expect(w.find('a[href*="evil.example"]').exists()).toBe(false)
    expect(w.find('a[href*="forged.md"]').exists()).toBe(false)
  })

  it('旧版或低置信证据不渲染外链', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.includes('/evidence')) return Promise.resolve({
        reliability_state: 'legacy_unverified',
        evidence: [{ id: 'E1', title: '旧来源', link_safe: false, final_url: null }],
      })
      if (url.includes('note-versions')) return Promise.resolve({ versions: [] })
      return Promise.resolve([])
    })
    const w = mountView()
    await flushPromises()
    const evidenceTab = w.find('.tabs').findAll('button').find(b => b.text().includes('权威来源'))
    await evidenceTab!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('旧版证据未验证')
    expect(w.find('a[href="javascript:alert(1)"]').exists()).toBe(false)
    expect(w.text()).toContain('链接不可用')
  })
})

describe('JobDetailView 流水线 tab 操作', () => {
  it('failed 态在流水线 tab 显示重试按钮并调用 store.retryJob', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'failed' }))
    retryJob.mockResolvedValue(undefined)
    const w = mountView()
    await flushPromises()
    // jobStatus 由 fetchDetail 写入 ws ref;保险起见对齐(读它决定按钮可见)
    wsJobStatus.value = 'failed'
    await flushPromises()
    const retry = w.findAll('button').find(b => b.text().trim() === '从失败处继续')
    expect(retry).toBeTruthy()
    await retry!.trigger('click')
    await flushPromises()
    expect(retryJob).toHaveBeenCalledWith('job_BV1abc')
  })
})

describe('JobDetailView 删除流程', () => {
  it('元信息 tab 点删除弹确认框,确认后调用 store.deleteJob 并跳转', async () => {
    deleteJob.mockResolvedValue(undefined)
    const w = mountView()
    await flushPromises()
    const infoBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('元信息'))
    await infoBtn!.trigger('click')
    await flushPromises()
    const delBtn = w.findAll('button').find(b => b.text().includes('删除内容'))
    await delBtn!.trigger('click')
    expect(w.text()).toContain('确定删除此内容及所有产物')
    // modal 内确认按钮文案为「删除」
    const confirm = w.find('.modal').findAll('button').find(b => b.text().trim() === '删除')
    await confirm!.trigger('click')
    await flushPromises()
    expect(deleteJob).toHaveBeenCalledWith('job_BV1abc')
    expect(push).toHaveBeenCalledWith('/content')
  })

  it('删除确认框可取消(不调用 deleteJob)', async () => {
    const w = mountView()
    await flushPromises()
    const infoBtn = w.find('.tabs').findAll('button').find(b => b.text().includes('元信息'))
    await infoBtn!.trigger('click')
    await flushPromises()
    const delBtn = w.findAll('button').find(b => b.text().includes('删除内容'))
    await delBtn!.trigger('click')
    const cancel = w.find('.modal').findAll('button').find(b => b.text().trim() === '取消')
    await cancel!.trigger('click')
    await flushPromises()
    expect(deleteJob).not.toHaveBeenCalled()
    expect(w.find('.modal').exists()).toBe(false)
  })
})

describe('JobDetailView Document 原文阅读面', () => {
  it('纯 PDF 文档无智能笔记时默认渲染独立 PDF 变体', async () => {
    fetchDetail.mockResolvedValue(makeDetail({
      content_type: 'document', pipeline: 'document', source_profile: 'digital_pdf', status: 'processing',
      document_kind: 'research_paper',
      artifacts: ['input/source.pdf'],
    }))
    const w = mountView()
    await flushPromises()
    expect(w.find('.seg').text()).toContain('原文 PDF')
    expect(w.find('.seg').findAll('button').map(b => b.text())).toEqual(['原文 PDF'])
    const viewer = w.findComponent({ name: 'DocumentPdfViewer' })
    expect(viewer.exists()).toBe(true)
    expect(viewer.props('url')).toContain('/media?')
    expect(viewer.props('url')).toContain('input%2Fsource.pdf')
    expect(w.find('.notes-wrap').exists()).toBe(false)
    expect(w.text()).toContain('PDF 保留论文原始公式、图表和版式')
    expect(w.find('.pdf-head a').attributes('target')).toBe('_blank')
  })

  it('学术 HTML 文档同时保留原文、译文和 PDF 版式原文', async () => {
    fetchDetail.mockResolvedValue(makeDetail({
      content_type: 'document', pipeline: 'document', source_profile: 'scholarly_html', status: 'failed',
      document_kind: 'research_paper',
      artifacts: ['input/source.html', 'input/source.pdf', 'output/translated.html'],
    }))

    const w = mountView()
    await flushPromises()
    const seg = w.find('.seg')
    expect(seg.text()).toContain('原文')
    expect(seg.text()).toContain('译文')
    expect(seg.text()).toContain('原文 PDF')
    expect(w.find('iframe.document-reader-frame').attributes('src')).toContain('/document/source')

    const translated = seg.findAll('button').find(b => b.text() === '译文')
    await translated!.trigger('click')
    await flushPromises()
    expect(w.find('iframe.document-reader-frame').attributes('src')).toContain('/document/translation')

    const pdf = seg.findAll('button').find(b => b.text() === '原文 PDF')
    await pdf!.trigger('click')
    await flushPromises()
    const viewer = w.findComponent({ name: 'DocumentPdfViewer' })
    expect(viewer.exists()).toBe(true)
    expect(viewer.props('url')).toBe('/api/jobs/job_BV1abc/media?path=input%2Fsource.pdf')
  })

  it('PDF 页码跳转切到 PDF 变体并定位目标页', async () => {
    fetchDetail.mockResolvedValue(makeDetail({
      content_type: 'document', pipeline: 'document', source_profile: 'scholarly_html', status: 'done',
      document_kind: 'research_paper', artifacts: ['input/source.html', 'input/source.pdf', 'output/translated.html'],
    }))
    const w = mountView()
    await flushPromises()
    const translated = w.find('.seg').findAll('button').find(b => b.text() === '译文')
    await translated!.trigger('click')
    await flushPromises()
    w.findComponent({ name: 'JobNotesPanel' }).vm.$emit('pdfPage', 4)
    await flushPromises()
    const viewer = w.findComponent({ name: 'DocumentPdfViewer' })
    expect(viewer.props('url')).toBe('/api/jobs/job_BV1abc/media?path=input%2Fsource.pdf')
    expect(viewer.props('page')).toBe(4)
  })
})

describe('JobDetailView 切 job 重置(跨 job 串台回归)', () => {
  it('切 job 后笔记重新加载,不残留上一个 job 的原文', async () => {
    // 复现实测事故:A(视频,已看机械笔记)→ 切到 B,notesInit 不复位则 ensureNotes no-op,
    // B 标题下挂着 A 的原文。断言切换后 MarkdownViewer 拿到 B 的内容。
    fetchDetail.mockImplementation(async (id: string) =>
      makeDetail({ job_id: id, content_type: 'video', pipeline: 'video', status: 'done', title: `title-${id}` }))
    api.getText.mockImplementation(async (url: string) =>
      url.includes('job_A') ? 'A 的原文内容' : 'B 的原文内容')

    routeParams.id = 'job_A'
    const w = mountView()
    await flushPromises()
    // done 态默认落笔记 tab;视频无智能笔记 → 显示机械变体。
    expect(w.find('markdown-viewer-stub').attributes('content')).toContain('A 的原文内容')

    routeParams.id = 'job_B'
    await flushPromises()
    const content = w.find('markdown-viewer-stub').attributes('content')
    expect(content).toContain('B 的原文内容')
    expect(content).not.toContain('A 的原文内容')
  }, 10000)

  it('A 详情迟到时不会覆盖已经完成的 B 路由', async () => {
    const pendingA = deferred<JobDetail>()
    fetchDetail.mockImplementation((id: string) => id === 'job_A'
      ? pendingA.promise
      : Promise.resolve(makeDetail({ job_id: id, title: 'B 的标题' })))

    routeParams.id = 'job_A'
    const wrapper = mountView()
    await flushPromises()
    routeParams.id = 'job_B'
    await flushPromises()
    expect(wrapper.text()).toContain('B 的标题')

    pendingA.resolve(makeDetail({ job_id: 'job_A', title: '迟到的 A 标题' }))
    await flushPromises()
    expect(wrapper.text()).toContain('B 的标题')
    expect(wrapper.text()).not.toContain('迟到的 A 标题')
  })

  it('WS 已进入终态后拒绝迟到 HTTP processing 快照降级', async () => {
    const pending = deferred<JobDetail>()
    fetchDetail.mockReturnValue(pending.promise)
    const wrapper = mountView()
    wsJobStatus.value = 'done'
    await flushPromises()
    pending.resolve(makeDetail({ status: 'processing' }))
    await flushPromises()
    expect(wrapper.findComponent({ name: 'StatusBadge' }).props('status')).toBe('done')
  })

  it('离页会中止在途详情请求并清除面包屑', async () => {
    fetchDetail.mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    await flushPromises()
    const signal = fetchDetail.mock.calls[0][1] as AbortSignal
    expect(signal.aborted).toBe(false)
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    expect(setCrumbs).toHaveBeenLastCalledWith(null)
  })
})
