<script setup lang="ts">
// 系统健康总览页(/system)。三带(自上而下):
//  带1 概览:概览指标 + 系统状态行(整体版本/部署/磁盘/内容/吞吐/中转) + 最近 5 事件
//  带2 基础设施:核心组件(api/scheduler/redis/minio) + 通联/链路拓扑
//  带3 算力与用量:上半 AI 用量 + 价表,下半 资源池 + Worker 列表 + 接入新 worker 折叠
// worker 接入在本页(运维);MCP 接入卡在 /settings(用户集成)。事件全量在 /system/events(类型/时间筛选)。
// 双通道:WS 每 2s 推 live 子集(计数/忙闲/队列/磁盘跳动);HTTP /api/status + /api/usage +
// /api/events 进页 1 次 + 每 15s 轮询(组件/版本/吞吐/用量/事件,慢变量)+ 手动刷新。
import { ref, computed, inject, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkerStore } from '../stores/workers'
import { useSystemDashboard } from '../composables/useSystemDashboard'
import ComponentCard from '../components/system/ComponentCard.vue'
import SystemReadinessPanel from '../components/system/dashboard/SystemReadinessPanel.vue'
import SystemOverviewPanel from '../components/system/dashboard/SystemOverviewPanel.vue'
import SystemEventsPanel from '../components/system/dashboard/SystemEventsPanel.vue'
import SystemLinkPanel from '../components/system/dashboard/SystemLinkPanel.vue'
import SystemUsagePanel from '../components/system/dashboard/SystemUsagePanel.vue'
import SystemPoolsPanel from '../components/system/dashboard/SystemPoolsPanel.vue'
import SystemWorkersPanel from '../components/system/dashboard/SystemWorkersPanel.vue'
import SystemEnrollmentPanel from '../components/system/dashboard/SystemEnrollmentPanel.vue'
import { fmtDuration } from '../utils/datetime'
import { fmtBytes } from '../utils/format'
import { workerDotClass, workerComputeDesc } from '../utils/worker'
import type { Worker, SystemComponent, LinkTraffic } from '../types'
import {
  Server, RefreshCw, Boxes,
} from 'lucide-vue-next'

const router = useRouter()
const workerStore = useWorkerStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const {
  status, usage, events, pricing, pricingBusy, poolLimits, limitDraft, history,
  systemStatus, connected, reconnect, refreshAll, loadStatus, loadPoolLimits,
  refreshPricing: doRefreshPricing, statusFetchFailed,
} = useSystemDashboard()

function openPricingRaw() { window.open('/api/pricing/raw', '_blank') }

// 池上限编辑:恢复默认 + 0 值确认
const limitBusy = ref<string | null>(null)
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
const readiness = computed(() => status.value?.health ?? null)
const hiddenReadinessReasonCount = computed(() => Math.max(0, (readiness.value?.reasons.length ?? 0) - 4))
const readinessLabel = computed(() => {
  if (statusFetchFailed.value) return '健康状态获取失败'
  if (!readiness.value) return '健康状态采集中'
  if (!readiness.value.ready) return '暂不可安全接单'
  return readiness.value.degraded ? '可接单，部分能力降级' : '可安全接单'
})
const readinessClass = computed(() => {
  if (statusFetchFailed.value) return 'rd-bad'
  if (!readiness.value) return 'rd-unknown'
  if (!readiness.value.ready) return 'rd-bad'
  return readiness.value.degraded ? 'rd-warn' : 'rd-ok'
})
const systemVersion = computed(() => status.value?.version || 'dev')
const frontendVersion = (import.meta.env.VITE_FLORI_VERSION || 'dev').trim()
const frontendBuildSha = (import.meta.env.VITE_FLORI_BUILD_SHA || '').trim().slice(0, 12)
const frontendFullVersion = computed(() => frontendBuildSha ? `${frontendVersion}+${frontendBuildSha}` : frontendVersion)
const versionSummaryTitle = computed(() => `系统 ${systemVersion.value} / 前端 ${frontendFullVersion.value}`)
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

