<script setup lang="ts">
// 系统健康总览页（/system）。6 区（自上而下，设计 §2.5）：
//  1 系统信息（版本 + 各组件版本 + 部署模式）
//  2 系统状态（健康条 + 组件健康 + jobs 计数 + 磁盘 + AI 用量聚合）
//  3 系统历史事件（事件流）
//  4 调度信息（Scheduler 健康 + 资源池）
//  5 worker 信息（计数 + 版本漂移汇总 + 接入新 worker 折叠）
//  6 worker 状态卡片
// 双通道：WS 每 2s 推 live 子集（计数/忙闲/队列/磁盘跳动）；HTTP /api/status + /api/usage +
// /api/events 进页 1 次 + 每 15s 轮询（组件/版本/吞吐/用量/事件，慢变量）+ 手动刷新。
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkerStore } from '../stores/workers'
import { useGlobalWs } from '../composables/useGlobalWs'
import StatusBadge from '../components/common/StatusBadge.vue'
import ComponentCard from '../components/system/ComponentCard.vue'
import { fmtDuration, fmtRelative } from '../utils/datetime'
import { workerDotClass, workerComputeDesc } from '../utils/worker'
import type { Worker, FullStatus, SystemComponent, SystemEvent, UsageAggregate } from '../types'
import { COMPONENT_KIND_LABELS } from '../types'
import {
  Server, RefreshCw, Cpu, Pause, Play, MessageSquare, X, Plus,
  Key, Copy, Check, Layers, HardDrive, Database, Boxes, AlertTriangle,
  Activity, GitCommit, Coins,
} from 'lucide-vue-next'

const router = useRouter()
const workerStore = useWorkerStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

// WS live 子集（每 2s）：只覆盖 jobs/workers/pools/disk 四段，组件/版本/吞吐保持上次轮询值。
const { systemStatus, connected, reconnect } = useGlobalWs()

const status = ref<FullStatus | null>(null)
const lastOkAt = ref<number | null>(null)   // 末次成功 /api/status 时间戳（绑「刷新 N 秒前」）
const failStreak = ref(0)                    // 连续失败计数（抖动缓冲，§8.4）
const usage = ref<UsageAggregate | null>(null)
const events = ref<SystemEvent[]>([])

async function loadStatus() {
  try {
    status.value = await workerStore.fetchFullStatus()
    lastOkAt.value = Date.now()
    failStreak.value = 0
  } catch {
    failStreak.value++
    // 不立即清空已有数据（保留陈旧快照）；健康条会进「不可达」档。
  }
}
async function loadUsage() {
  try { usage.value = await workerStore.fetchUsage() } catch { /* 非致命 */ }
}
async function loadEvents() {
  try { events.value = (await workerStore.fetchEvents(50)).events } catch { /* 非致命 */ }
}

async function refreshAll() {
  await Promise.all([loadStatus(), workerStore.fetchAll(), loadPoolLimits(), loadUsage(), loadEvents()])
}

// 进页 1 次 + 每 15s 轮询（组件/版本/吞吐/用量/事件）。WS 负责计数实时跳动。
let poll: number | undefined
onMounted(() => {
  refreshAll()
  poll = window.setInterval(() => {
    loadStatus(); loadUsage(); loadEvents()
  }, 15000)
})
onUnmounted(() => { if (poll) window.clearInterval(poll) })

// ── 池上限编辑（沿用既有交互 + 恢复默认 + 0 值确认）──
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

// ── 组件 / 版本派生 ──
const components = computed<SystemComponent[]>(() => status.value?.components ?? [])
const systemVersion = computed(() => status.value?.version || 'dev')
const apiComp = computed(() => components.value.find(c => c.kind === 'api'))
const schedComp = computed(() => components.value.find(c => c.kind === 'scheduler'))
const minioComp = computed(() => components.value.find(c => c.kind === 'minio'))
const deployMode = computed(() => {
  const m = minioComp.value?.extra?.mode
  return m === 'remote' ? '分布式（对象存储）' : m === 'local' ? '单机（本地盘）' : '—'
})

