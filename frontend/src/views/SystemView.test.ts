import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { ref } from 'vue'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import { useWorkerStore } from '../stores/workers'

// ── 顶层 mock：组件 <script setup> import 什么就 mock 什么 ──
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

// 实时 ws：返回可控 systemStatus ref + connected，避免真连 WebSocket。
const systemStatus = ref<any>(null)
vi.mock('../composables/useGlobalWs', () => ({
  useGlobalWs: () => ({ systemStatus, connected: ref(true), reconnect: vi.fn() }),
}))

import SystemView from './SystemView.vue'

function fullStatus(over: Partial<any> = {}) {
  return {
    version: 'a1b2c3d',
    components: [
      { name: 'api', kind: 'api', status: 'up', version: 'a1b2c3d',
        last_heartbeat: new Date().toISOString(), uptime_sec: 100, detail: null, extra: { rss_mb: 50 } },
      { name: 'scheduler', kind: 'scheduler', status: 'up', version: 'a1b2c3d',
        last_heartbeat: new Date().toISOString(), uptime_sec: 90, detail: null,
        extra: { loop_lag_sec: 0.5, loop_interval_sec: 30, pid: 7 } },
      { name: 'redis', kind: 'redis', status: 'up', version: '7.2.4',
        last_heartbeat: new Date().toISOString(), uptime_sec: 1000, detail: null,
        extra: { used_memory_human: '2M', used_memory_mb: 2, maxmemory_mb: 0, connected_clients: 1, ping_ms: 1.0 } },
      { name: 'minio', kind: 'minio', status: 'unknown', version: null,
        last_heartbeat: null, uptime_sec: null, detail: '本地盘', extra: { mode: 'local' } },
    ],
    workers: {},
    pools: { cpu: { capacity: 4, used: 2, queue: 1 } },
    jobs: { total: 10, done: 7, processing: 1, failed: 2, pending: 3 },
    disk: { used_gb: 12, available_gb: 88, total_gb: 100, used_pct: 12 },
    throughput_1h: { done: 5, failed: 1 },
    ...over,
  }
}

function makeWorker(over: Partial<any> = {}) {
  return {
    id: 'w1', type: 'cpu', pools: [], tags: [], reject_tags: [],
    hostname: 'host-a', gpu_name: null, gpu_memory_mb: null, concurrency: 1,
    remote_addr: null, spec: {}, load: {},
    status: 'online-idle', current_job: null, current_step: null,
    tasks_completed: 3, tasks_failed: 1, total_duration_sec: 0,
    first_seen: '2026-01-01T00:00:00Z', started_at: null,
    last_heartbeat: new Date().toISOString(), admin_note: null,
    ...over,
  }
}

// 共享一个 testing pinia(在 beforeEach 建并 setActivePinia)→ 测试里 useWorkerStore() 在 mount 前/后
// 都拿到与组件同一个 store 实例。此前 pinia 建在 mountView 内且无 setActivePinia,mount 前取的 store
// 绑到上一个测试遗留的陈旧 pinia,onMounted 的 fetch* 永不命中目标 store → 9/12 失败。
let pinia: ReturnType<typeof createTestingPinia>

function mountView(state: { workers?: any[]; loading?: boolean } = {}) {
  const store: any = useWorkerStore()
  store.workers = state.workers ?? []
  store.loading = state.loading ?? false
  return mount(SystemView, {
    global: {
      plugins: [pinia],
      stubs: { StatusBadge: true, ComponentCard: true },
    },
  })
}

// store actions（stubActions）需返回值：fetchFullStatus/fetchUsage/fetchEvents 给默认。
function stubStoreData(store: any, opts: { full?: any; usage?: any; events?: any[] } = {}) {
  ;(store.fetchFullStatus as any).mockResolvedValue(opts.full ?? fullStatus())
  ;(store.fetchUsage as any).mockResolvedValue(opts.usage ?? { calls: 0, by_model: [], cache_hit_rate_pct: 0,
    total_input_tokens: 0, total_output_tokens: 0, total_cache_creation_tokens: 0,
    total_cache_read_tokens: 0, total_cost_usd: 0, total_num_turns: 0, total_duration_sec: 0 })
  ;(store.fetchEvents as any).mockResolvedValue({ events: opts.events ?? [] })
  ;(store.fetchPoolLimits as any).mockResolvedValue({ cpu: { default: 4, override: null } })
}

beforeEach(() => {
  vi.clearAllMocks()
  systemStatus.value = null
  pinia = createTestingPinia({ createSpy: vi.fn, stubActions: true })
  setActivePinia(pinia)
  stubStoreData(useWorkerStore())   // 安全默认(onMounted 即 refreshAll 会用到);各测试可再 stub 覆盖
})

