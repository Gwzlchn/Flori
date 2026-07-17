import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

// router:固定 route.params.domain;共享 push 间谍断言跳转。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: { domain: 'tech' }, query: {} }),
}))

// useApi:真 Pinia + 假 useApi,让真 store.workspace 跑通并把数据写进视图本地 ref。
const get = vi.fn()
vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get, post: vi.fn(), del: vi.fn(), put: vi.fn(), upload: vi.fn(), getText: vi.fn() }),
}))

import DomainWorkspaceView from './DomainWorkspaceView.vue'

function ws(over: Partial<any> = {}) {
  return {
    domain: 'tech',
    stats: {
      collection_count: 2,
      job_count: 5,
      concept_count: 3,
      subscription_count: 1,
      last_active_at: null,
      display_name: '技术库',
      icon: null,
      color: null,
    },
    collections: [],
    recent_jobs: [],
    top_concepts: [],
    topics: [],
    suggested_count: 0,
    ...over,
  }
}

const STUBS = { ProfileEditor: true, ConceptTimeline: true, ConceptGraph: true, StatusBadge: true }

async function mountView() {
  const w = mount(DomainWorkspaceView, { global: { stubs: STUBS } })
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  push.mockReset()
})

describe('DomainWorkspaceView 头部与加载', () => {
  it('挂载时按 route.domain 拉取工作台数据', async () => {
    get.mockResolvedValue(ws())
    await mountView()
    expect(get).toHaveBeenCalledWith('/api/domains/tech')
  })

  it('渲染 display_name 与统计行', async () => {
    get.mockResolvedValue(ws())
    const w = await mountView()
    const t = w.text()
    expect(t).toContain('技术库')
    expect(t).toContain('2 集合')
    expect(t).toContain('5 内容')
    expect(t).toContain('3 概念')
  })

  it('错误时显示错误文案与重试按钮', async () => {
    get.mockRejectedValue(new Error('网络炸了'))
    const w = await mountView()
    expect(w.text()).toContain('网络炸了')
    expect(w.text()).toContain('重试')
  })
})

describe('DomainWorkspaceView tab 内容', () => {
  it('无集合且无内容时显示空态', async () => {
    get.mockResolvedValue(ws({ collections: [], recent_jobs: [] }))
    const w = await mountView()
    expect(w.text()).toContain('这个知识库还没有内容')
  })

  it('渲染集合分组及其归属内容', async () => {
    get.mockResolvedValue(
      ws({
        collections: [
          {
            id: 'c1', name: '我的集合', job_count: 1, is_subscription: false, source_id: null, sync_enabled: false,
            // 卡片读后端按集合返回的 recent
            recent: [
              {
                job_id: 'j1', content_type: 'video', status: 'done', created_at: '2026-01-01',
                title: '集合内视频', progress_pct: 100, source: 'youtube', domain: 'tech', collection_id: 'c1',
              },
            ],
          },
        ],
        recent_jobs: [],
      }),
    )
    const w = await mountView()
    const t = w.text()
    expect(t).toContain('我的集合')
    expect(t).toContain('集合内视频')
    expect(t).toContain('手动')
  })

  it('未归集合的内容进入「未归集合」分组', async () => {
    get.mockResolvedValue(
      ws({
        collections: [],
        recent_jobs: [
          {
            job_id: 'jx', content_type: 'document', document_kind: 'article',
            status: 'done', created_at: '2026-01-01',
            title: '游离文章', progress_pct: 100, source: null, domain: 'tech', collection_id: null,
          },
        ],
      }),
    )
    const w = await mountView()
    const t = w.text()
    expect(t).toContain('未归集合')
    expect(t).toContain('游离文章')
  })

  it('点击内容行跳转内容详情', async () => {
    get.mockResolvedValue(
      ws({
        recent_jobs: [
          {
            job_id: 'click-me', content_type: 'video', status: 'done', created_at: '2026-01-01',
            title: 'T', progress_pct: 100, source: null, domain: 'tech', collection_id: null,
          },
        ],
      }),
    )
    const w = await mountView()
    await w.find('.row').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/click-me')
  })
})

describe('DomainWorkspaceView 概念 tab', () => {
  it('切到概念 tab 后按佐证强度展示概念，空时显示暂无概念', async () => {
    get.mockResolvedValue(ws({ top_concepts: [] }))
    const w = await mountView()
    // tab 顺序:内容 / 概念 / 时间线 / 图谱;[1] 是概念
    const conceptTab = w.findAll('.tabs button')[1]
    await conceptTab.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('暂无概念')
  })

  it('有待确认概念时显示 suggested_count 提示', async () => {
    get.mockResolvedValue(ws({ suggested_count: 7 }))
    const w = await mountView()
    await w.findAll('.tabs button')[1].trigger('click')
    await flushPromises()
    const t = w.text()
    expect(t).toContain('7')
    expect(t).toContain('待确认概念')
  })

  it('点击概念跳转术语页（含 encodeURIComponent）', async () => {
    get.mockResolvedValue(
      ws({
        top_concepts: [
          { term: 'A B', definition: '定义', source_count: 3, status: 'accepted', is_topic: false },
        ],
      }),
    )
    const w = await mountView()
    await w.findAll('.tabs button')[1].trigger('click')
    await flushPromises()
    await w.find('.concept').trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/tech/concepts/A%20B')
  })
})

describe('DomainWorkspaceView 设定与刷新', () => {
  it('点刷新按钮重新拉取工作台', async () => {
    get.mockResolvedValue(ws())
    const w = await mountView()
    get.mockClear()
    // 头部 btn.sm 依次是雷达/周报、知识库设定、刷新;刷新取最后一个。
    const btns = w.findAll('button.btn.sm')
    await btns[btns.length - 1].trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/domains/tech')
  })

  it('点知识库设定打开 ProfileEditor', async () => {
    get.mockResolvedValue(ws())
    const w = await mountView()
    expect(w.findComponent({ name: 'ProfileEditor' }).exists()).toBe(false)
    const setBtn = w.findAll('button.btn.sm').find(b => b.text().includes('知识库设定'))
    await setBtn!.trigger('click')
    await flushPromises()
    expect(w.findComponent({ name: 'ProfileEditor' }).exists()).toBe(true)
  })
})
