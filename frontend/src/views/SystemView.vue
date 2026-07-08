<script setup lang="ts">
// 系统健康总览页(/system)。三带(自上而下):
//  带1 概览:概览指标 + 系统状态行(整体版本/部署/磁盘/内容/吞吐/中转) + 最近 5 事件
//  带2 基础设施:核心组件(api/scheduler/redis/minio) + 通联/链路拓扑
//  带3 算力与用量:上半 AI 用量 + 价表,下半 资源池 + Worker 列表 + 接入新 worker 折叠
// worker 接入在本页(运维);MCP 接入卡在 /settings(用户集成)。事件全量在 /system/events(类型/时间筛选)。
// 双通道:WS 每 2s 推 live 子集(计数/忙闲/队列/磁盘跳动);HTTP /api/status + /api/usage +
// /api/events 进页 1 次 + 每 15s 轮询(组件/版本/吞吐/用量/事件,慢变量)+ 手动刷新。
import { ref, computed, onMounted, onUnmounted, inject, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkerStore } from '../stores/workers'
import { useGlobalWs } from '../composables/useGlobalWs'
import StatusBadge from '../components/common/StatusBadge.vue'
import ComponentCard from '../components/system/ComponentCard.vue'
import LinkTopologyTree from '../components/system/LinkTopologyTree.vue'
import { fmtDuration, fmtRelative } from '../utils/datetime'
import { fmtBytes } from '../utils/format'
import { eventLabel, eventDot, eventSummary } from '../utils/events'
import { workerDotClass, workerComputeDesc } from '../utils/worker'
import type { Worker, FullStatus, SystemComponent, SystemEvent, UsageAggregate, PricingStatus, LinkTraffic } from '../types'
import { COMPONENT_KIND_LABELS } from '../types'
import {
  Server, RefreshCw, Cpu, Pause, Play, MessageSquare, X, Plus,
  Key, Copy, Check, Layers, HardDrive, Database, Boxes, AlertTriangle,
  Coins, Braces, Network,
} from 'lucide-vue-next'

const router = useRouter()
const workerStore = useWorkerStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

// WS live 子集(每 2s):只覆盖 jobs/workers/pools/disk 四段,组件/版本/吞吐保持上次轮询值。
const { systemStatus, connected, reconnect } = useGlobalWs()

const status = ref<FullStatus | null>(null)
const lastOkAt = ref<number | null>(null)   // 末次成功 /api/status 时间戳
const failStreak = ref(0)                    // 连续失败计数(抖动缓冲)
const usage = ref<UsageAggregate | null>(null)
const events = ref<SystemEvent[]>([])
const pricing = ref<PricingStatus | null>(null)   // LiteLLM 价表状态(模型数 + 更新时间)
const pricingBusy = ref(false)                     // 手动更新中(按钮转圈禁用)

async function loadStatus() {
  try {
    status.value = await workerStore.fetchFullStatus()
    lastOkAt.value = Date.now()
    failStreak.value = 0
  } catch {
    failStreak.value++
    // 不立即清空已有数据,保留陈旧快照就地展示。
  }
}
async function loadUsage() {
  try { usage.value = await workerStore.fetchUsage() } catch { /* 非致命 */ }
}
async function loadEvents() {
  try { events.value = (await workerStore.fetchEvents(50)).events } catch { /* 非致命 */ }
}
async function loadPricing() {
  try { pricing.value = await workerStore.fetchPricing() } catch { /* 非致命 */ }
}
// 手动更新价表:转圈禁用,成功回填新 status,失败提示(后端拉取失败回 502)。
async function doRefreshPricing() {
  if (pricingBusy.value) return
  pricingBusy.value = true
  try {
    pricing.value = await workerStore.refreshPricing()
    showToast('价表已更新', 'success')
  } catch {
    showToast('价表更新失败(网络/上游异常),已保留旧表', 'error')
  } finally {
    pricingBusy.value = false
  }
}
function openPricingRaw() { window.open('/api/pricing/raw', '_blank') }

async function refreshAll() {
  await Promise.all([loadStatus(), workerStore.fetchAll(), loadPoolLimits(), loadUsage(), loadEvents(), loadPricing(), loadHistory()])
}

// 进页 1 次 + 每 15s 轮询(组件/版本/吞吐/用量/事件)。WS 负责计数实时跳动。
let poll: number | undefined
onMounted(() => {
  refreshAll()
  poll = window.setInterval(() => {
    loadStatus(); loadUsage(); loadEvents(); loadPricing()
  }, 15000)
})
onUnmounted(() => { if (poll) window.clearInterval(poll) })

// 池上限编辑:恢复默认 + 0 值确认
const poolLimits = ref<Record<string, { default: number; override: number | null }>>({})
const limitDraft = ref<Record<string, number | null>>({})
const limitBusy = ref<string | null>(null)
async function loadPoolLimits() {
  try {
    poolLimits.value = await workerStore.fetchPoolLimits()
    limitDraft.value = Object.fromEntries(
      Object.entries(poolLimits.value).map(([k, v]) => [k, v.override ?? v.default]),
    )
  } catch { /* 非致命 */ }
}
async function saveOnePoolLimit(pool: string) {
  const val = limitDraft.value[pool]
  if (val === 0 && !confirm(`将 ${pool} 上限设为 0 会暂停该池，运行中的任务跑完后不再认领新任务，确定？`)) return
  limitBusy.value = pool
  try {
    await workerStore.savePoolLimits({ [pool]: val })
    await Promise.all([loadPoolLimits(), loadStatus()])
    showToast('上限已更新，即时生效', 'success')
  } catch {
    showToast('保存失败', 'error')
  } finally {
    limitBusy.value = null
  }
}
async function resetPoolLimit(pool: string) {
  limitBusy.value = pool
  try {
    await workerStore.savePoolLimits({ [pool]: null })
    await Promise.all([loadPoolLimits(), loadStatus()])
    showToast('已恢复默认', 'success')
  } catch {
    showToast('恢复失败', 'error')
  } finally {
    limitBusy.value = null
  }
}

// 组件 / 版本派生
const components = computed<SystemComponent[]>(() => status.value?.components ?? [])
const systemVersion = computed(() => status.value?.version || 'dev')
const minioComp = computed(() => components.value.find(c => c.kind === 'minio'))
const deployMode = computed(() => {
  const m = minioComp.value?.extra?.mode
  return m === 'remote' ? '分布式（对象存储）' : m === 'local' ? '单机（本地盘）' : '—'
})

// live 四段:WS 优先,回退轮询
const liveJobs = computed(() => systemStatus.value?.jobs ?? status.value?.jobs ?? null)
const livePools = computed(() => systemStatus.value?.pools ?? status.value?.pools ?? {})
const liveDisk = computed(() => systemStatus.value?.disk ?? status.value?.disk ?? null)
const throughput = computed(() => status.value?.throughput_1h ?? null)
const traffic = computed(() => status.value?.traffic ?? null)

// 通联 / 链路流量
const link = computed<LinkTraffic | null>(() => status.value?.link_traffic ?? null)
const hasLink = computed(() => !!link.value || workerStore.workers.some(w => w.remote_addr))
const fmtRate = (bps: number | undefined) => (bps && bps > 0 ? `${fmtBytes(bps)}/s` : '—')