// ── live 四段：WS 优先，回退轮询 ──
const liveJobs = computed(() => systemStatus.value?.jobs ?? status.value?.jobs ?? null)
const livePools = computed(() => systemStatus.value?.pools ?? status.value?.pools ?? {})
const liveDisk = computed(() => systemStatus.value?.disk ?? status.value?.disk ?? null)
const throughput = computed(() => status.value?.throughput_1h ?? null)

// ── Worker 列表派生 ──
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

// 池派生：占用 / 积压 / 无 worker 积压 / 暂停。
function poolDot(name: string, p: { capacity: number; used: number; queue: number }): string {
  if (p.capacity === 0) return 'd-mut'                          // 暂停
  const onlineForType = status.value?.workers?.[name]?.online ?? 0
  if (p.queue > 0 && onlineForType === 0) return 'd-bad'        // 无 worker 积压
  if (p.queue > 0 && p.used >= p.capacity) return 'd-warn'      // 满载积压
  return 'd-ok'
}
function poolQueueBadge(name: string, p: { capacity: number; used: number; queue: number }): { cls: string; text: string } {
  if (p.capacity === 0) return { cls: 'b-mut', text: `⏸ ${p.queue} 等待` }
  const onlineForType = status.value?.workers?.[name]?.online ?? 0
  if (p.queue > 0 && onlineForType === 0) return { cls: 'b-bad', text: `⚠ ${p.queue} 等待无 worker` }
  if (p.queue > 0) return { cls: 'b-warn', text: `▲ ${p.queue} 积压` }
  return { cls: 'b-mut', text: `队列 ${p.queue}` }
}