describe('SystemView', () => {
  it('渲染页头与四项系统指标标签', async () => {
    const w = mountView()
    stubStoreData(useWorkerStore())
    await flushPromises()
    const t = w.text()
    expect(t).toContain('系统健康总览')
    expect(t).toContain('Worker 在线 / 共')
    expect(t).toContain('忙碌 · 处理中')
    expect(t).toContain('待处理 · 队列')
    expect(t).toContain('累计完成 · 吞吐')
  })

  it('拉取全量状态后渲染三带区块与资源池', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(store.fetchFullStatus).toHaveBeenCalled()
    // 三带重组后:系统信息/调度信息 区已并入概览/核心组件,不再单列。
    expect(t).toContain('核心组件')
    expect(t).toContain('系统事件')
    expect(t).toContain('资源池')
    expect(t).toContain('cpu')
    expect(t).toContain('a1b2c3d')   // 系统版本(构建 sha,概览版本徽章)
  })

  it('空态：无 worker 显示接入提示', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    expect(w.text()).toContain('还没有接入任何 Worker')
    expect(w.findAll('.wcard').length).toBe(0)
  })

  it('指标计数：在线/共、忙碌、待处理随 store + 全量派生', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({
      workers: [
        makeWorker({ id: 'a', status: 'online-idle' }),
        makeWorker({ id: 'b', status: 'online-busy' }),
        makeWorker({ id: 'c', status: 'offline' }),
      ],
    })
    await flushPromises()
    const metrics = w.findAll('.metric .v').map(n => n.text())
    expect(metrics[0]).toBe('2 / 3')   // 在线/共
    expect(metrics[1]).toBe('1')       // 忙碌
    expect(metrics[2]).toBe('3')       // 待处理(jobs.pending)
  })

  it('版本漂移：worker spec.version 与系统版本不符显示旧版本徽章', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({
      workers: [makeWorker({ id: 'old-w', spec: { version: 'deadbeef999' } })],
    })
    await flushPromises()
    expect(w.text()).toContain('旧版本')
    expect(w.text()).toContain('版本漂移')
  })

  it('worker live 负载显示 CPU/内存/负载', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({
      workers: [makeWorker({ id: 'busy', load: { cpu_pct: 33, mem_pct: 60, loadavg: 1.1 } })],
    })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('CPU 33%')
    expect(t).toContain('内存 60%')
    expect(t).toContain('负载 1.1')
  })

  it('在线 worker 点暂停调用 store.pause', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [makeWorker({ id: 'w-pause', status: 'online-idle' })] })
    await flushPromises()
    const pauseBtn = w.findAll('.wcard .btn.sm').find(b => b.text().includes('暂停'))
    await pauseBtn!.trigger('click')
    await flushPromises()
    expect(store.pause).toHaveBeenCalledWith('w-pause')
  })

  it('离线 worker 确认后调用 store.remove', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [makeWorker({ id: 'w-off', status: 'offline' })] })
    await flushPromises()
    const btn = w.findAll('.wcard .btn.danger').find(b => b.text().includes('移除'))
    await btn!.trigger('click')
    await flushPromises()
    expect(store.remove).toHaveBeenCalledWith('w-off', false)  // 离线=普通移除(force=false)
    confirmSpy.mockRestore()
  })

  it('接入新 Worker 折叠区含镜像与 GATEWAY_URL，点生成 token 调 store.mintToken', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('接入新 Worker')
    expect(t).toContain('flori:latest')
    expect(t).toContain('GATEWAY_URL')
    const mintBtn = w.findAll('button').find(b => b.text().includes('生成接入 token'))
    await mintBtn!.trigger('click')
    await flushPromises()
    expect(store.mintToken).toHaveBeenCalled()
  })

  it('AI 用量聚合：有调用时展示命中率与成本', async () => {
    const store = useWorkerStore()
    stubStoreData(store, {
      usage: {
        calls: 5, total_input_tokens: 1000, total_output_tokens: 200,
        total_cache_creation_tokens: 100, total_cache_read_tokens: 400,
        total_cost_usd: 0.5, total_num_turns: 10, total_duration_sec: 20,
        cache_hit_rate_pct: 26.7,
        by_model: [{ provider: 'claude-cli', model: 'claude-opus', calls: 5,
          input_tokens: 1000, output_tokens: 200, cache_creation_tokens: 100,
          cache_read_tokens: 400, cost_usd: 0.5, cache_hit_rate_pct: 26.7 }],
      },
    })
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('AI 用量')
    expect(t).toContain('26.7%')
    expect(t).toContain('（等价）')   // claude-cli 成本标等价
  })

  it('健康条：组件 down 时进异常档', async () => {
    const store = useWorkerStore()
    const fs = fullStatus()
    fs.components[2].status = 'down'  // redis down
    fs.components[2].detail = 'redis 不可达'
    stubStoreData(store, { full: fs })
    const w = mountView({ workers: [] })
    await flushPromises()
    expect(w.text()).toContain('需处理')
    expect(w.find('.health-bar').classes()).toContain('hb-down')
  })

  it('点刷新触发 store.fetchAll 与 fetchFullStatus', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    ;(store.fetchAll as any).mockClear()
    ;(store.fetchFullStatus as any).mockClear()
    const refreshBtn = w.findAll('button').find(b => b.text().includes('刷新'))
    await refreshBtn!.trigger('click')
    await flushPromises()
    expect(store.fetchAll).toHaveBeenCalled()
    expect(store.fetchFullStatus).toHaveBeenCalled()
  })
})
