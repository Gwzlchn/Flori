import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import type { Collection, JobSummary } from '../types'

// ── 路由 mock：route.params.id 决定加载哪个集合;push 用于 openJob/删除后跳转 ──
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: { id: 'col-1' }, query: {} }),
}))

// ── store mock：直接控制 get/fetchJobs/remove 的返回,避免真实 action 调 useApi ──
const storeGet = vi.fn()
const storeFetchJobs = vi.fn()
const storeRemove = vi.fn()
vi.mock('../stores/collections', () => ({
  useCollectionStore: () => ({ get: storeGet, fetchJobs: storeFetchJobs, remove: storeRemove }),
}))

// ── jobs store mock：集合级重试走 jobStore.retryFailedInCollection ──
const retryFailedInCollection = vi.fn()
vi.mock('../stores/jobs', () => ({
  useJobStore: () => ({ retryFailedInCollection }),
}))

const setCrumbs = vi.fn()
vi.mock('../stores/global', () => ({
  useGlobalStore: () => ({ setCrumbs }),
}))

// ── useApi mock：syncNow/toggleAutoSync 走 api.post/api.put ──
const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import CollectionDetailView from './CollectionDetailView.vue'

function makeCollection(over: Partial<Collection> = {}): Collection {
  return {
    id: 'col-1',
    name: '深度学习课',
    domain: 'ml',
    description: '一些描述',
    tags: ['tag-a', 'tag-b'],
    job_count: 4,
    created_at: '2026-01-01T00:00:00Z',
    subscription: null,
    ...over,
  }
}

function makeJob(over: Partial<JobSummary> = {}): JobSummary {
  return {
    job_id: 'job-1',
    content_type: 'video',
    status: 'done',
    created_at: '2026-02-01T10:00:00Z',
    title: '第一讲',
    progress_pct: 100,
    source: 'bilibili',
    domain: 'ml',
    collection_id: 'col-1',
    ...over,
  }
}

const showToast = vi.fn()
function mountView() {
  return mount(CollectionDetailView, {
    global: {
      provide: { showToast },
      // 子组件 stub:不在测试范围内,避免其内部依赖
      stubs: { StatusBadge: true, DeleteCollectionDialog: true },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('CollectionDetailView 加载与渲染', () => {
  it('加载成功:渲染集合名/信息卡/内容列表,并写面包屑', async () => {
    storeGet.mockResolvedValue(makeCollection())
    storeFetchJobs.mockResolvedValue({ total: 1, items: [makeJob()] })
    const w = mountView()
    await flushPromises()

    const t = w.text()
    expect(t).toContain('深度学习课')
    expect(t).toContain('手动')          // 无 subscription → 手动徽标
    expect(t).toContain('集合信息')
    expect(t).toContain('一些描述')
    expect(t).toContain('第一讲')        // 列表内容标题
    expect(t).toContain('内容 · 1')      // total
    expect(w.findAll('.list .row')).toHaveLength(1)
    // load() 用真实集合名写面包屑
    expect(setCrumbs).toHaveBeenCalled()
    const segs = setCrumbs.mock.calls[0][0]
    expect(segs[segs.length - 1].t).toBe('深度学习课')
  })

  it('404:store.get 抛含 404 的错误 → 显示不存在', async () => {
    storeGet.mockRejectedValue(new Error('HTTP 404 Not Found'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('集合不存在或已删除')
  })

  it('非 404 错误:显示错误信息与重试', async () => {
    storeGet.mockRejectedValue(new Error('boom'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('boom')
    expect(w.text()).toContain('重试')
  })

  it('空内容:显示「此集合暂无内容」', async () => {
    storeGet.mockResolvedValue(makeCollection())
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('此集合暂无内容')
  })
})

describe('CollectionDetailView 订阅集合', () => {
  function subCollection() {
    return makeCollection({
      subscription: {
        source_type: 'bilibili_up',
        source_id: '247209804',
        source_label: 'bilibili',
        enabled: true,
        last_synced_at: '2026-03-01T08:00:00Z',
      },
    })
  }

  it('有订阅:渲染订阅源卡 + 立即同步按钮', async () => {
    storeGet.mockResolvedValue(subCollection())
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('订阅源')
    expect(t).toContain('立即同步')
    // enabled=true + 有 last_synced_at + 无 last_sync_status → 5 态点 active(绿)
    expect(w.find('.sub-dot.active').exists()).toBe(true)
  })

  it('点击立即同步:POST sync 端点并成功 toast', async () => {
    storeGet.mockResolvedValue(subCollection())
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    api.post.mockResolvedValue({ new: 2, total: 10 })
    const w = mountView()
    await flushPromises()

    const syncBtn = w.findAll('button').find((b) => b.text().includes('立即同步'))!
    await syncBtn.trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/collections/col-1/sync')
    expect(showToast).toHaveBeenCalledWith('同步完成：新增 2 个（共 10）', 'success')
  })

  it('切换自动同步:PUT 集合 sync_enabled 取反', async () => {
    storeGet.mockResolvedValue(subCollection())   // enabled=true
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    api.put.mockResolvedValue({})
    const w = mountView()
    await flushPromises()

    await w.find('.switch').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/collections/col-1', { sync_enabled: false })
  })
})

describe('CollectionDetailView 交互', () => {
  it('点击内容行:router.push 到内容详情', async () => {
    storeGet.mockResolvedValue(makeCollection())
    storeFetchJobs.mockResolvedValue({ total: 1, items: [makeJob({ job_id: 'job-9' })] })
    const w = mountView()
    await flushPromises()

    await w.find('.list .row').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/job-9')
  })

  it('点击删除按钮:打开删除对话框', async () => {
    storeGet.mockResolvedValue(makeCollection())
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mountView()
    await flushPromises()
    // 初始 showDelete=false → 对话框 stub 不渲染
    expect(w.find('delete-collection-dialog-stub').exists()).toBe(false)

    const delBtn = w.findAll('button').find((b) => b.text().includes('删除'))!
    await delBtn.trigger('click')
    await flushPromises()
    expect(w.find('delete-collection-dialog-stub').exists()).toBe(true)
  })
})

describe('CollectionDetailView 状态分布与集合级重试', () => {
  function withCounts(failed: number) {
    return {
      ...makeCollection(),
      status_counts: { done: 2, processing: 1, failed, pending: 0 },
    } as any
  }

  it('有 status_counts:信息卡渲染状态分布', async () => {
    storeGet.mockResolvedValue(withCounts(3))
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('状态分布')
    expect(t).toContain('完成 2')
    expect(t).toContain('失败 3')
  })

  it('有失败任务:显示重试按钮,确认后调 retryFailedInCollection 并 toast', async () => {
    storeGet.mockResolvedValue(withCounts(3))
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    retryFailedInCollection.mockResolvedValue({ retried: 3 })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const w = mountView()
    await flushPromises()

    const btn = w.findAll('button').find((b) => b.text().includes('重试本集合失败'))!
    expect(btn).toBeTruthy()
    await btn.trigger('click')
    await flushPromises()

    expect(retryFailedInCollection).toHaveBeenCalledWith('col-1')
    expect(showToast).toHaveBeenCalledWith('已重试 3 个失败任务', 'success')
  })

  it('无失败任务:不显示重试按钮', async () => {
    storeGet.mockResolvedValue(withCounts(0))
    storeFetchJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mountView()
    await flushPromises()
    expect(w.findAll('button').some((b) => b.text().includes('重试本集合失败'))).toBe(false)
  })
})
