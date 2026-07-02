import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref, reactive } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import type { JobDetail, JobConcept } from '../types'

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
    meta: {},
    steps: [
      { name: 'download', label: '下载', status: 'done', started_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:01:00Z', duration_sec: 60, meta: {}, error: null },
    ],
    ...over,
  } as JobDetail
}

const showToast = vi.fn()
function mountView() {
  return mount(JobDetailView, {
    global: {
      provide: { showToast },
      // 子组件 stub:非本测目标,避免其内部依赖
      stubs: { MarkdownViewer: true, StepWorkbench: true },
    },
  })
}

beforeEach(() => {
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

  it('文章无智能笔记也无译文时隐藏变体切换,显示「原文(未生成智能笔记)」', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'done', content_type: 'article' }))
    api.get.mockResolvedValue([])         // note-versions 空 → 无智能笔记
    // 只有 original.md 有内容;translated.md 无(否则译文变体合理出现,seg 不该隐藏)
    api.getText.mockImplementation(async (url: string) =>
      url.includes('original.md') ? '# 原文\n正文' : Promise.reject({ status: 404 }))
    const w = mountView()
    await flushPromises()
    expect(w.find('.seg').exists()).toBe(false)   // 无第二变体 → 切换器隐藏
    expect(w.text()).toContain('原文')
    expect(w.text()).not.toContain('智能版')
  })

  it('有译文时笔记内出现「译文」变体并可切换(不再是顶层 tab)', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ status: 'done', content_type: 'article' }))
    api.get.mockResolvedValue([])
    api.getText.mockImplementation(async (url: string) =>
      url.includes('translated.md') ? '# 译文内容' : '# 原文内容')
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
    expect(w.find('markdown-viewer-stub').attributes('content')).toContain('译文内容')
    // 切回原文,切换器仍在(不消失)
    const back = seg.findAll('button').find(b => b.text() === '原文')
    await back!.trigger('click')
    await flushPromises()
    expect(w.find('.seg').exists()).toBe(true)
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
    const retry = w.findAll('button').find(b => b.text().trim() === '重试')
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

describe('JobDetailView 论文原文 = 内嵌 PDF', () => {
  it('paper 无智能笔记时,笔记 tab 渲染 PDF iframe(不走 MD 文本)', async () => {
    fetchDetail.mockResolvedValue(makeDetail({ content_type: 'paper', status: 'processing' }))
    const w = mountView()
    await flushPromises()
    const frame = w.find('iframe.pdf-frame')
    expect(frame.exists()).toBe(true)
    expect(frame.attributes('src')).toContain('input%2Fsource.pdf')
    // 笔记正文区不再渲染 MD(页面他处的 MarkdownViewer 实例如摘要不在此断言范围)
    expect(w.find('.notes-wrap').exists()).toBe(false)
  })
})

describe('JobDetailView 切 job 重置(跨 job 串台回归)', () => {
  it('切 job 后笔记重新加载,不残留上一个 job 的原文', async () => {
    // 复现实测事故:A(文章,已看原文)→ 切到 B,notesInit 不复位则 ensureNotes no-op,
    // B 标题下挂着 A 的原文。断言切换后 MarkdownViewer 拿到 B 的内容。
    fetchDetail.mockImplementation(async (id: string) =>
      makeDetail({ job_id: id, content_type: 'article', status: 'done', title: `title-${id}` }))
    api.getText.mockImplementation(async (url: string) =>
      url.includes('job_A') ? 'A 的原文内容' : 'B 的原文内容')

    routeParams.id = 'job_A'
    const w = mountView()
    await flushPromises()
    // done 态默认落笔记 tab;文章无智能笔记 → 显示原文(机械变体)
    expect(w.find('markdown-viewer-stub').attributes('content')).toContain('A 的原文内容')

    routeParams.id = 'job_B'
    await flushPromises()
    const content = w.find('markdown-viewer-stub').attributes('content')
    expect(content).toContain('B 的原文内容')
    expect(content).not.toContain('A 的原文内容')
  })
})
