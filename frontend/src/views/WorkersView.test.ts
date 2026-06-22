import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { ref } from 'vue'
import { createTestingPinia } from '@pinia/testing'
import { useWorkerStore } from '../stores/workers'

// ── 顶层 mock：组件 <script setup> import 什么就 mock 什么 ──
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

// useApi 仅被本视图用于 loadStatus(GET /api/status)；store 的请求被 stubActions 拦掉。
const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

// 实时 ws：返回一个可控的 systemStatus ref，避免真连 WebSocket。
const systemStatus = ref<any>(null)
vi.mock('../composables/useGlobalWs', () => ({
  useGlobalWs: () => ({ systemStatus, connected: ref(false) }),
}))

import WorkersView from './WorkersView.vue'

function makeWorker(over: Partial<any> = {}) {
  return {
    id: 'w1',
    type: 'cpu',
    pools: [],
    tags: [],
    reject_tags: [],
    hostname: 'host-a',
    gpu_name: null,
    gpu_memory_mb: null,
    status: 'online-idle',
    current_job: null,
    current_step: null,
    tasks_completed: 3,
    tasks_failed: 1,
    total_duration_sec: 0,
    first_seen: '2026-01-01T00:00:00Z',
    started_at: null,
    last_heartbeat: new Date().toISOString(),
    admin_note: null,
    ...over,
  }
}

// 统一挂载：用 createTestingPinia 注入 workers store 初始状态，stubActions 把 action 变 spy。
function mountView(state: { workers?: any[]; loading?: boolean } = {}) {
  const wrapper = mount(WorkersView, {
    global: {
      plugins: [
        createTestingPinia({
          createSpy: vi.fn,
          stubActions: true,
          initialState: {
            workers: {
              workers: state.workers ?? [],
              loading: state.loading ?? false,
            },
          },
        }),
      ],
      stubs: {
        // 子组件与图标不在被测范围，stub 掉以隔离。
        StatusBadge: true,
      },
    },
  })
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  systemStatus.value = null
  // 默认 /api/status 给一份最小可用结构（避免 onMounted 抛错）。
  api.get.mockResolvedValue({
    workers: {},
    pools: {},
    jobs: { total: 0, done: 0, processing: 0, failed: 0, pending: 0 },
    disk: { used_gb: 0, available_gb: 0 },
  })
})