// 选中拓扑节点(默认 ECS=隧道,数据最丰富);富时间线供按节点切片画趋势。
const selectedNode = ref('ecs')
const history = ref<Array<{ ts: number; gw?: any; tun?: any; t?: any; w?: any }>>([])
async function loadHistory() {
  try {
    history.value = await workerStore.fetchLinkTrafficHistory()
  } catch {
    history.value = []  // 无上报器/边缘离线 → 空,详情显「累积中」
  }
}

// 选中节点详情:累计 + 当前速率 + 近期速率序列(相邻样本累计差/dt)。节点语义不同:隧道=rx/tx,worker=pull/push。
const detail = computed(() => {
  const tl = [...history.value].reverse()  // 时间正序
  const rate = (pick: (s: any) => { a: number; b: number }) => {
    const d: number[] = [], u: number[] = []
    for (let i = 1; i < tl.length; i++) {
      const dt = (tl[i].ts - tl[i - 1].ts) || 1
      const A = pick(tl[i]), B = pick(tl[i - 1])
      d.push(Math.max(0, (A.a - B.a) / dt)); u.push(Math.max(0, (A.b - B.b) / dt))
    }
    return { d, u, peak: Math.max(...d, ...u, 1) }
  }
  const span = tl.length > 1 ? `${Math.max(1, Math.round((tl[tl.length - 1].ts - tl[0].ts) / 60))}min` : '—'
  const id = selectedNode.value
  if (id === 'ecs' || id === 'nas') {
    const r = rate(s => ({ a: s.tun?.rx ?? 0, b: s.tun?.tx ?? 0 }))
    return {
      title: id === 'nas' ? 'NAS · 经隧道收发(ECS↔NAS)' : 'ECS ↔ NAS 隧道', dl: '↓ 下行', ul: '↑ 上行', span,
      cum: `↓${fmtBytes(link.value?.tunnel.rx ?? 0)} ↑${fmtBytes(link.value?.tunnel.tx ?? 0)}`,
      rate: `↓${fmtRate(link.value?.tunnel.rx_bps)} ↑${fmtRate(link.value?.tunnel.tx_bps)}`,
      down: r.d, up: r.u, peak: r.peak, tunnels: link.value?.tunnel.tunnels ?? [], wid: '',
      linkDesc: link.value ? (link.value.tunnel.up ? '隧道 通' : '隧道 断') : '',
    }
  }
  if (id.startsWith('w:')) {
    const wid = id.slice(2)
    const w = workerStore.workers.find(x => x.id === wid)
    const r = rate(s => ({ a: s.w?.[wid]?.pull ?? 0, b: s.w?.[wid]?.push ?? 0 }))
    return {
      title: `worker · ${w?.hostname || wid}`, dl: '↓ 拉取(出库)', ul: '↑ 回传(入库)', span,
      cum: `↓${fmtBytes(w?.traffic?.pull ?? 0)} ↑${fmtBytes(w?.traffic?.push ?? 0)}`, rate: '',
      down: r.d, up: r.u, peak: r.peak, tunnels: [], wid,
      linkDesc: w?.remote_addr ? `远程·网关接入 ${w.remote_addr}` : '本地·直连',
    }
  }
  return null
})
// 速率序列 → SVG polyline points。down/up 共享峰值 max 以可比;x 等分 0..100,y 翻转留边。
function chartPoints(arr: number[], max: number): string {
  if (arr.length < 2) return ''
  const n = arr.length
  return arr.map((v, i) => `${((i / (n - 1)) * 100).toFixed(1)},${((1 - v / (max || 1)) * 25 + 1.5).toFixed(1)}`).join(' ')
}

// Worker 列表派生
const STATUS_ORDER: Record<string, number> = {
  'online-busy': 0, 'online-idle': 1, paused: 2, stale: 3, offline: 4,
}
const sortedWorkers = computed(() =>
  [...workerStore.workers].sort((a, b) => (STATUS_ORDER[a.status] ?? 5) - (STATUS_ORDER[b.status] ?? 5)),
)
const onlineCount = computed(() => workerStore.workers.filter(w => w.status.startsWith('online') || w.status === 'paused').length)
const busyCount = computed(() => workerStore.workers.filter(w => w.status === 'online-busy').length)
const doneCount = computed(() => liveJobs.value?.done ?? 0)
const pendingCount = computed(() => liveJobs.value?.pending ?? 0)
const pools = computed(() => Object.entries(livePools.value))
const dotClass = workerDotClass
const computeDesc = workerComputeDesc
function isOnline(w: Worker): boolean { return w.status.startsWith('online') || w.status === 'paused' }

// 池派生:占用 / 积压 / 无 worker 积压 / 暂停。
function poolDot(name: string, p: { capacity: number; used: number; queue: number }): string {
  if (p.capacity === 0) return 'd-mut'                          // 暂停
  const onlineForType = status.value?.workers?.[name]?.online ?? 0
  if (p.queue > 0 && onlineForType === 0) return 'd-bad'        // 无 worker 积压
  if (p.queue > 0 && p.used >= p.capacity) return 'd-warn'      // 满载积压
  return 'd-ok'
}
function poolQueueBadge(name: string, p: { capacity: number; used: number; queue: number }): { cls: string; text: string } {
  if (p.capacity === 0) return { cls: 'b-mut', text: `⏸ ${p.queue} 个任务等待` }
  const onlineForType = status.value?.workers?.[name]?.online ?? 0
  if (p.queue > 0 && onlineForType === 0) return { cls: 'b-bad', text: `⚠ ${p.queue} 个任务无 worker` }
  if (p.queue > 0) return { cls: 'b-warn', text: `▲ ${p.queue} 个任务积压` }
  return { cls: 'b-mut', text: `队列 ${p.queue} 任务` }
}

// 版本漂移(前端比对)
// 版本显示:FLORI_VERSION = "<语义版本>+<构建短sha>"(如 0.2.0+f1d86f0)。
// verSem→主显语义版本(v0.2.0;Redis 7.4.9→v7.4.9;dev 等原样);verBuild→构建 sha(f1d86f0)。
function verSem(v: string | null | undefined): string {
  const sem = (v || '').trim().split('+')[0]
  if (!sem) return '—'
  return /^\d/.test(sem) ? `v${sem}` : sem
}
function verBuild(v: string | null | undefined): string {
  const s = (v || '').trim()
  const i = s.indexOf('+')
  return i >= 0 ? s.slice(i + 1) : ''
}
function versionMatches(expected: string, actual: string | null | undefined): boolean {
  const e = (expected || '').trim().toLowerCase()
  const a = (actual || '').trim().toLowerCase()
  if (!e || !a) return true   // 缺基准/缺自报 → 不算漂移(不误报)
  const n = Math.min(e.length, a.length, 40)
  if (n < 7) return e === a
  return e.slice(0, n) === a.slice(0, n)
}
const driftEnabled = computed(() => {
  const v = systemVersion.value
  return !!v && v !== 'dev'
})
function workerDrifted(w: Worker): boolean {
  if (!driftEnabled.value) return false
  const wv = w.spec?.version
  if (!wv || wv === 'dev') return false
  return !versionMatches(systemVersion.value, wv)
}
const driftCount = computed(() => sortedWorkers.value.filter(workerDrifted).length)
const sameVersionCount = computed(() =>
  driftEnabled.value
    ? sortedWorkers.value.filter(w => w.spec?.version && w.spec.version !== 'dev' && !workerDrifted(w)).length
    : 0,
)

