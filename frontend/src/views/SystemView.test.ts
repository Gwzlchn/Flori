import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { ref } from 'vue'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import { useWorkerStore } from '../stores/workers'

// 顶层 mock:组件 <script setup> import 什么就 mock 什么
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

// 实时 ws:返回可控 systemStatus ref + connected,避免真连 WebSocket。
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
// 都拿到与组件同一个 store 实例。若只在 mountView 内建 pinia 且不 setActivePinia,mount 前取的 store
// 会绑到上一个测试遗留的陈旧 pinia,onMounted 的 fetch* 永不命中目标 store。
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

// store actions(stubActions)需返回值:fetchFullStatus/fetchUsage/fetchEvents 给默认。
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
    // 系统信息/调度信息并入概览/核心组件区,不单列。
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
        makeWorker({ id: 'c', status: 'paused' }),
        makeWorker({ id: 'd', status: 'offline' }),
      ],
    })
    await flushPromises()
    // 概览拆「系统 / Worker·作业」两组;KPI 在 Worker·作业 网格(.sg-worker),前 4 格 = KPI。
    const metrics = w.findAll('.sg-worker .st-val').map(n => n.text())
    expect(metrics[0]).toBe('3 / 4')   // online-* + paused 视为在线管理态
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

  it('worker 卡片不再提供配置入口', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({
      workers: [makeWorker({ id: 'w-cfg', status: 'online-idle', cfg_rev: 2, applied_cfg_rev: 2 })],
    })
    await flushPromises()
    const card = w.find('.wcard')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('备注')
    expect(card.text()).not.toContain('配置')
    expect(card.find('.cfg-panel').exists()).toBe(false)
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

  it('接入新 Worker 折叠区含镜像与 GATEWAY_URL，点生成临时 token 后展示有效期', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const sampleToken = 'flw-' + 'test'
    ;(store.mintToken as any).mockResolvedValue({ token: sampleToken, expires_in_sec: 3600 })
    const w = mountView({ workers: [] })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('接入新 Worker')
    expect(t).toContain('flori-worker:latest')   // 接入命令默认镜像 = flori-worker
    expect(t).toContain('GATEWAY_URL')
    expect(t).toContain('WORKER_TOKEN_FILE')
    expect(t).toContain('选择能力')
    expect(t).toContain('复制部署文件')
    const mintBtn = w.findAll('button').find(b => b.text().includes('生成 token'))
    await mintBtn!.trigger('click')
    await flushPromises()
    expect(store.mintToken).toHaveBeenCalled()
    expect(w.text()).toContain('flw-test')
    expect(w.text()).toContain('有效期 1h00m')
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

  // 组件 down/降级的状态呈现由核心组件卡承担,归 ComponentCard 测试,此处不覆盖。

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

  it('接入命令默认输出 Gateway-only compose,不包含 Redis/MinIO 直连配置', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    const cmd = w.find('pre').text()
    expect(cmd).toContain('services:')
    expect(cmd).toContain('GATEWAY_URL')
    expect(cmd).toContain('WORKER_REGISTRATION_TOKEN')
    expect(cmd).toContain('WORKER_ID_FILE')
    expect(cmd).toContain('WORKER_TOKEN_FILE')
    expect(cmd).toContain('HOME')
    expect(cmd).toContain('./flori-worker-state/cpu-1:/home/worker')
    expect(cmd).not.toContain('REDIS_URL')
    expect(cmd).not.toContain('MINIO_')
    expect(cmd).not.toContain('depends_on')
  })

  it('Watchtower 勾选后输出 worker + watchtower compose 与 scope/interval', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    await w.find('[data-testid="watchtower-enabled"]').setValue(true)
    await w.find('[data-testid="watchtower-interval"]').setValue('240')
    await flushPromises()
    const cmd = w.find('pre').text()
    expect(cmd).toContain('flori-worker-cpu-1:')
    expect(cmd).toContain('watchtower-cpu-1:')
    expect(cmd).toContain('ghcr.io/containrrr/watchtower:latest')
    expect(cmd).toContain('com.centurylinklabs.watchtower.enable=true')
    expect(cmd).toContain('com.centurylinklabs.watchtower.scope=flori-worker-cpu-1')
    expect(cmd).toContain('--label-enable --scope flori-worker-cpu-1 --cleanup --interval 240')
    expect(cmd).toContain('/var/run/docker.sock:/var/run/docker.sock')
  })

  it('docker run 输出也是 Gateway-only', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    const dockerBtn = w.findAll('.seg button').find(b => b.text().includes('docker run'))
    await dockerBtn!.trigger('click')
    await flushPromises()
    const cmd = w.find('pre').text()
    expect(cmd).toContain('docker run')
    expect(cmd).toContain('GATEWAY_URL')
    expect(cmd).toContain('WORKER_ID_FILE=/home/worker/worker.id')
    expect(cmd).toContain('WORKER_TOKEN_FILE=/home/worker/worker.token')
    expect(cmd).toContain('-v "./flori-worker-state/cpu-1:/home/worker"')
    expect(cmd).not.toContain('REDIS_URL')
    expect(cmd).not.toContain('MINIO_')
  })

  it('接入命令随勾选能力多选生成 --pools + 各能力配置并集', async () => {
    const store = useWorkerStore()
    stubStoreData(store)
    const w = mountView({ workers: [] })
    await flushPromises()
    // 默认只勾 cpu → --pools cpu,无 GPU/代理凭证
    expect(w.text()).toContain('--pools cpu')
    expect(w.find('pre').text()).not.toContain('gpus: all')
    // 再勾 io + gpu + ai(cpu 仍勾):命令排序稳定 + 三套配置取并集,无主次
    await w.find('input[type="checkbox"][value="io"]').setValue(true)
    await w.find('input[type="checkbox"][value="gpu"]').setValue(true)
    await w.find('input[type="checkbox"][value="ai"]').setValue(true)
    await flushPromises()
    const t = w.text()
    expect(t).toContain('--pools ai cpu gpu io')   // 排序 join,不随勾选顺序抖
    expect(w.find('pre').text()).toContain('gpus: all') // gpu → compose GPU 直通
    expect(t).toContain('MODEL_CACHE_DIR')          // gpu → whisper 缓存卷
    expect(t).not.toContain('BILI_' + 'SE' + 'SS' + 'DATA')  // io 凭证走中心分发,不进 worker env。
    expect(t).toContain('HF_ENDPOINT')              // cpu/gpu → whisper HF 国内镜像
    expect(w.find('pre').text()).not.toContain('GATEWAY_TLS_INSECURE') // 命令默认严格校验(页面提示文案除外)
    expect(t).toContain('使用持久状态目录内的 .claude') // ai(默认 claude-sub)→ 使用 worker 独立 HOME
    expect(w.find('pre').text()).not.toContain('${HOME}/.claude')
  })
})