const selectedNode = ref<string | null>(null)

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
  if (id?.startsWith('w:')) {
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
const WORKER_TYPES = ['ai', 'cpu', 'gpu', 'io']
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

// 能力 = worker 声明的资源池,可多选、无主次,命令生成 --pools。至少选一个。
const selectedPools = ref<string[]>(['cpu'])
// WORKER_NAME 基名:多池排序 join('-')(如 cpu-gpu),仅命名/展示用;排序=命令稳定不随勾选顺序抖。
const nameBase = computed(() => [...selectedPools.value].sort().join('-') || 'worker')
const nameTouched = ref(false)
const workerNameDraft = ref(`${nameBase.value}-1`)
watch(nameBase, (base) => {
  if (!nameTouched.value) workerNameDraft.value = `${base}-1`
})
const workerName = computed(() => workerNameDraft.value.trim() || `${nameBase.value}-1`)
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
const AI_ACCESS_METHODS = [
  { id: 'claude-cli', label: 'Claude CLI', tags: ['claude-cli', 'read'] },
  { id: 'codex-cli', label: 'Codex CLI', tags: ['codex-cli'] },
  { id: 'kimi-api', label: 'Kimi API key', tags: ['kimi-api'] },
] as const
const aiAccessMethod = ref<(typeof AI_ACCESS_METHODS)[number]['id']>('claude-cli')
const selectedAiAccess = computed(() =>
  AI_ACCESS_METHODS.find((m) => m.id === aiAccessMethod.value) ?? AI_ACCESS_METHODS[0],
)

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
  // 按勾选的能力取并集:勾 ai→接入方式凭证。多池同机全都要。
  let s = ''
  if (selectedPools.value.includes('ai')) {
    if (aiAccessMethod.value === 'kimi-api') s += '  -e KIMI_API_KEY=<KEY> \\\n'
  }
  // io 无需任何凭证 env:B站/YouTube cookie 由中心在任务认领时自动下发(1.1.85 凭证分发)。
  return s
})
// whisper 在 cpu 池执行(GPU 机自动加速),cpu/gpu 都需要模型缓存;HF 两 env 是国内网络
// 实测坑(代理剥元数据头 + Xet CAS 401),海外机器带着也无害。
const dockerCacheLines = computed(() => (needsCache.value
  ? '  -v whisper-cache:/cache \\\n  -e MODEL_CACHE_DIR=/cache \\\n  -e HF_ENDPOINT=https://hf-mirror.com \\\n  -e HF_HUB_DISABLE_XET=1 \\\n' : ''))
const tagsArg = computed(() => {
  const auto = selectedPools.value.includes('ai') ? [...selectedAiAccess.value.tags] : []
  const manual = newTags.value.split(/[\s,]+/).filter(Boolean)
  const t = [...new Set([...auto, ...manual])].sort()
  return t.length ? ` --tags ${t.join(' ')}` : ''
})
// 唯一能力表达:--pools <所勾选>。
const runCmd = computed(() => `python -m worker.main --pools ${[...selectedPools.value].sort().join(' ') || '<至少勾一个能力>'}${tagsArg.value}`)
const gpuFlag = computed(() => (selectedPools.value.includes('gpu') ? ' --gpus all' : ''))
const dockerWatchtowerLabels = computed(() => (watchtowerEnabled.value
  ? `  --label "com.centurylinklabs.watchtower.enable=true" \\
  --label "com.centurylinklabs.watchtower.scope=${watchtowerScope.value}" \\
`
  : ''))

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
    if (aiAccessMethod.value === 'kimi-api') lines.push(['KIMI_API_KEY', '${KIMI_API_KEY:?set KIMI_API_KEY}'])
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
${dockerWatchtowerLabels.value}  -e GATEWAY_URL=${gatewayUrl.value} \\
  -e WORKER_REGISTRATION_TOKEN=${tokenLine.value} \\
  -e WORKER_NAME=${workerName.value} \\
  -e WORKER_ID_FILE=/home/worker/worker.id \\
  -e WORKER_TOKEN_FILE=/home/worker/worker.token \\
  -e HOME=/home/worker \\