// worker 网关中转流量短文案:拉取/回传字节,均为 0 则不显。
function trafficText(w: Worker): string {
  const t = w.traffic
  if (!t) return ''
  const pull = t.pull ?? 0
  const push = t.push ?? 0
  if (pull <= 0 && push <= 0) return ''
  return `↓${fmtBytes(pull)} ↑${fmtBytes(push)}`
}

// worker live 负载短文案(cpu%/mem%/load)。
function loadText(w: Worker): string {
  const l = w.load
  if (!l) return ''
  const parts: string[] = []
  if (l.cpu_pct != null) parts.push(`CPU ${l.cpu_pct}%`)
  if (l.mem_pct != null) parts.push(`内存 ${l.mem_pct}%`)
  if (l.loadavg != null) parts.push(`负载 ${l.loadavg}`)
  return parts.join(' ')
}

// 刻意不做独立健康条,状态/告警就地呈现:失败/孤儿/worker 清理 → 系统事件圆点;
// 排队无 worker → 资源池卡告警;版本漂移 → Worker 区;组件 down/降级 → 核心组件卡。失败累计记 failStreak(供轮询)。

// 事件 kind→标签/点色/摘要 已统一到 utils/events(EventsView 共用)。

const diskBarColor = computed(() => {
  const pct = liveDisk.value?.used_pct ?? 0
  return pct > 90 ? 'var(--bad)' : pct >= 75 ? 'var(--warn)' : 'var(--ok)'
})

// 行内 暂停 / 继续 / 移除
const rowBusy = ref<string | null>(null)
async function togglePause(w: Worker) {
  rowBusy.value = w.id
  try {
    if (w.status === 'paused') { await workerStore.resume(w.id); showToast('已继续', 'success') }
    else { await workerStore.pause(w.id); showToast('已暂停，当前任务跑完后不再认领新任务', 'success') }
  } catch { showToast('操作失败', 'error') } finally { rowBusy.value = null }
}
async function removeWorker(w: Worker) {
  const online = isOnline(w)
  // 在线/paused 管理态需要 force,删除会吊销 per-worker token 并让旧连接快速失败。
  if (!confirm(online
    ? `移除 Worker ${w.id} 并吊销 worker token？该 worker 会停止接入；需要重新接入时请重新生成临时接入 token。`
    : `移除 Worker ${w.id} 并吊销 worker token？需要重新接入时请重新生成临时接入 token。`)) return
  rowBusy.value = w.id
  try { await workerStore.remove(w.id, online); showToast('已移除并吊销 worker token', 'success') }
  catch { showToast('移除失败', 'error') } finally { rowBusy.value = null }
}

// 接入新 Worker(mintToken + Gateway-only 命令;折叠 <details>)
// worker 镜像 = flori-worker,接入命令默认用它。
const DEFAULT_WORKER_IMAGE = `ghcr.io/${'gwzl' + 'chn'}/flori-worker:latest`
const IMAGE = import.meta.env.VITE_WORKER_IMAGE || DEFAULT_WORKER_IMAGE
const WORKER_TYPES = ['cpu', 'gpu', 'ai', 'io']
const OUTPUT_MODES = [
  { id: 'compose', label: 'compose(推荐)' },
  { id: 'docker', label: 'docker run' },
] as const
const ENROLL_KEY = 'flori.system.enroll.open'
const enrollOpen = ref(localStorage.getItem(ENROLL_KEY) === '1')
function onEnrollToggle(e: Event) {
  const open = (e.target as HTMLDetailsElement).open
  localStorage.setItem(ENROLL_KEY, open ? '1' : '0')
}

// 能力 = 订阅的资源池,可多选、无主次,命令生成 --pools。至少选一个。
const selectedPools = ref<string[]>(['cpu'])
// WORKER_NAME 基名:多池排序 join('-')(如 cpu-gpu),仅命名/展示用;排序=命令稳定不随勾选顺序抖。
const nameBase = computed(() => [...selectedPools.value].sort().join('-') || 'worker')
const workerName = computed(() => `${nameBase.value}-1`)
const newTags = ref('')
const outputMode = ref<(typeof OUTPUT_MODES)[number]['id']>('compose')
const token = ref('')
const tokenExpiresInSec = ref<number | null>(null)
const minting = ref(false)
const watchtowerEnabled = ref(false)
const watchtowerInterval = ref(120)
const stateDirTouched = ref(false)
function defaultStateDir(name: string): string { return `./flori-worker-state/${name}` }
const workerStateDir = ref(defaultStateDir(workerName.value))
watch(workerName, (name) => {
  if (!stateDirTouched.value) workerStateDir.value = defaultStateDir(name)
})
watch(watchtowerEnabled, (enabled) => {
  if (enabled) outputMode.value = 'compose'
})
const AI_CRED_METHODS = [
  { id: 'claude-sub', label: 'Claude 订阅(worker HOME)' },
  { id: 'anthropic', label: 'Anthropic API Key' },
  { id: 'deepseek', label: 'DeepSeek API Key' },
] as const
const aiCredMethod = ref<(typeof AI_CRED_METHODS)[number]['id']>('claude-sub')

const gatewayUrl = computed(() => {
  const o = typeof window !== 'undefined' ? window.location?.origin : ''
  return o && o.startsWith('http') ? o : 'https://<FLORI_HOST>'
})
const serviceName = computed(() => `flori-worker-${workerName.value}`)
const watchtowerScope = computed(() => serviceName.value)
const stateDir = computed(() => workerStateDir.value.trim() || defaultStateDir(workerName.value))
const updateIntervalSec = computed(() => {
  const n = Math.trunc(Number(watchtowerInterval.value))
  return Number.isFinite(n) && n > 0 ? n : 120
})
const needsCache = computed(() => selectedPools.value.some((t) => t === 'gpu' || t === 'cpu'))
const tokenLine = computed(() => token.value || 'flw-<生成临时接入 token 后填入>')
const tokenTtlText = computed(() => tokenExpiresInSec.value == null ? '' : fmtDuration(tokenExpiresInSec.value))

const dockerCredLines = computed(() => {
  // 按勾选的能力取并集:勾 ai→claude/API 凭证。多池同机全都要。
  let s = ''
  if (selectedPools.value.includes('ai')) {
    if (aiCredMethod.value === 'anthropic') s += '  -e ANTHROPIC_API_KEY=<KEY> \\\n'
    else if (aiCredMethod.value === 'deepseek') s += '  -e DEEPSEEK_API_KEY=<KEY> \\\n'
  }
  // io 无需任何凭证 env:B站/YouTube cookie 由中心在任务认领时自动下发(1.1.85 凭证分发)。
  return s
})
// whisper 在 cpu 池执行(GPU 机自动加速),cpu/gpu 都需要模型缓存;HF 两 env 是国内网络
// 实测坑(代理剥元数据头 + Xet CAS 401),海外机器带着也无害。
const dockerCacheLines = computed(() => (needsCache.value
  ? '  -v whisper-cache:/cache \\\n  -e MODEL_CACHE_DIR=/cache \\\n  -e HF_ENDPOINT=https://hf-mirror.com \\\n  -e HF_HUB_DISABLE_XET=1 \\\n' : ''))
const tagsArg = computed(() => {
  const t = newTags.value.split(/[\s,]+/).filter(Boolean)
  return t.length ? ` --tags ${t.join(' ')}` : ''
})
// 唯一能力表达:--pools <所勾选>。
const runCmd = computed(() => `python -m worker.main --pools ${[...selectedPools.value].sort().join(' ') || '<至少勾一个能力>'}${tagsArg.value}`)
const gpuFlag = computed(() => (selectedPools.value.includes('gpu') ? ' --gpus all' : ''))

