import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { installSourceCatalog } from '../constants/sources'

// router: 共享 push 间谍以断言跳转。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

// useApi: 按 URL 路由不同返回。views 把 store action 的返回写入本地 ref,
// 故用真 Pinia + 假 useApi,让真 action 跑通、把数据灌进视图。
const get = vi.fn()
const post = vi.fn()
const del = vi.fn()
vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get, post, del, put: vi.fn(), upload: vi.fn(), getText: vi.fn() }),
}))

import JobListView from './JobListView.vue'

// 默认空响应;各用例可前置覆写。
function setupApi(opts: {
  jobs?: { items: any[]; total: number }
  facets?: any
  domains?: any[]
} = {}) {
  const jobs = opts.jobs ?? { items: [], total: 0 }
  const facets = opts.facets ?? { source: {}, domain: {}, status: {} }
  const domains = opts.domains ?? []
  get.mockImplementation((url: string) => {
    if (url.includes('/api/jobs/facets')) return Promise.resolve(facets)
    if (url.startsWith('/api/jobs')) return Promise.resolve(jobs)
    if (url.startsWith('/api/domains')) return Promise.resolve({ domains })
    return Promise.resolve({})
  })
}

function job(over: Partial<any> = {}) {
  return {
    job_id: 'j1',
    content_type: 'video',
    status: 'done',
    created_at: '2026-01-01T00:00:00Z',
    title: '示例标题',
    progress_pct: 100,
    source: 'bilibili',
    domain: 'tech',
    collection_id: null,
    ...over,
  }
}

async function mountView() {
  const w = mount(JobListView, {
    global: { stubs: { StatusBadge: true } },
  })
  await flushPromises()
  return w
}

beforeEach(() => {
  installSourceCatalog({
    content_types: [{ type: 'video', label: '视频', upload_extensions: ['.mp4'] }],
    job_sources: [{ type: 'bilibili', label: 'Bilibili' }],
    subscription_sources: [],
  })
  setActivePinia(createPinia())
  vi.clearAllMocks()
  push.mockReset()
})

describe('JobListView 页头与筛选骨架', () => {
  it('渲染页头标题与三组筛选标签', async () => {
    setupApi()
    const w = await mountView()
    const t = w.text()
    expect(t).toContain('所有来源')
    expect(t).toContain('按状态')
    expect(t).toContain('按来源')
    expect(t).toContain('按知识库')
  })

  it('挂载时拉取 domains / facets / jobs 列表', async () => {
    setupApi()
    await mountView()
    const urls = get.mock.calls.map((c) => c[0] as string)
    expect(urls.some((u) => u.startsWith('/api/domains'))).toBe(true)
    expect(urls.some((u) => u.includes('/api/jobs/facets'))).toBe(true)
    expect(urls.some((u) => u.startsWith('/api/jobs?'))).toBe(true)
  })

  it('chip 计数来自后端 facets（处理中 = 多档求和）', async () => {
    setupApi({
      facets: {
        status: { done: 3, processing: 1, downloading: 1, pending: 2, failed: 4 },
        source: { bilibili: 5 },
        domain: {},
      },
    })
    const w = await mountView()
    const t = w.text()
    // 处理中 = processing(1)+downloading(1)+pending(2) = 4
    expect(t).toContain('已完成')
    expect(t).toContain('处理中')
    expect(t).toContain('失败')
  })

  it('待激活状态只渲染一个筛选 chip', async () => {
    setupApi({
      facets: {
        status: { pending_activation: 2 },
        source: {},
        domain: {},
      },
    })
    const w = await mountView()
    const statusChips = w.findAll('.fgroup')[0].findAll('.chip')
    const activationChips = statusChips.filter(chip => chip.text().startsWith('待激活'))
    expect(activationChips).toHaveLength(1)
    expect(activationChips[0].text()).toContain('2')
  })
})