${dockerCredLines.value}${dockerCacheLines.value}  -v "${stateDir.value}:/home/worker" \\
  ${IMAGE} \\
  ${runCmd.value}${watchtowerEnabled.value ? `

docker run -d --name watchtower-${serviceName.value} --restart unless-stopped \\
  -v /var/run/docker.sock:/var/run/docker.sock \\
  ghcr.io/containrrr/watchtower:latest \\
  --label-enable --scope ${watchtowerScope.value} --cleanup --interval ${updateIntervalSec.value}` : ''}`)
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

    <SystemReadinessPanel :readiness="readiness" :failed="statusFetchFailed" :label="readinessLabel"
      :panel-class="readinessClass" :hidden-reason-count="hiddenReadinessReasonCount" />

    <!-- 带1 · 概览 -->
    <!-- 概览拆两组(同一种标签+值 cell):1. 系统 2. Worker·作业。系统在前。 -->
    <SystemOverviewPanel :version-title="versionSummaryTitle" :system-version="systemVersion" :frontend-version="frontendVersion"
      :deploy-mode="deployMode" :disk="liveDisk" :disk-bar-color="diskBarColor" :traffic="traffic"
      :online-count="onlineCount" :worker-count="workerStore.workers.length" :busy-count="busyCount"
      :pending-count="pendingCount" :done-count="doneCount" :jobs="liveJobs" :throughput="throughput" :version-label="verSem" />

    <!-- 最近事件:概览只摘 5 条,全部 → /system/events(可按类型/时间筛选)-->
    <SystemEventsPanel :events="events" @open-all="router.push('/system/events')" />

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
    <SystemLinkPanel v-if="hasLink" :workers="workerStore.workers" :link="link" :selected="selectedNode"
      :detail="detail" :points="chartPoints" @select="selectedNode = $event"
      @open-worker="router.push(`/system/workers/${encodeURIComponent($event)}`)" />

    <!-- 带3 · 算力与用量(用量在上,算力在下) -->
    <!-- AI 用量聚合 + LiteLLM 价表 -->
    <SystemUsagePanel v-if="usage && usage.calls > 0"
      :usage="usage" :groups="usageByProvider" :pricing="pricing" :pricing-busy="pricingBusy"
      @refresh-pricing="doRefreshPricing" @open-pricing="openPricingRaw" />

    <!-- 资源池 -->
    <SystemPoolsPanel :pools="pools" :limits="poolLimits" :draft="limitDraft" :busy="limitBusy"
      :dot="poolDot" :badge="poolQueueBadge"
      @open-queue="router.push($event ? `/system/queue?pool=${encodeURIComponent($event)}` : '/system/queue')"
      @save="saveOnePoolLimit" @reset="resetPoolLimit" @update-draft="(name, value) => limitDraft[name] = value" />

    <SystemWorkersPanel :workers="workerStore.workers" :sorted-workers="sortedWorkers" :loading="workerStore.loading"
      :pending-count="pendingCount" :drift-enabled="driftEnabled" :system-version="systemVersion"
      :same-version-count="sameVersionCount" :drift-count="driftCount" :row-busy="rowBusy"
      :is-online="isOnline" :dot-class="dotClass" :compute-desc="computeDesc" :drifted="workerDrifted"
      :version-label="verSem" :version-build="verBuild" :load-text="loadText" :traffic-text="trafficText"
      @open="router.push(`/system/workers/${encodeURIComponent($event)}`)" @toggle="togglePause" @remove="removeWorker">
      <template #enrollment>
        <SystemEnrollmentPanel :open="enrollOpen" :worker-types="WORKER_TYPES" :output-modes="OUTPUT_MODES"
          :ai-access-methods="AI_ACCESS_METHODS" :name-base="nameBase" :worker-name="workerName"
          :registration-code="token" :token-ttl-text="tokenTtlText" :minting="minting" :copied-registration-code="copiedToken"
          :copied-command="copiedCmd" :command="command" :command-title="commandTitle"
          :command-copy-label="commandCopyLabel" :gateway-url="gatewayUrl"
          v-model:selected-pools="selectedPools" v-model:worker-name-draft="workerNameDraft" v-model:name-touched="nameTouched"
          v-model:new-tags="newTags" v-model:worker-state-dir="workerStateDir" v-model:state-dir-touched="stateDirTouched"
          v-model:output-mode="outputMode" v-model:watchtower-enabled="watchtowerEnabled"
          v-model:watchtower-interval="watchtowerInterval" v-model:ai-access-method="aiAccessMethod"
          @toggle="onEnrollToggle" @mint="mint" @copy="copy" />
      </template>
    </SystemWorkersPanel>
  </section>
</template>

<style scoped>
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