describe('WorkersView', () => {
  it('渲染页头与三项系统指标标签', async () => {
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('系统与 Worker')
    expect(t).toContain('Worker 在线 / 共')
    expect(t).toContain('忙碌 · 处理中')
    expect(t).toContain('累计完成 · 吞吐')
  })

  it('空态：无 worker 时显示接入提示文案', async () => {
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('还没有接入任何 Worker')
    // 列表项不应渲染
    expect(w.findAll('.wcard').length).toBe(0)
  })

  it('加载态：loading 且列表空显示“加载中…”', async () => {
    const w = mountView({ workers: [], loading: true })
    await flushPromises()
    expect(w.text()).toContain('加载中…')
    expect(w.text()).not.toContain('还没有接入任何 Worker')
  })

  it('列表：渲染 worker 卡片与基本信息', async () => {
    const w = mountView({
      workers: [makeWorker({ id: 'worker-alpha', tasks_completed: 9, tasks_failed: 2 })],
    })
    await flushPromises()
    const cards = w.findAll('.wcard')
    expect(cards.length).toBe(1)
    const t = w.text()
    expect(t).toContain('worker-alpha')
    expect(t).toContain('host-a')
    expect(t).toContain('完成 9')
    expect(t).toContain('失败 2')
  })

  it('指标计数：在线/共、忙碌随 store.workers 派生', async () => {
    const w = mountView({
      workers: [
        makeWorker({ id: 'a', status: 'online-idle' }),
        makeWorker({ id: 'b', status: 'online-busy' }),
        makeWorker({ id: 'c', status: 'offline' }),
      ],
    })
    await flushPromises()
    const metrics = w.findAll('.metric .v').map(n => n.text())
    // 在线(online* + paused)=2 / 共 3；忙碌=1
    expect(metrics[0]).toBe('2 / 3')
    expect(metrics[1]).toBe('1')
  })

  it('累计完成优先取实时 ws.jobs.done', async () => {
    systemStatus.value = { jobs: { total: 0, done: 42, processing: 0, failed: 0 } }
    const w = mountView({ workers: [] })
    await flushPromises()
    const metrics = w.findAll('.metric .v').map(n => n.text())
    expect(metrics[2]).toBe('42')
  })

  it('在线 worker 显示“暂停”按钮，点击调用 store.pause', async () => {
    const w = mountView({
      workers: [makeWorker({ id: 'w-pause', status: 'online-idle' })],
    })
    await flushPromises()
    const store = useWorkerStore()
    expect(w.text()).toContain('暂停')
    // 第一个 .btn.sm 是行内 暂停 按钮
    const pauseBtn = w.findAll('.wcard .btn.sm').find(b => b.text().includes('暂停'))
    expect(pauseBtn).toBeTruthy()
    await pauseBtn!.trigger('click')
    await flushPromises()
    expect(store.pause).toHaveBeenCalledWith('w-pause')
    expect(store.resume).not.toHaveBeenCalled()
  })

  it('paused worker 显示“继续”，点击调用 store.resume', async () => {
    const w = mountView({
      workers: [makeWorker({ id: 'w-resume', status: 'paused' })],
    })
    await flushPromises()
    const store = useWorkerStore()
    const btn = w.findAll('.wcard .btn.sm').find(b => b.text().includes('继续'))
    expect(btn).toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()
    expect(store.resume).toHaveBeenCalledWith('w-resume')
    expect(store.pause).not.toHaveBeenCalled()
  })

  it('离线 worker 显示“移除”，确认后调用 store.remove', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const w = mountView({
      workers: [makeWorker({ id: 'w-off', status: 'offline' })],
    })
    await flushPromises()
    const store = useWorkerStore()
    const btn = w.findAll('.wcard .btn.danger').find(b => b.text().includes('移除'))
    expect(btn).toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalled()
    expect(store.remove).toHaveBeenCalledWith('w-off')
    confirmSpy.mockRestore()
  })

  it('移除：confirm 取消时不调用 store.remove', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const w = mountView({
      workers: [makeWorker({ id: 'w-off', status: 'offline' })],
    })
    await flushPromises()
    const store = useWorkerStore()
    const btn = w.findAll('.wcard .btn.danger').find(b => b.text().includes('移除'))
    await btn!.trigger('click')
    await flushPromises()
    expect(store.remove).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('点击刷新按钮触发 store.fetchAll 与 /api/status 拉取', async () => {
    const w = mountView({ workers: [] })
    await flushPromises()
    const store = useWorkerStore()
    api.get.mockClear()
    ;(store.fetchAll as any).mockClear()
    // 页头刷新按钮（margin-left:auto），取页头里第一个含“刷新”文案的按钮
    const refreshBtn = w.findAll('button').find(b => b.text().includes('刷新'))
    expect(refreshBtn).toBeTruthy()
    await refreshBtn!.trigger('click')
    await flushPromises()
    expect(store.fetchAll).toHaveBeenCalled()
    expect(api.get).toHaveBeenCalledWith('/api/status')
  })

  it('onMounted 自动拉取系统状态并渲染资源池区块', async () => {
    api.get.mockResolvedValue({
      workers: {},
      pools: { cpu: { capacity: 4, used: 2, queue: 1 } },
      jobs: { total: 10, done: 7, processing: 1, failed: 2, pending: 0 },
      disk: { used_gb: 12, available_gb: 88 },
    })
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(api.get).toHaveBeenCalledWith('/api/status')
    expect(t).toContain('资源池')
    expect(t).toContain('cpu')
  })

  it('系统状态拉取失败显示错误与重试按钮', async () => {
    api.get.mockRejectedValueOnce(new Error('网络炸了'))
    const w = mountView({ workers: [] })
    await flushPromises()
    expect(w.text()).toContain('网络炸了')
    const retry = w.findAll('button').find(b => b.text().includes('重试'))
    expect(retry).toBeTruthy()
  })

  it('接入新 Worker：默认 gateway 命令含镜像与 GATEWAY_URL', async () => {
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('接入新 Worker')
    expect(t).toContain('flori:latest')
    expect(t).toContain('GATEWAY_URL')
    expect(t).toContain('生成接入 token')
  })

  it('点击生成 token 调用 store.mintToken', async () => {
    const w = mountView({ workers: [] })
    await flushPromises()
    const store = useWorkerStore()
    const mintBtn = w.findAll('button').find(b => b.text().includes('生成接入 token'))
    expect(mintBtn).toBeTruthy()
    await mintBtn!.trigger('click')
    await flushPromises()
    expect(store.mintToken).toHaveBeenCalled()
  })
})