function yamlString(v: string | number): string {
  return JSON.stringify(String(v))
}
function composeEnvLines(): string {
  const lines: Array<[string, string | number]> = [
    ['HOME', '/home/worker'],
    ['WORKER_NAME', workerName.value],
    ['WORKER_REGISTRATION_TOKEN', tokenLine.value],
    ['WORKER_ID_FILE', '/home/worker/worker.id'],
    ['WORKER_TOKEN_FILE', '/home/worker/worker.token'],
    ['GATEWAY_URL', gatewayUrl.value],
  ]
  if (needsCache.value) {
    lines.push(['MODEL_CACHE_DIR', '/cache'])
    lines.push(['HF_ENDPOINT', 'https://hf-mirror.com'])
    lines.push(['HF_HUB_DISABLE_XET', '1'])
  }
  if (selectedPools.value.includes('ai')) {
    if (aiCredMethod.value === 'anthropic') lines.push(['ANTHROPIC_API_KEY', '${ANTHROPIC_API_KEY:-}'])
    if (aiCredMethod.value === 'deepseek') lines.push(['DEEPSEEK_API_KEY', '${DEEPSEEK_API_KEY:-}'])
  }
  return lines.map(([k, v]) => `      ${k}: ${yamlString(v)}`).join('\n')
}
function composeVolumeLines(): string {
  const volumes = [`${stateDir.value}:/home/worker`]
  if (needsCache.value) volumes.push('whisper-cache:/cache')
  return volumes.map(v => `      - ${yamlString(v)}`).join('\n')
}
const composeCommand = computed(() => {
  const gpu = selectedPools.value.includes('gpu') ? '    gpus: all\n' : ''
  const labels = watchtowerEnabled.value
    ? `    labels:
      - "com.centurylinklabs.watchtower.enable=true"
      - "com.centurylinklabs.watchtower.scope=${watchtowerScope.value}"
`
    : ''
  const watchtower = watchtowerEnabled.value
    ? `
  watchtower-${workerName.value}:
    image: ghcr.io/containrrr/watchtower:latest
    container_name: watchtower-${serviceName.value}
    restart: unless-stopped
    command: "--label-enable --scope ${watchtowerScope.value} --cleanup --interval ${updateIntervalSec.value}"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock"
`
    : ''
  const topVolumes = needsCache.value ? '\nvolumes:\n  whisper-cache:\n' : ''
  return `services:
  ${serviceName.value}:
    image: ${IMAGE}
    container_name: ${serviceName.value}
    restart: unless-stopped
    command: ${yamlString(runCmd.value)}
${gpu}    environment:
${composeEnvLines()}
    volumes:
${composeVolumeLines()}
${labels}${watchtower}${topVolumes}`
})
const dockerRunCommand = computed(() => `docker run -d --name ${serviceName.value} --restart unless-stopped${gpuFlag.value} \\
  -e GATEWAY_URL=${gatewayUrl.value} \\
  -e WORKER_REGISTRATION_TOKEN=${tokenLine.value} \\
  -e WORKER_NAME=${workerName.value} \\
  -e WORKER_ID_FILE=/home/worker/worker.id \\
  -e WORKER_TOKEN_FILE=/home/worker/worker.token \\
  -e HOME=/home/worker \\
${dockerCredLines.value}${dockerCacheLines.value}  -v "${stateDir.value}:/home/worker" \\
  ${IMAGE} \\
  ${runCmd.value}`)
const command = computed(() => outputMode.value === 'compose' ? composeCommand.value : dockerRunCommand.value)
const commandTitle = computed(() => outputMode.value === 'compose' ? 'docker-compose.yml' : 'docker run')
const commandCopyLabel = computed(() => outputMode.value === 'compose' ? '复制 compose' : '复制命令')

async function mint() {
  minting.value = true
  try {
    const minted = await workerStore.mintToken()
    token.value = minted.token
    tokenExpiresInSec.value = minted.expires_in_sec
    showToast('已生成临时接入 token（仅此一次完整展示）', 'success')
  } catch { showToast('生成失败', 'error') } finally { minting.value = false }
}

const copiedToken = ref(false)
const copiedCmd = ref(false)
async function copy(text: string, which: 'token' | 'cmd') {
  try {
    await navigator.clipboard.writeText(text)
    if (which === 'token') { copiedToken.value = true; setTimeout(() => (copiedToken.value = false), 1800) }
    else { copiedCmd.value = true; setTimeout(() => (copiedCmd.value = false), 1800) }
    showToast('已复制', 'success')
  } catch { showToast('复制失败，请手动选择文本', 'error') }
}

// AI 用量:成本按 provider==claude-cli 标「(等价)」。
function costLabel(provider: string): string { return provider === 'claude-cli' ? '（等价）' : '' }
function fmtCost(v: number): string { return `$${(v ?? 0).toFixed(4)}` }

// 按 provider 分组(每个可点开看自己的统计;跨 provider 总计在顶部 4 块)。
const usageByProvider = computed(() => {
  const u = usage.value
  if (!u) return []
  const m = new Map<string, any>()
  for (const r of u.by_model) {
    let g = m.get(r.provider)
    if (!g) {
      g = { provider: r.provider, calls: 0, input: 0, output: 0, cc: 0, cr: 0, cost: 0, models: [] as any[] }
      m.set(r.provider, g)
    }
    g.calls += r.calls; g.input += r.input_tokens; g.output += r.output_tokens
    g.cc += r.cache_creation_tokens; g.cr += r.cache_read_tokens; g.cost += r.cost_usd
    g.models.push(r)
  }
  return [...m.values()]
    .map(g => ({ ...g, hit: (g.input + g.cc + g.cr) ? Math.round((g.cr / (g.input + g.cc + g.cr)) * 1000) / 10 : 0 }))
    .sort((a, b) => b.cost - a.cost)
})
</script>

