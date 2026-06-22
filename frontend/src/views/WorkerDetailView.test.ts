import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'

// ── 依赖 mock（须在 import 组件前）──
// 路由：固定 worker id = w1；记录 push 以验证「移除后跳转 / 历史行点击」。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'w1' }, query: {} }),
  useRouter: () => ({ push, replace: vi.fn() }),
}))

// api：组件主体走 api.get(/api/workers/{id})；store 内部也用同一 useApi。
const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import WorkerDetailView from './WorkerDetailView.vue'

// 完整 Worker 夹具（按 types.Worker 字段补齐）。
function makeWorker(over: Record<string, unknown> = {}) {
  return {
    id: 'w1',
    type: 'gpu',
    pools: ['default'],
    tags: ['fast'],
    reject_tags: [],
    hostname: 'host-a',
    gpu_name: 'RTX 4090',
    gpu_memory_mb: 24576,
    status: 'online-idle',
    current_job: null,
    current_step: null,
    tasks_completed: 8,
    tasks_failed: 2,
    total_duration_sec: 3725,
    first_seen: '2026-01-01T00:00:00Z',
    started_at: '2026-01-01T00:00:00Z',
    last_heartbeat: new Date().toISOString(),
    admin_note: '主力卡',
    ...over,
  }
}

// 历史任务夹具。
const JOBS = [
  { job_id: 'job-1', step: '01_download', status: 'done', started_at: null, finished_at: new Date().toISOString(), duration_sec: 42, error: null },
  { job_id: 'job-2', step: '02_transcribe', status: 'failed', started_at: null, finished_at: new Date().toISOString(), duration_sec: 90, error: 'x' },
]

// 挂载助手：注入 showToast、createTestingPinia(stubActions:false → 真 action 走 mock api)、stub 子组件。
function factory() {
  return mount(WorkerDetailView, {
    global: {
      plugins: [createTestingPinia({ createSpy: vi.fn, stubActions: false })],
      provide: { showToast: vi.fn() },
      stubs: { StatusBadge: true },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  // 默认成功路径：主体 + 历史。
  api.get.mockImplementation((url: string) => {
    if (url.endsWith('/jobs')) return Promise.resolve(JOBS)
    return Promise.resolve(makeWorker())
  })
  api.put.mockResolvedValue(undefined)
  api.del.mockResolvedValue(undefined)
})

describe('WorkerDetailView', () => {
  it('onMounted 调 GET /api/workers/{id}（URL 编码 id）', async () => {
    factory()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/workers/w1')
  })

  it('成功态：渲染 id、统计与成功率', async () => {
    const w = factory()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('w1')
    expect(t).toContain('累计完成')
    expect(t).toContain('累计失败')
    // 8 完成 / (8+2) = 80.0%
    expect(t).toContain('80.0%')
  })

  it('成功态：渲染基本信息（算力 GPU+显存、主机名）', async () => {
    const w = factory()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('host-a')
    expect(t).toContain('RTX 4090')
    expect(t).toContain('24GB')
  })

  it('历史为空：显示空文案', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/jobs')) return Promise.resolve([])
      return Promise.resolve(makeWorker())
    })
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('暂无任务历史')
  })

  it('历史非空：渲染任务行并可点击跳转内容详情', async () => {
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('job-1')
    const row = w.find('.row')
    expect(row.exists()).toBe(true)
    await row.trigger('click')
    expect(push).toHaveBeenCalledWith('/content/job-1')
  })

  it('404 错误态：展示「Worker 不存在或已移除」', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/jobs')) return Promise.resolve([])
      return Promise.reject({ status: 404 })
    })
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('Worker 不存在或已移除')
  })

  it('错误态「重试」按钮重新加载', async () => {
    api.get.mockImplementationOnce(() => Promise.reject({ message: 'boom' }))
      .mockImplementation((url: string) => (url.endsWith('/jobs') ? Promise.resolve([]) : Promise.resolve(makeWorker())))
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('boom')
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))
    expect(retry).toBeTruthy()
    api.get.mockClear()
    await retry!.trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/workers/w1')
  })

  it('排空操作：PUT status=draining（worker store drain 走 mock api）', async () => {
    const w = factory()
    await flushPromises()
    const drainBtn = w.findAll('button').find((b) => b.text().includes('排空') && !b.text().includes('取消'))
    expect(drainBtn).toBeTruthy()
    await drainBtn!.trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/workers/w1', { status: 'draining' })
  })

  it('移除操作：confirm 通过后 force=true 删除并跳转 /system', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    const w = factory()
    await flushPromises()
    const removeBtn = w.findAll('button').find((b) => b.text().includes('移除'))
    await removeBtn!.trigger('click')
    await flushPromises()
    // isOnline(online-idle) → force=true
    expect(api.del).toHaveBeenCalledWith('/api/workers/w1?force=true')
    expect(push).toHaveBeenCalledWith('/system')
    vi.unstubAllGlobals()
  })

  it('移除操作：confirm 取消则不发请求', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    const w = factory()
    await flushPromises()
    const removeBtn = w.findAll('button').find((b) => b.text().includes('移除'))
    await removeBtn!.trigger('click')
    await flushPromises()
    expect(api.del).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('备注编辑：点击编辑展开输入，保存触发 PUT admin_note', async () => {
    const w = factory()
    await flushPromises()
    const editBtn = w.findAll('button').find((b) => b.text().includes('编辑'))
    expect(editBtn).toBeTruthy()
    await editBtn!.trigger('click')
    const input = w.find('input.input')
    expect(input.exists()).toBe(true)
    await input.setValue('新备注')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/workers/w1', { admin_note: '新备注' })
  })

  it('成功率：完成与失败均为 0 时显示「—」', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/jobs')) return Promise.resolve([])
      return Promise.resolve(makeWorker({ tasks_completed: 0, tasks_failed: 0 }))
    })
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('成功率')
    expect(w.text()).toContain('—')
  })
})