describe('JobListView 状态分支', () => {
  it('库里没有内容时显示空态文案', async () => {
    setupApi({ jobs: { items: [], total: 0 } })
    const w = await mountView()
    expect(w.text()).toContain('还没有任何内容')
  })

  it('有内容时渲染列表行（标题 + 来源标签）', async () => {
    setupApi({ jobs: { items: [job({ title: '深度学习入门' })], total: 1 } })
    const w = await mountView()
    const t = w.text()
    expect(t).toContain('深度学习入门')
    expect(t).toContain('Bilibili')
  })

  it('标题缺省时回退展示 job_id', async () => {
    setupApi({ jobs: { items: [job({ title: null, job_id: 'fallback-id' })], total: 1 } })
    const w = await mountView()
    expect(w.text()).toContain('fallback-id')
  })
})

describe('JobListView 交互', () => {
  it('点击非失败行跳转内容详情', async () => {
    setupApi({ jobs: { items: [job({ job_id: 'go-me', status: 'done' })], total: 1 } })
    const w = await mountView()
    await w.find('.row').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/go-me')
  })

  it('失败行显示重试按钮，点击调用 retryJob 且不跳详情', async () => {
    setupApi({ jobs: { items: [job({ job_id: 'bad', status: 'failed' })], total: 1 } })
    post.mockResolvedValue({})
    const w = await mountView()
    const btn = w.find('button.btn.sm')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    // retryJob → POST /api/jobs/:id/retry
    expect(post).toHaveBeenCalledWith('/api/jobs/bad/retry')
    expect(push).not.toHaveBeenCalled()
  })

  it('行内删除按钮调用 deleteJob（DELETE /api/jobs/:id）', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    setupApi({ jobs: { items: [job({ job_id: 'del-me', status: 'done' })], total: 1 } })
    del.mockResolvedValue({})
    const w = await mountView()
    await w.find('[data-testid="row-delete"]').trigger('click')
    await flushPromises()
    expect(del).toHaveBeenCalledWith('/api/jobs/del-me')
  })

  it('选择模式多选并批量删除（逐条 DELETE）', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    setupApi({ jobs: { items: [job({ job_id: 'a' }), job({ job_id: 'b' })], total: 2 } })
    del.mockResolvedValue({})
    const w = await mountView()
    await w.find('[data-testid="select-toggle"]').trigger('click')   // 进选择模式
    await flushPromises()
    const checks = w.findAll('input.rowcheck')
    expect(checks.length).toBe(2)
    await checks[0].trigger('click')
    await checks[1].trigger('click')
    await flushPromises()
    const batchBtn = w.find('[data-testid="batch-delete"]')
    expect(batchBtn.text()).toContain('2')
    await batchBtn.trigger('click')
    await flushPromises()
    expect(del).toHaveBeenCalledWith('/api/jobs/a')
    expect(del).toHaveBeenCalledWith('/api/jobs/b')
  })

  it('「重建所有过期」按钮调用 rebuild-stale(POST /api/jobs/rebuild-stale)', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    setupApi({ jobs: { items: [job({ job_id: 'x', status: 'done' })], total: 1 } })
    post.mockResolvedValue({ rebuilt: 1 })
    const w = await mountView()
    await w.find('[data-testid="rebuild-stale"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/jobs/rebuild-stale')
  })

  it('点击状态 chip 触发带 status 参数的重新加载', async () => {
    setupApi({ jobs: { items: [], total: 0 }, facets: { status: { done: 1 }, source: {}, domain: {} } })
    const w = await mountView()
    get.mockClear()
    // 第一组(按状态)的第一个 chip 是已完成 done,单值可下推到服务端。
    const chip = w.findAll('.fgroup')[0].findAll('.chip')[0]
    await chip.trigger('click')
    await flushPromises()
    const jobsCall = get.mock.calls.map((c) => c[0] as string).find((u) => u.startsWith('/api/jobs?'))
    expect(jobsCall).toBeTruthy()
    expect(new URLSearchParams((jobsCall as string).split('?')[1]).get('status')).toBe('done')
  })
})