<template>
  <section class="page">
    <!-- 页头 -->
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
      <div class="h1"><Server :size="18" />系统健康总览</div>
      <button v-if="!connected" class="badge b-warn" style="margin-left:auto" @click="reconnect">
        实时已断开 · 点此重连
      </button>
      <button class="btn sm" :style="connected ? 'margin-left:auto' : ''" :disabled="workerStore.loading" @click="refreshAll">
        <RefreshCw :size="13" :class="workerStore.loading ? 'spin' : ''" />刷新
      </button>
    </div>

    <!-- 带1 · 概览 -->
    <!-- 概览拆两组(同一种标签+值 cell):1. 系统 2. Worker·作业。系统在前。 -->
    <div class="seclabel" style="margin-bottom:8px"><Server :size="14" />系统</div>
    <div class="card pad statgrid" style="margin-bottom:16px">
      <div class="st-cell">
        <div class="st-lbl">版本</div>
        <div class="st-val" :title="systemVersion">系统 {{ verSem(systemVersion) }}<span v-if="verBuild(systemVersion)" class="dim"> · {{ verBuild(systemVersion) }}</span></div>
      </div>
      <div class="st-cell">
        <div class="st-lbl">部署</div>
        <div class="st-val">{{ deployMode }}</div>
      </div>
      <div class="st-cell">
        <div class="st-lbl"><HardDrive :size="11" />磁盘</div>
        <template v-if="liveDisk && liveDisk.total_gb >= 0">
          <div class="st-val">{{ liveDisk.used_gb }}/{{ liveDisk.total_gb }}GB <b :style="{ color: liveDisk.used_pct > 90 ? 'var(--bad)' : 'var(--ink-900)' }">{{ liveDisk.used_pct }}%</b><span class="dim" style="margin-left:6px">剩 {{ liveDisk.available_gb }}GB</span></div>
          <span class="track" style="margin-top:5px;max-width:240px"><span :style="{ width: `${Math.min(100, liveDisk.used_pct)}%`, background: diskBarColor }"></span></span>
        </template>
        <div v-else class="st-val dim">不可用</div>
      </div>
      <div class="st-cell" v-if="traffic && (traffic.pull_bytes > 0 || traffic.push_bytes > 0)">
        <div class="st-lbl" title="网关产物代理:出库=worker 拉取(NAS→worker) / 入库=回传(worker→NAS)">网关中转</div>
        <div class="st-val">出库 {{ fmtBytes(traffic.pull_bytes) }} · 入库 {{ fmtBytes(traffic.push_bytes) }}</div>
      </div>
    </div>

    <div class="seclabel" style="margin-bottom:8px"><Cpu :size="14" />Worker · 作业</div>
    <div class="card pad statgrid sg-worker" style="margin-bottom:18px">
      <div class="st-cell"><div class="st-lbl">Worker 在线 / 共</div><div class="st-val"><b>{{ onlineCount }} / {{ workerStore.workers.length }}</b></div></div>
      <div class="st-cell"><div class="st-lbl">忙碌 · 处理中</div><div class="st-val"><b>{{ busyCount }}</b></div></div>
      <div class="st-cell"><div class="st-lbl">待处理 · 队列</div><div class="st-val"><b>{{ pendingCount }}</b></div></div>
      <div class="st-cell"><div class="st-lbl">累计完成 · 吞吐</div><div class="st-val"><b>{{ doneCount }}</b></div></div>
      <div class="st-cell">
        <div class="st-lbl"><Database :size="11" />内容(作业)</div>
        <div class="st-val" v-if="liveJobs">共 {{ liveJobs.total }} · 处理中 {{ liveJobs.processing }} · 失败 <b :style="{ color: liveJobs.failed > 0 ? 'var(--bad)' : 'var(--ink-900)' }">{{ liveJobs.failed }}</b></div>
      </div>
      <div class="st-cell">
        <div class="st-lbl">近 1h</div>
        <div class="st-val">完成 {{ throughput?.done ?? 0 }} · 失败 {{ throughput?.failed ?? 0 }}</div>
      </div>
    </div>

    <!-- 最近事件:概览只摘 5 条,全部 → /system/events(可按类型/时间筛选)-->
    <div class="seclabel" style="margin-bottom:10px;display:flex;align-items:center">
      <AlertTriangle :size="14" />系统事件
      <span style="margin-left:auto;font-weight:400;font-size:11.5px;color:var(--brand-600);cursor:pointer;text-transform:none;letter-spacing:0" @click="router.push('/system/events')">查看全部 →</span>
    </div>
    <div class="card pad" style="margin-bottom:24px">
      <div v-if="events.length === 0" style="display:flex;align-items:center;gap:8px;color:var(--ink-500);font-size:13px">
        <span class="dot d-ok"></span>系统运行平稳，近期无告警
      </div>
      <div v-else class="list">
        <div v-for="(e, i) in events.slice(0, 5)" :key="i" style="display:flex;align-items:center;gap:9px;font-size:12.5px">
          <span class="dot" :class="eventDot(e.kind)"></span>
          <span style="color:var(--ink-500);min-width:64px">{{ fmtRelative(e.ts * 1000) }}</span>
          <b style="color:var(--ink-900)">{{ eventLabel(e.kind) }}</b>
          <span style="color:var(--ink-600);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ eventSummary(e) }}</span>
        </div>
      </div>
    </div>

    <!-- 带2 · 基础设施(核心组件 + 通联) -->
    <div class="seclabel" style="margin-bottom:12px"><Boxes :size="14" />核心组件 · {{ components.length }}</div>
    <div v-if="status === null && components.length === 0" class="grid2" style="margin-bottom:18px">
      <div v-for="n in 4" :key="n" class="card pad comp-card">
        <div class="sk-bar" style="height:14px;width:50%"></div>
        <div class="sk-bar" style="height:11px;width:70%"></div>
      </div>
    </div>
    <div v-else class="grid2" style="margin-bottom:18px">
      <ComponentCard v-for="c in components" :key="c.name" :comp="c" />
    </div>

    <!-- 通联 / 链路流量:远程 worker、ECS 网关、隧道、NAS 间链路 -->
    <div v-if="hasLink" class="seclabel" style="margin-bottom:12px"><Network :size="14" />通联 · 链路流量</div>
    <div v-if="hasLink" class="card pad" style="margin-bottom:24px">
      <LinkTopologyTree :workers="workerStore.workers" :link="link" :selected="selectedNode" @select="selectedNode = $event" />
      <!-- 选中节点详情:累计 + 当前速率 + 近期趋势(来自 /api/link-traffic/history 按节点切片)-->
      <div v-if="detail" class="tp-detail">
        <div class="tp-detail-h">
          {{ detail.title }}
          <span class="tp-detail-cum">{{ detail.cum }}<template v-if="detail.rate"> · 速率 {{ detail.rate }}</template></span>
          <span v-if="detail.linkDesc" class="tp-detail-tag">{{ detail.linkDesc }}</span>
          <span v-if="detail.wid" class="tp-detail-link" @click="router.push(`/workers/${detail.wid}`)">worker 详情 →</span>
        </div>
        <div v-if="detail.down.length > 1" class="tp-chart">
          <svg viewBox="0 0 100 28" preserveAspectRatio="none">
            <polyline :points="chartPoints(detail.down, detail.peak)" class="ch-d" />
            <polyline :points="chartPoints(detail.up, detail.peak)" class="ch-u" />
          </svg>
          <span class="tp-chart-leg"><i class="ch-dd"></i>{{ detail.dl }} <i class="ch-ud"></i>{{ detail.ul }}<span class="dim" style="margin-left:6px">近 {{ detail.span }}</span></span>
        </div>
        <div v-else class="tp-detail-empty">趋势数据累积中…(上报器每 20s 采样;需边缘在线)</div>
        <div v-if="detail.tunnels.length" class="tp-tunnels">
          <span class="tp-tn" v-for="t in detail.tunnels" :key="t.name" :title="t.fwd"><b>{{ t.name }}</b> ↓{{ fmtBytes(t.rx) }} ↑{{ fmtBytes(t.tx) }}</span>
        </div>
      </div>
    </div>

    <!-- 带3 · 算力与用量(用量在上,算力在下) -->
    <!-- AI 用量聚合 + LiteLLM 价表 -->
    <div v-if="usage && usage.calls > 0" class="card pad" style="margin-bottom:24px">
      <div class="card-h"><Coins :size="15" />AI 用量 · {{ usage.calls }} 次调用</div>
      <div class="grid4" style="margin-bottom:12px">
        <div class="metric"><div class="v">{{ usage.total_input_tokens.toLocaleString() }}</div><div class="l">输入 token</div></div>
        <div class="metric"><div class="v">{{ usage.total_output_tokens.toLocaleString() }}</div><div class="l">输出 token</div></div>
        <div class="metric"><div class="v">{{ usage.cache_hit_rate_pct }}%</div><div class="l">平均缓存命中</div></div>
        <div class="metric"><div class="v">{{ fmtCost(usage.total_cost_usd) }}</div><div class="l">累计成本</div></div>
      </div>
      <!-- 每个 provider 一行;多模型可点开看分模型,单模型平铺(不冗余展开) -->
      <div>
        <template v-for="p in usageByProvider" :key="p.provider">
          <div v-if="p.models.length === 1" class="prov-flat">
            <span class="badge b-mut">{{ p.provider }}</span>
            <b class="mono">{{ p.models[0].model }}</b>
            <span class="prov-meta">{{ p.calls }} 次 · 入 {{ p.input.toLocaleString() }} / 出 {{ p.output.toLocaleString() }} · 命中 {{ p.hit }}%</span>
            <span class="prov-cost">{{ fmtCost(p.cost) }}<span class="dim" style="font-size:11px">{{ costLabel(p.provider) }}</span></span>
          </div>
          <details v-else class="prov-group">
            <summary class="prov-sum">
              <span class="badge b-mut">{{ p.provider }}</span>
              <span class="prov-meta">{{ p.models.length }} 个模型 · {{ p.calls }} 次 · 命中 {{ p.hit }}%</span>
              <span class="prov-cost">{{ fmtCost(p.cost) }}<span class="dim" style="font-size:11px">{{ costLabel(p.provider) }}</span></span>
            </summary>
            <div class="prov-models">
              <div v-for="m in p.models" :key="m.model" class="prov-row">
                <b class="mono">{{ m.model }}</b>
                <span class="prov-meta">{{ m.calls }} 次 · 入 {{ m.input_tokens.toLocaleString() }} / 出 {{ m.output_tokens.toLocaleString() }} · 命中 {{ m.cache_hit_rate_pct }}%</span>
                <span class="prov-cost">{{ fmtCost(m.cost_usd) }}</span>
              </div>
            </div>
          </details>
        </template>
      </div>
      <!-- LiteLLM 价表:模型数 + 更新时间 + 手动更新 + 看原始 JSON -->
      <div v-if="pricing" class="pricing-row">
        <span class="badge b-mut">LiteLLM 价表</span>
        <span class="prov-meta">{{ pricing.model_count }} 模型 · 更新于 {{ pricing.fetched_at ? fmtRelative(pricing.fetched_at) : '从未' }}</span>
        <button class="btn sm" :disabled="pricingBusy" style="margin-left:auto" @click="doRefreshPricing">
          <RefreshCw :size="12" :class="pricingBusy ? 'spin' : ''" />手动更新
        </button>
        <button class="btn sm" @click="openPricingRaw"><Braces :size="12" />原始 JSON</button>
      </div>
    </div>

    <!-- 资源池 -->
    <div class="seclabel" style="margin-bottom:12px;display:flex;align-items:center">
      <Layers :size="14" />资源池 · {{ pools.length }}
      <span style="margin-left:auto;font-weight:400;font-size:11.5px;color:var(--brand-600);cursor:pointer;text-transform:none;letter-spacing:0"
        @click="router.push('/system/queue')">查看队列 →</span>
    </div>
    <div class="grid3" style="margin-bottom:24px">
      <div v-for="[name, p] in pools" :key="name" class="card pad" style="padding:13px 15px">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:8px">
          <span class="dot" :class="poolDot(name, p)"></span>
          <b class="mono" style="font-size:13px;color:var(--ink-900);flex:1">{{ name }}</b>
          <span class="badge" :class="poolQueueBadge(name, p).cls" style="cursor:pointer"
            title="查看该池队列" @click="router.push(`/system/queue?pool=${encodeURIComponent(name)}`)">{{ poolQueueBadge(name, p).text }}</span>
        </div>
        <div class="dim-g">
          <div class="row-l"><span>在跑任务</span><b>{{ p.used }} / {{ p.capacity === 0 ? '暂停' : p.capacity }}</b></div>
          <div class="track"><span :style="{ width: `${Math.min(100, p.capacity ? (p.used / p.capacity) * 100 : 0)}%` }"></span></div>
        </div>
        <div v-if="name in limitDraft" style="display:flex;align-items:center;gap:6px;margin-top:9px;flex-wrap:wrap">
          <span style="font-size:11px;color:var(--ink-600)">上限</span>
          <input v-model.number="limitDraft[name]" type="number" min="0" class="input"
            style="width:64px;padding:3px 7px;font-size:12px"
            :placeholder="String(poolLimits[name]?.default ?? '')" />
          <button class="btn sm" :disabled="limitBusy === name" @click="saveOnePoolLimit(name)">
            {{ limitBusy === name ? '…' : '保存' }}
          </button>
          <button v-if="poolLimits[name]?.override != null" class="btn sm" :disabled="limitBusy === name" @click="resetPoolLimit(name)">默认</button>
          <span style="font-size:11px" :style="{ color: poolLimits[name]?.override == null ? 'var(--ink-400)' : 'var(--brand,#7c3aed)' }">
            {{ poolLimits[name]?.override == null ? '默认' : '已覆盖' }}
          </span>
        </div>
      </div>
    </div>

    <!-- Worker 信息 -->
    <div class="seclabel" style="margin-bottom:12px">
      <Cpu :size="14" />Worker · {{ workerStore.workers.length }}
      <template v-if="driftEnabled">
        <span class="sep" style="margin:0 6px;color:var(--ink-300)">·</span>
        <span style="font-weight:500;text-transform:none;letter-spacing:0">系统版本 <b class="mono">{{ verSem(systemVersion) }}</b></span>
        <span v-if="sameVersionCount > 0" style="font-weight:500;color:var(--ok);text-transform:none;letter-spacing:0"> · ✓{{ sameVersionCount }} 同版</span>
        <span v-if="driftCount > 0" style="font-weight:500;color:var(--warn);text-transform:none;letter-spacing:0"> · ▲{{ driftCount }} 版本漂移</span>
      </template>
    </div>
    <div v-if="workerStore.workers.length === 0 && pendingCount > 0" style="margin-bottom:8px">
      <span class="badge b-warn">{{ pendingCount }} 个任务在排队，但无可用 worker</span>
    </div>
    <div v-else-if="workerStore.workers.length === 0" style="margin-bottom:8px">
      <span class="badge b-mut">0 个 worker 在线 · 任务将排队等待算力</span>
    </div>

    <!-- 接入新 Worker(折叠) -->
    <details class="card pad worker-enroll" style="margin-bottom:18px" :open="enrollOpen" @toggle="onEnrollToggle">
      <summary class="card-h enroll-summary" style="margin-bottom:0;cursor:pointer;list-style:none">
        <span><Plus :size="15" />接入新 Worker</span>
        <span class="enroll-summary-meta">{{ selectedPools.length ? `能力 ${[...selectedPools].sort().join(' + ')}` : '未选择能力' }} · {{ commandTitle }}</span>
      </summary>
      <div class="enroll-panel">
        <div class="enroll-steps">
          <div class="enroll-step">
            <span class="step-dot">1</span>
            <div>
              <b>选择能力</b>
              <span>{{ selectedPools.length ? [...selectedPools].sort().join(' / ') : '至少选一个' }}</span>
            </div>
          </div>
          <div class="enroll-step">
            <span class="step-dot">2</span>
            <div>
              <b>生成 token</b>
              <span>{{ token ? `有效期 ${tokenTtlText || '已生成'}` : '首次注册用' }}</span>
            </div>
          </div>
          <div class="enroll-step">
            <span class="step-dot">3</span>
            <div>
              <b>复制部署文件</b>
              <span>{{ serviceName }}</span>
            </div>
          </div>
        </div>

        <div class="enroll-grid">
          <section class="enroll-box">
            <div class="enroll-box-h">
              <span>能力</span>
              <code class="mono">{{ runCmd }}</code>
            </div>
            <div class="pool-picker">
              <label v-for="t in WORKER_TYPES" :key="t" :class="{ on: selectedPools.includes(t) }">
                <input type="checkbox" :value="t" v-model="selectedPools" />
                <span>{{ t }}</span>
              </label>
            </div>
            <div v-if="selectedPools.includes('ai')" class="inline-field">
              <span>AI 凭证</span>
              <select v-model="aiCredMethod" class="input">
                <option v-for="m in AI_CRED_METHODS" :key="m.id" :value="m.id">{{ m.label }}</option>
              </select>
            </div>
            <p v-if="selectedPools.includes('ai')" class="note-tip">
              <template v-if="aiCredMethod === 'claude-sub'">使用持久状态目录内的 .claude。</template>
              <template v-else>部署时把 provider key 写入环境变量。</template>
            </p>
          </section>

          <section class="enroll-box">
            <div class="enroll-box-h">
              <span>接入 token</span>
              <span v-if="tokenTtlText" class="badge b-mut">有效期 {{ tokenTtlText }}</span>
            </div>
            <button class="btn pri enroll-main-action" :disabled="minting" @click="mint">
              <Key :size="14" />{{ token ? '重新生成 token' : '生成 token' }}
            </button>
            <div v-if="token" class="token-row">
              <code class="mono">{{ token }}</code>
              <button class="iconbtn" @click="copy(token, 'token')">
                <component :is="copiedToken ? Check : Copy" :size="15" />
              </button>
            </div>
            <p v-else class="note-tip">注册成功后长期 token 会写入状态目录。</p>
          </section>
        </div>

        <details class="advanced-box">
          <summary>高级选项</summary>
          <div class="advanced-grid">
            <div class="field">
              <label>Worker 名称</label>
              <input :value="workerName" class="input" disabled />
            </div>
            <div class="field">
              <label>标签</label>
              <input v-model="newTags" class="input" placeholder="home-desktop vision" />
            </div>
            <div class="field">
              <label>状态目录</label>
              <input
                v-model="workerStateDir"
                class="input"
                :placeholder="defaultStateDir(workerName)"
                @input="stateDirTouched = true"
              />
            </div>
            <div class="field">
              <label>部署形式</label>
              <div class="seg">
                <button
                  v-for="m in OUTPUT_MODES"
                  :key="m.id"
                  :class="{ on: outputMode === m.id }"
                  :disabled="watchtowerEnabled && m.id === 'docker'"
                  @click="outputMode = m.id"
                >
                  {{ m.label }}
                </button>
              </div>
            </div>
            <div class="field">
              <label>自动更新</label>
              <label class="checkline">
                <input data-testid="watchtower-enabled" type="checkbox" v-model="watchtowerEnabled" />
                Watchtower
              </label>
            </div>
            <div class="field">
              <label>更新间隔</label>
              <div class="inline-number">
                <input
                  v-model.number="watchtowerInterval"
                  data-testid="watchtower-interval"
                  type="number"
                  min="1"
                  class="input"
                  :disabled="!watchtowerEnabled"
                />
                <span>秒</span>
              </div>
            </div>
          </div>
          <p class="note-tip">Watchtower 会挂载 Docker socket。自签证书部署时可加 <code>GATEWAY_TLS_INSECURE=1</code> 或 <code>GATEWAY_CA_BUNDLE</code>。</p>
        </details>

        <section class="deploy-box">
          <div class="deploy-head">
            <div>
              <b>{{ commandTitle }}</b>
              <span>{{ serviceName }} · Gateway {{ gatewayUrl }}</span>
            </div>
            <button class="btn sm" @click="copy(command, 'cmd')">
              <component :is="copiedCmd ? Check : Copy" :size="13" />{{ copiedCmd ? '已复制' : commandCopyLabel }}
            </button>
          </div>
          <pre>{{ command }}</pre>
        </section>
      </div>
    </details>

    <!-- worker 状态卡片 -->

    <div v-if="workerStore.loading && workerStore.workers.length === 0" class="card pad" style="color:var(--ink-500);font-size:13px;margin-bottom:24px">
      加载中…
    </div>
    <div v-else-if="workerStore.workers.length === 0" class="card pad"
      style="margin-bottom:24px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:36px 18px">
      <Cpu :size="40" :stroke-width="1" style="color:var(--ink-300)" />
      <div style="font-size:14px;color:var(--ink-700);font-weight:600">还没有接入任何 Worker</div>
      <div class="lead" style="max-width:360px">在上方「接入新 Worker」生成临时接入 token，按 Gateway HTTPS 命令在任意机器上拉起一个 worker 即可。</div>
    </div>
    <div v-else class="list" style="margin-bottom:24px">
      <div
        v-for="w in sortedWorkers"
        :key="w.id"
        class="card pad wcard"
        :class="{ off: !isOnline(w) }"
        @click="router.push(`/system/workers/${encodeURIComponent(w.id)}`)"
      >
        <span class="dot" :class="[dotClass(w.status), { pulse: w.status === 'online-busy' }]"></span>
        <div class="wcard-main">
          <div class="wcard-hd">
            <b class="mono wcard-id">{{ w.id }}</b>
            <StatusBadge :status="w.status" />
            <span class="badge b-mut">{{ w.type.toUpperCase() }}</span>
            <span v-if="w.status === 'online-busy' && w.current_step" class="badge b-run">
              当前任务 {{ w.current_step }}
              <span v-if="w.current_job" class="mono">@ {{ w.current_job }}</span>
            </span>
            <span v-if="workerDrifted(w)" class="badge b-warn"
              :title="`期望 ${systemVersion}，当前 ${w.spec?.version}`">
              旧版本 {{ verSem(w.spec?.version) }}<span v-if="verBuild(w.spec?.version)">·{{ verBuild(w.spec?.version) }}</span>
            </span>
          </div>
          <div class="wcard-stats">
            <span class="wstat"><b>{{ w.tasks_completed }}</b>完成</span>
            <span class="wstat"><b :class="{ bad: w.tasks_failed > 0 }">{{ w.tasks_failed }}</b>失败</span>
            <span class="wstat"><b>{{ w.concurrency }}</b>并发</span>
            <span v-if="loadText(w)" class="wload">{{ loadText(w) }}</span>
          </div>
          <div class="wcard-sub">
            <span v-if="w.hostname">{{ w.hostname }}</span>
            <span v-if="w.hostname" class="sep">·</span>
            <span>{{ computeDesc(w) }}</span>
            <span class="sep">·</span>
            <span :title="w.spec?.version || '未上报版本(多为旧镜像 worker)'"
              :style="workerDrifted(w) ? 'color:var(--warn)' : ''">{{ w.spec?.version ? verSem(w.spec?.version) : '版本未报' }}</span>
            <template v-if="w.total_duration_sec > 0"><span class="sep">·</span><span>运行 {{ fmtDuration(w.total_duration_sec) }}</span></template>
            <template v-if="trafficText(w)"><span class="sep">·</span><span title="网关中转:拉取产物 / 回传产物">中转 {{ trafficText(w) }}</span></template>
            <span class="sep">·</span><span>心跳 {{ fmtRelative(w.last_heartbeat) }}</span>
          </div>
        </div>
        <template v-if="isOnline(w)">
          <button class="btn sm" :disabled="rowBusy === w.id" @click.stop="togglePause(w)">
            <Play v-if="w.status === 'paused'" :size="13" /><Pause v-else :size="13" />{{ w.status === 'paused' ? '继续' : '暂停' }}
          </button>
          <button class="btn sm" @click.stop="router.push(`/system/workers/${encodeURIComponent(w.id)}`)">
            <MessageSquare :size="13" />备注
          </button>
        </template>
        <!-- 移除在所有卡片可用(在线=强制移除);离线只显移除 -->
        <button class="btn sm danger" :disabled="rowBusy === w.id" @click.stop="removeWorker(w)">
          <X :size="13" />移除
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
summary::-webkit-details-marker { display: none; }
.seg button:disabled { opacity: .45; cursor: not-allowed; }