// ── 版本漂移（前端比对，§8.6）──
function shortSha(v: string | null | undefined): string {
  return (v || '').trim().slice(0, 7)
}
function versionMatches(expected: string, actual: string | null | undefined): boolean {
  const e = (expected || '').trim().toLowerCase()
  const a = (actual || '').trim().toLowerCase()
  if (!e || !a) return true   // 缺基准/缺自报 → 不算漂移（不误报）
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

// worker live 负载短文案（cpu%/mem%/load）。
function loadText(w: Worker): string {
  const l = w.load
  if (!l) return ''
  const parts: string[] = []
  if (l.cpu_pct != null) parts.push(`CPU ${l.cpu_pct}%`)
  if (l.mem_pct != null) parts.push(`内存 ${l.mem_pct}%`)
  if (l.loadavg != null) parts.push(`负载 ${l.loadavg}`)
  return parts.join(' ')
}

// ── ①健康条聚合（纯函数派生，§3）──
type Overall = 'ok' | 'warn' | 'down' | 'unreachable'
const overall = computed<Overall>(() => {
  // 不可达：连续失败 ≥1 且当前无可用快照（或失败累计已多次）。保留陈旧快照仍展示其余。
  if (failStreak.value >= 1 && status.value === null) return 'unreachable'
  if (failStreak.value >= 3) return 'unreachable'
  const comps = components.value
  // 红：任一组件 down；所有 worker 离线（曾有 worker）；暂停池却有积压。
  const anyDown = comps.some(c => c.status === 'down')
  const allOffline = workerStore.workers.length > 0 && onlineCount.value === 0
  const pausedBacklog = pools.value.some(([, p]) => p.capacity === 0 && p.queue > 0)
  if (anyDown || allOffline || pausedBacklog) return 'down'
  // 黄：组件 degraded/unknown（minio local 不算）；stale worker；版本漂移；排队无 worker。
  const anyDegraded = comps.some(c => c.status === 'degraded'
    || (c.status === 'unknown' && c.extra?.mode !== 'local'))
  const anyStale = workerStore.workers.some(w => w.status === 'stale')
  const queueNoWorker = pools.value.some(([name, p]) =>
    p.queue > 0 && (status.value?.workers?.[name]?.online ?? 0) === 0 && p.capacity !== 0)
  if (anyDegraded || anyStale || driftCount.value > 0 || queueNoWorker) return 'warn'
  return 'ok'
})
const overallDot = computed(() =>
  overall.value === 'ok' ? 'd-ok' : overall.value === 'warn' ? 'd-warn' : 'd-bad')
const overallClass = computed(() =>
  overall.value === 'ok' ? 'hb-ok' : overall.value === 'warn' ? 'hb-warn' : 'hb-down')
const healthTitle = computed(() => {
  if (overall.value === 'unreachable') return `无法连接后端 · 正在重试（第 ${failStreak.value} 次）`
  if (overall.value === 'down') return `${issues.value.length} 项异常，需处理`
  if (overall.value === 'warn') return `${issues.value.length} 项需关注`
  return '系统运行正常'
})
// 异常摘要（最多列 2 条，超出「等 N 项」）。
const issues = computed<string[]>(() => {
  const out: string[] = []
  for (const c of components.value) {
    if (c.status === 'down') out.push(`${COMPONENT_KIND_LABELS[c.kind]} 离线`)
    else if (c.status === 'degraded') out.push(`${COMPONENT_KIND_LABELS[c.kind]} 降级`)
    else if (c.status === 'unknown' && c.extra?.mode !== 'local') out.push(`${COMPONENT_KIND_LABELS[c.kind]} 采集失败`)
  }
  for (const [name, p] of pools.value) {
    if (p.capacity === 0 && p.queue > 0) out.push(`${name} 池暂停但有 ${p.queue} 排队`)
    else if (p.queue > 0 && (status.value?.workers?.[name]?.online ?? 0) === 0) out.push(`${name} 池 ${p.queue} 排队无 worker`)
  }
  if (workerStore.workers.length > 0 && onlineCount.value === 0) out.push('所有 worker 均已离线')
  if (driftCount.value > 0) out.push(`${driftCount.value} 个 worker 运行旧版本`)
  if (workerStore.workers.some(w => w.status === 'stale')) out.push('有 worker 失联')
  return out
})
const healthSummary = computed(() => {
  if (overall.value === 'unreachable') {
    const ago = lastOkAt.value ? `上次更新 ${Math.round((Date.now() - lastOkAt.value) / 1000)}s 前` : '从未成功'
    return ago
  }
  if (overall.value === 'ok') {
    const ago = lastOkAt.value ? `刷新 ${Math.round((Date.now() - lastOkAt.value) / 1000)}s 前` : ''
    const upN = components.value.filter(c => c.status === 'up').length
    return `组件 ${upN}/${components.value.length} · Worker ${onlineCount.value}/${workerStore.workers.length} 在线 · 0 队列阻塞${ago ? ' · ' + ago : ''}`
  }
  const head = issues.value.slice(0, 2).join(' · ')
  const more = issues.value.length > 2 ? ` 等 ${issues.value.length} 项` : ''
  return head + more
})

// 事件 kind → 中文 + 严重度点色。
const EVENT_LABELS: Record<string, string> = {
  orphan_reclaimed: '孤儿回收', step_stuck: '卡住步', no_worker: '无 worker',
  worker_cleaned: 'worker 清理', job_failed: '任务失败',
}
const EVENT_DOT: Record<string, string> = {
  orphan_reclaimed: 'd-warn', step_stuck: 'd-warn', no_worker: 'd-bad',
  worker_cleaned: 'd-mut', job_failed: 'd-bad',
}
function eventLabel(k: string): string { return EVENT_LABELS[k] ?? k }
function eventDot(k: string): string { return EVENT_DOT[k] ?? 'd-mut' }
function eventSummary(e: SystemEvent): string {
  const parts: string[] = []
  if (e.job_id) parts.push(e.job_id)
  if (e.step) parts.push(e.step)
  if (e.pool) parts.push(`池 ${e.pool}`)
  if (e.reason) parts.push(e.reason)
  if (e.error) parts.push(String(e.error).slice(0, 80))
  if (e.worker_id) parts.push(e.worker_id)
  return parts.join(' · ')
}

// 磁盘阈值色。
const diskBarColor = computed(() => {
  const pct = liveDisk.value?.used_pct ?? 0
  return pct > 90 ? 'var(--bad)' : pct >= 75 ? 'var(--warn)' : 'var(--ok)'
})

// ── 行内 暂停 / 继续 / 移除 ──
const rowBusy = ref<string | null>(null)
async function togglePause(w: Worker) {
  rowBusy.value = w.id
  try {
    if (w.status === 'paused') { await workerStore.resume(w.id); showToast('已继续', 'success') }
    else { await workerStore.pause(w.id); showToast('已暂停，当前任务跑完后不再认领新任务', 'success') }
  } catch { showToast('操作失败', 'error') } finally { rowBusy.value = null }
}
async function removeWorker(w: Worker) {
  if (!confirm(`确定移除 Worker ${w.id}？`)) return
  rowBusy.value = w.id
  try { await workerStore.remove(w.id); showToast('已移除', 'success') }
  catch { showToast('移除失败', 'error') } finally { rowBusy.value = null }
}

// ── 接入新 Worker（mintToken + docker 命令；折叠 <details>）──
const IMAGE = import.meta.env.VITE_WORKER_IMAGE || 'ghcr.io/gwzlchn/flori:latest'
const WORKER_TYPES = ['cpu', 'gpu', 'ai', 'io']
const TABS = [
  { id: 'gateway', label: '分布式' },
  { id: 'docker', label: 'docker run' },
  { id: 'compose', label: 'compose' },
] as const
const ENROLL_KEY = 'flori.system.enroll.open'
const enrollOpen = ref(localStorage.getItem(ENROLL_KEY) === '1')
function onEnrollToggle(e: Event) {
  const open = (e.target as HTMLDetailsElement).open
  localStorage.setItem(ENROLL_KEY, open ? '1' : '0')
}

const newType = ref('cpu')
const newTags = ref('')
const activeTab = ref<(typeof TABS)[number]['id']>('gateway')
const token = ref('')
const minting = ref(false)
const newConcurrency = ref(1)
const AI_CRED_METHODS = [
  { id: 'claude-sub', label: 'Claude 订阅(共享 ~/.claude)' },
  { id: 'anthropic', label: 'Anthropic API Key' },
  { id: 'deepseek', label: 'DeepSeek API Key' },
] as const
const aiCredMethod = ref<(typeof AI_CRED_METHODS)[number]['id']>('claude-sub')

const gatewayUrl = computed(() => {
  const o = typeof window !== 'undefined' ? window.location?.origin : ''
  return o && o.startsWith('http') ? o : 'https://<FLORI_HOST>'
})
const credLines = computed(() => {
  if (newType.value === 'ai') {
    if (aiCredMethod.value === 'claude-sub') return '  -v $HOME/.claude:/root/.claude \\\n'
    if (aiCredMethod.value === 'deepseek') return '  -e DEEPSEEK_API_KEY=<KEY> \\\n'
    return '  -e ANTHROPIC_API_KEY=<KEY> \\\n'
  }
  if (newType.value === 'io') return '  -e BILI_SESSDATA=<B站SESSDATA,留空=匿名480P> \\\n'
  return ''
})
const cacheLine = computed(() => (newType.value === 'gpu'
  ? '  -v whisper-cache:/cache -e MODEL_CACHE_DIR=/cache \\\n' : ''))
const tagsArg = computed(() => {
  const t = newTags.value.split(/[\s,]+/).filter(Boolean)
  return t.length ? ` --tags ${t.join(' ')}` : ''
})
const runCmd = computed(() => `python -m worker.main --type ${newType.value}${tagsArg.value}`)
const tokenLine = computed(() => token.value || 'flw-<生成后填入>')
const gpuFlag = computed(() => (newType.value === 'gpu' ? ' --gpus all' : ''))

const command = computed(() => {
  if (activeTab.value === 'gateway') {
    return `docker run -d --restart unless-stopped${gpuFlag.value} \\
  -e GATEWAY_URL=${gatewayUrl.value} \\
  -e GATEWAY_TLS_INSECURE=1 \\
  -e WORKER_REGISTRATION_TOKEN=${tokenLine.value} \\
  -e WORKER_NAME=${newType.value}-1 \\
  -e WORKER_CONCURRENCY=${newConcurrency.value} \\
${credLines.value}${cacheLine.value}  ${IMAGE} \\
  ${runCmd.value}`
  }
  if (activeTab.value === 'compose') {
    const credCompose = newType.value === 'ai'
      ? '      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}\n'
      : newType.value === 'io'
        ? '      - BILI_SESSDATA=${BILI_SESSDATA:-}\n'
        : ''
    return `# 追加到 docker-compose.yml services:
  worker-${newType.value}-extra:
    image: ${IMAGE}
    restart: unless-stopped
    command: ${runCmd.value}
    volumes: [ "\${FLORI_DATA_DIR:-flori-data}:/data" ]
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATA_DIR=/data
      - WORKER_NAME=${newType.value}-1
      - WORKER_CONCURRENCY=${newConcurrency.value}
${credCompose}    depends_on: [ redis ]`
  }
  return `docker run -d --restart unless-stopped${gpuFlag.value} \\
  -e REDIS_URL=redis://<HOST>:6379/0 \\
  -e MINIO_URL=<HOST>:9000 -e MINIO_ACCESS_KEY=<KEY> -e MINIO_SECRET_KEY=<SECRET> -e MINIO_BUCKET=flori \\
  -e DATA_DIR=/data -e WORK_DIR=/tmp/flori-work -e WORKER_CONCURRENCY=${newConcurrency.value} \\
${credLines.value}${cacheLine.value}  -v flori-data:/data \\
  ${IMAGE} \\
  ${runCmd.value}`
})

async function mint() {
  minting.value = true
  try {
    token.value = await workerStore.mintToken()
    showToast('已生成接入 token（仅此一次完整展示，妥善保存）', 'success')
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

// AI 用量：成本按 provider==claude-cli 标「(等价)」。
function costLabel(provider: string): string { return provider === 'claude-cli' ? '（等价）' : '' }
function fmtCost(v: number): string { return `$${(v ?? 0).toFixed(4)}` }

// 按 provider 分组（每个可点开看自己的统计；跨 provider 总计在顶部 4 块）。
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

    <!-- ① 系统健康条 -->
    <div class="card pad health-bar" :class="overallClass" style="margin-bottom:18px">
      <span class="dot dot-lg" :class="[overallDot, { pulse: overall === 'down' || overall === 'unreachable' }]"></span>
      <div class="hb-text">
        <b class="hb-title">{{ healthTitle }}</b>
        <span class="hb-sub">{{ healthSummary }}</span>
      </div>
    </div>

    <!-- 1. 系统信息 -->
    <div class="seclabel" style="margin-bottom:10px"><GitCommit :size="14" />系统信息</div>
    <div class="card pad" style="margin-bottom:18px;display:flex;flex-wrap:wrap;gap:8px 22px;align-items:center">
      <span style="font-size:13px;color:var(--ink-700)">系统版本 <b class="mono">{{ systemVersion }}</b></span>
      <span class="sep" style="color:var(--ink-300)">·</span>
      <span style="font-size:13px;color:var(--ink-700)">部署模式 <b>{{ deployMode }}</b></span>
      <template v-for="c in components" :key="`v-${c.name}`">
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span style="font-size:12.5px;color:var(--ink-600)">{{ COMPONENT_KIND_LABELS[c.kind] }}
          <b class="mono">{{ c.version || '—' }}</b></span>
      </template>
    </div>

    <!-- 2. 系统状态 -->
    <!-- ② 概览(grid4) -->
    <div class="grid4" style="margin-bottom:18px">
      <div class="metric"><div class="v">{{ onlineCount }} / {{ workerStore.workers.length }}</div><div class="l">Worker 在线 / 共</div></div>
      <div class="metric"><div class="v">{{ busyCount }}</div><div class="l">忙碌 · 处理中</div></div>
      <div class="metric"><div class="v">{{ pendingCount }}</div><div class="l">待处理 · 队列</div></div>
      <div class="metric"><div class="v">{{ doneCount }}</div><div class="l">累计完成 · 吞吐</div></div>
    </div>

    <!-- ③ 核心组件 -->
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

    <!-- jobs 计数 + 磁盘 -->
    <div class="card pad" style="margin-bottom:14px;display:flex;align-items:center;gap:18px;flex-wrap:wrap">
      <span class="badge b-mut"><HardDrive :size="12" />磁盘</span>
      <template v-if="liveDisk && liveDisk.total_gb >= 0">
        <span style="font-size:13px;color:var(--ink-700)">
          {{ liveDisk.used_gb }}/{{ liveDisk.total_gb }}GB
          <b :style="{ color: liveDisk.used_pct > 90 ? 'var(--bad)' : 'var(--ink-900)' }">{{ liveDisk.used_pct }}%</b>
        </span>
        <span class="dim-g" style="flex:1;min-width:120px;max-width:280px">
          <span class="track"><span :style="{ width: `${Math.min(100, liveDisk.used_pct)}%`, background: diskBarColor }"></span></span>
        </span>
        <span style="font-size:12.5px;color:var(--ink-500)">剩 {{ liveDisk.available_gb }}GB</span>
      </template>
      <span v-else class="dim" style="font-size:13px">磁盘信息不可用</span>
      <span class="sep" style="color:var(--ink-300)">·</span>
      <span class="badge b-mut"><Database :size="12" />内容</span>
      <span v-if="liveJobs" style="font-size:13px;color:var(--ink-700)">
        共 <b>{{ liveJobs.total }}</b> · 处理中 <b>{{ liveJobs.processing }}</b> ·
        失败 <b :style="{ color: liveJobs.failed > 0 ? 'var(--bad)' : 'var(--ink-900)' }">{{ liveJobs.failed }}</b>
      </span>
      <template v-if="throughput">
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span style="font-size:12.5px;color:var(--ink-500)">近 1h 完成 {{ throughput.done }} · 失败 {{ throughput.failed }}</span>
      </template>
    </div>

    <!-- AI 用量聚合 -->
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
    </div>

    <!-- 3. 系统历史事件 -->
    <div class="seclabel" style="margin-bottom:12px"><AlertTriangle :size="14" />系统事件</div>
    <div class="card pad" style="margin-bottom:24px">
      <div v-if="events.length === 0" style="display:flex;align-items:center;gap:8px;color:var(--ink-500);font-size:13px">
        <span class="dot d-ok"></span>系统运行平稳，近期无告警
      </div>
      <div v-else class="list">
        <div v-for="(e, i) in events" :key="i" style="display:flex;align-items:center;gap:9px;font-size:12.5px">
          <span class="dot" :class="eventDot(e.kind)"></span>
          <span style="color:var(--ink-500);min-width:64px">{{ fmtRelative(e.ts * 1000) }}</span>
          <b style="color:var(--ink-900)">{{ eventLabel(e.kind) }}</b>
          <span style="color:var(--ink-600);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ eventSummary(e) }}</span>
        </div>
      </div>
    </div>

    <!-- 4. 调度信息 -->
    <div class="seclabel" style="margin-bottom:12px"><Activity :size="14" />调度信息</div>
    <div v-if="schedComp" style="margin-bottom:14px">
      <ComponentCard :comp="schedComp" />
    </div>
    <!-- 资源池 -->
    <div class="seclabel" style="margin-bottom:12px"><Layers :size="14" />资源池 · {{ pools.length }}</div>
    <div class="grid3" style="margin-bottom:24px">
      <div v-for="[name, p] in pools" :key="name" class="card pad" style="padding:13px 15px">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:8px">
          <span class="dot" :class="poolDot(name, p)"></span>
          <b class="mono" style="font-size:13px;color:var(--ink-900);flex:1">{{ name }}</b>
          <span class="badge" :class="poolQueueBadge(name, p).cls">{{ poolQueueBadge(name, p).text }}</span>
        </div>
        <div class="dim-g">
          <div class="row-l"><span>占用</span><b>{{ p.used }} / {{ p.capacity === 0 ? '暂停' : p.capacity }}</b></div>
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

    <!-- 5. worker 信息 -->
    <div class="seclabel" style="margin-bottom:12px">
      <Cpu :size="14" />Worker · {{ workerStore.workers.length }}
      <template v-if="driftEnabled">
        <span class="sep" style="margin:0 6px;color:var(--ink-300)">·</span>
        <span style="font-weight:500;text-transform:none;letter-spacing:0">系统版本 <b class="mono">{{ shortSha(systemVersion) }}</b></span>
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

    <!-- 接入新 Worker（折叠） -->
    <details class="card pad" style="margin-bottom:18px" :open="enrollOpen" @toggle="onEnrollToggle">
      <summary class="card-h" style="margin-bottom:0;cursor:pointer;list-style:none"><Plus :size="15" />接入新 Worker</summary>
      <div style="margin-top:14px">
        <div class="row2" style="margin-bottom:14px">
          <div class="field" style="margin:0">
            <label>类型</label>
            <select v-model="newType" class="input">
              <option v-for="t in WORKER_TYPES" :key="t" :value="t">{{ t }}</option>
            </select>
          </div>
          <div class="field" style="margin:0">
            <label>标签（可选，空=自动探测）</label>
            <input v-model="newTags" class="input" placeholder="如 home-desktop vision" />
          </div>
        </div>
        <div class="field" style="margin:0 0 14px;max-width:240px">
          <label>并发(本机同时跑几步;弱机=1,强机调大)</label>
          <input v-model.number="newConcurrency" type="number" min="1" class="input" />
        </div>
        <div v-if="newType === 'ai'" class="field" style="margin:0 0 14px">
          <label>AI 凭证方式</label>
          <select v-model="aiCredMethod" class="input">
            <option v-for="m in AI_CRED_METHODS" :key="m.id" :value="m.id">{{ m.label }}</option>
          </select>
          <p class="note-tip" style="margin:6px 0 0">
            <template v-if="aiCredMethod === 'claude-sub'">挂宿主已登录的 ~/.claude,claude-cli 自动续期、走 Max 订阅(不按 token 计费);镜像内置 claude CLI。中国大陆机器另加 -e HTTPS_PROXY=… 走代理。</template>
            <template v-else>按量计费:从对应 provider 控制台取 key 填入 &lt;KEY&gt;。</template>
          </p>
        </div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px">
          <button class="btn pri" :disabled="minting" @click="mint">
            <Key :size="14" />{{ token ? '重新生成 token' : '生成接入 token' }}
          </button>
          <template v-if="token">
            <code class="mono" style="flex:1;min-width:160px;background:var(--line-soft);border-radius:var(--r-sm);padding:6px 10px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ token }}</code>
            <button class="iconbtn" @click="copy(token, 'token')">
              <component :is="copiedToken ? Check : Copy" :size="15" />
            </button>
          </template>
          <span v-else class="note-tip" style="margin:0">生成后仅此一次完整展示，妥善保存。</span>
        </div>
        <div class="seg" style="margin-bottom:12px">
          <button v-for="t in TABS" :key="t.id" :class="{ on: activeTab === t.id }" @click="activeTab = t.id">{{ t.label }}</button>
        </div>
        <p v-if="activeTab === 'gateway'" class="note-tip" style="margin:0 0 8px">
          真零隧道：只需出站 HTTPS 到网关（{{ gatewayUrl }}），不连 redis/minio。
        </p>
        <pre style="background:var(--ink-900);color:#cbd5e1;font-family:var(--mono);font-size:12px;padding:12px;border-radius:var(--r-sm);overflow:auto;line-height:1.7;margin:0;white-space:pre-wrap;word-break:break-all">{{ command }}</pre>
        <button class="btn sm" style="margin-top:10px" @click="copy(command, 'cmd')">
          <component :is="copiedCmd ? Check : Copy" :size="13" />{{ copiedCmd ? '已复制' : '复制命令' }}
        </button>
      </div>
    </details>

    <!-- 6. worker 状态卡片 -->
    <div v-if="workerStore.loading && workerStore.workers.length === 0" class="card pad" style="color:var(--ink-500);font-size:13px;margin-bottom:24px">
      加载中…
    </div>
    <div v-else-if="workerStore.workers.length === 0" class="card pad"
      style="margin-bottom:24px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:36px 18px">
      <Cpu :size="40" :stroke-width="1" style="color:var(--ink-300)" />
      <div style="font-size:14px;color:var(--ink-700);font-weight:600">还没有接入任何 Worker</div>
      <div class="lead" style="max-width:360px">在上方「接入新 Worker」生成接入 token，按命令在任意机器上拉起一个 worker 即可。</div>
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
              当前 {{ w.current_step }}
              <span v-if="w.current_job" class="mono">{{ w.current_job }}</span>
            </span>
            <span v-if="workerDrifted(w)" class="badge b-warn"
              :title="`期望 ${shortSha(systemVersion)}，当前 ${shortSha(w.spec?.version)}`">
              旧版本 {{ shortSha(w.spec?.version) }}
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
            <template v-if="w.total_duration_sec > 0"><span class="sep">·</span><span>运行 {{ fmtDuration(w.total_duration_sec) }}</span></template>
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
        <button v-else class="btn sm danger" :disabled="rowBusy === w.id" @click.stop="removeWorker(w)">
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
</style>