/* Worker 接入向导:默认只暴露能力、token、部署文件,高级项折叠。 */
.enroll-summary { justify-content: space-between; gap: 12px; }
.enroll-summary > span:first-child { display: inline-flex; align-items: center; gap: 7px; }
.enroll-summary-meta { font-size: 12px; font-weight: 500; color: var(--ink-500); white-space: nowrap; }
.worker-enroll { scroll-margin-top: 72px; }
.enroll-panel { margin-top: 14px; }
.enroll-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.enroll-step { display: flex; align-items: center; gap: 9px; min-width: 0; padding: 10px 12px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); background: var(--surface-2, #fafafa); }
.enroll-step b { display: block; font-size: 12.5px; color: var(--ink-800); line-height: 1.2; }
.enroll-step span:last-child { display: block; min-width: 0; margin-top: 2px; font-size: 11.5px; color: var(--ink-500); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-dot { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; flex: none; border-radius: 50%; background: var(--brand-50); color: var(--brand-700); font-size: 12px; font-weight: 700; }
.enroll-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(280px, .85fr); gap: 12px; margin-bottom: 12px; }
.enroll-box { min-width: 0; padding: 14px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface); }
.enroll-box-h { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 11px; font-size: 13px; font-weight: 700; color: var(--ink-800); }
.enroll-box-h code { min-width: 0; max-width: 64%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11.5px; font-weight: 500; color: var(--ink-500); }
.pool-picker { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.pool-picker label { display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-600); font-size: 12.5px; font-weight: 700; cursor: pointer; user-select: none; }
.pool-picker label.on { border-color: var(--brand-300); background: var(--brand-50); color: var(--brand-700); }
.pool-picker input { width: 13px; height: 13px; margin: 0; }
.inline-field { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 9px; margin-top: 11px; }
.inline-field > span { font-size: 12px; color: var(--ink-500); white-space: nowrap; }
.inline-field .input { padding: 6px 9px; font-size: 12px; }
.enroll-main-action { width: 100%; justify-content: center; min-height: 36px; }
.token-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; align-items: center; margin-top: 10px; }
.token-row code { min-width: 0; padding: 7px 9px; border-radius: var(--r-sm); background: var(--line-soft); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.advanced-box { margin: 2px 0 12px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); background: var(--surface); }
.advanced-box > summary { cursor: pointer; padding: 10px 12px; font-size: 12.5px; font-weight: 700; color: var(--ink-700); }
.advanced-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 0 12px 12px; }
.advanced-grid .field { margin: 0; }
.advanced-grid .input { padding: 7px 9px; font-size: 12px; }
.checkline { display: flex !important; align-items: center; gap: 7px; margin: 0 !important; font-weight: 500 !important; cursor: pointer; }
.inline-number { display: flex; align-items: center; gap: 8px; }
.inline-number .input { width: 92px; }
.inline-number span { font-size: 12px; color: var(--ink-500); }
.advanced-box .note-tip { margin: -2px 12px 12px; line-height: 1.6; }
.deploy-box { border: 1px solid var(--line); border-radius: var(--r-sm); overflow: hidden; }
.deploy-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 12px; background: var(--line-soft); border-bottom: 1px solid var(--line); }
.deploy-head b { display: block; font-size: 13px; color: var(--ink-800); }
.deploy-head span { display: block; max-width: 680px; margin-top: 2px; font-size: 11.5px; color: var(--ink-500); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.deploy-box pre { margin: 0; max-height: 300px; padding: 12px; overflow: auto; background: var(--ink-900); color: #cbd5e1; font-family: var(--mono); font-size: 12px; line-height: 1.65; white-space: pre-wrap; word-break: break-all; }
@media (max-width: 900px) {
  .enroll-steps, .enroll-grid, .advanced-grid { grid-template-columns: 1fr; }
  .pool-picker { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .deploy-head { align-items: stretch; flex-direction: column; }
  .deploy-head .btn { justify-content: center; }
}
@media (max-width: 560px) {
  .enroll-summary { align-items: flex-start; flex-direction: column; }
  .enroll-summary-meta { white-space: normal; }
}

/* 通联 / 链路流量:选中节点详情面板(树本身样式在 LinkTopologyTree.vue) */
.tp-detail { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--line-soft); }
.tp-detail-h { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; font-size: 13px; font-weight: 600; color: var(--ink-800); }
.tp-detail-cum { font-weight: 400; font-size: 12px; color: var(--ink-500); font-variant-numeric: tabular-nums; }
.tp-detail-tag, .tp-detail .tp-detail-h > .tp-detail-tag { font-weight: 400; font-size: 11px; color: var(--ink-400); }
.tp-detail-link { font-weight: 400; font-size: 11.5px; color: var(--brand-600); cursor: pointer; margin-left: auto; }
.tp-detail-link:hover { text-decoration: underline; }
.tp-detail-empty { font-size: 11.5px; color: var(--ink-400); margin-top: 8px; }
.tp-chart { display: flex; align-items: center; gap: 10px; margin-top: 9px; }
.tp-chart svg { width: 220px; height: 30px; flex: none; }
.tp-chart polyline { fill: none; stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.ch-d { stroke: var(--brand-500); }
.ch-u { stroke: var(--warn); }
.tp-chart-leg { font-size: 10.5px; color: var(--ink-400); display: inline-flex; align-items: center; gap: 5px; }
.tp-chart-leg i { width: 9px; height: 2.5px; border-radius: 1px; display: inline-block; }
.tp-chart-leg .ch-dd { background: var(--brand-500); }
.tp-chart-leg .ch-ud { background: var(--warn); }
.tp-tunnels { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; font-size: 11.5px; color: var(--ink-500); }
.tp-tn { font-variant-numeric: tabular-nums; }
.tp-tn b { color: var(--ink-700); font-weight: 600; }

/* 系统状态标签化网格 */
/* 固定列网格(两组共用同一列结构 → 系统/Worker 列对齐);长值在 cell 内换行,不挤不截断。 */
.statgrid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px 24px; align-items: start; }
@media (max-width: 900px) { .statgrid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 560px) { .statgrid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.st-cell { min-width: 0; }
.st-lbl { display: flex; align-items: center; gap: 4px; font-size: 10.5px; color: var(--ink-400); letter-spacing: .03em; margin-bottom: 3px; }
.st-val { font-size: 13px; color: var(--ink-800); font-variant-numeric: tabular-nums; line-height: 1.35; word-break: break-word; }
</style>
