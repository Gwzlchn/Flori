<script setup lang="ts">
// 系统与 Worker（原型 #system）：系统状态（/api/status：资源池 / jobs 计数 / 磁盘）+ Worker 列表
// + 接入新 Worker（mintToken + docker 命令）。Worker 列表走 store.fetchAll()，卡片跳 /system/workers/:id。
import { ref, computed, onMounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useWorkerStore } from '../stores/workers'
import { useGlobalWs } from '../composables/useGlobalWs'
import StatusBadge from '../components/common/StatusBadge.vue'
import { fmtDuration, fmtRelative } from '../utils/datetime'
import { workerDotClass, workerComputeDesc } from '../utils/worker'
import type { Worker } from '../types'
import {
  Server, RefreshCw, Cpu, Pause, Play, MessageSquare, X, Plus,
  Key, Copy, Check, Layers, HardDrive, Database,
} from 'lucide-vue-next'

const router = useRouter()
const api = useApi()
const workerStore = useWorkerStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

// 实时系统状态（每 2s 推送，仅 jobs 字段）；与 /api/status 拉取的全量并存。
const { systemStatus } = useGlobalWs()

// 资源池 / 磁盘只在 /api/status 里有（全局 ws 的 SystemStatus 仅 jobs），故单拉一份。
interface PoolInfo { capacity: number; used: number; queue: number }
interface FullStatus {
  workers: Record<string, { online: number; busy: number }>
  pools: Record<string, PoolInfo>
  jobs: { total: number; done: number; processing: number; failed: number; pending: number }
  disk: { used_gb: number; available_gb: number }
}

const status = ref<FullStatus | null>(null)
const statusErr = ref('')

async function loadStatus() {
  statusErr.value = ''
  try {
    status.value = await api.get<FullStatus>('/api/status')
  } catch (e: any) {
    statusErr.value = e?.message || '加载系统状态失败'
  }
}

async function refreshAll() {
  await Promise.all([loadStatus(), workerStore.fetchAll()])
}

// ── Worker 列表派生 ──
const STATUS_ORDER: Record<string, number> = {
  'online-busy': 0, 'online-idle': 1, paused: 2, stale: 3, offline: 4,
}
const sortedWorkers = computed(() =>
  [...workerStore.workers].sort((a, b) => (STATUS_ORDER[a.status] ?? 5) - (STATUS_ORDER[b.status] ?? 5))
)
const onlineCount = computed(() => workerStore.workers.filter(w => w.status.startsWith('online') || w.status === 'paused').length)
const busyCount = computed(() => workerStore.workers.filter(w => w.status === 'online-busy').length)
// jobs.done 优先用实时推送，回退到拉取的全量。
const doneCount = computed(() => systemStatus.value?.jobs?.done ?? status.value?.jobs?.done ?? 0)

const pools = computed(() => Object.entries(status.value?.pools ?? {}))

// dot 颜色 / 算力描述统一走 utils/worker(状态→点色、算力描述),时长/相对时间走 utils/datetime。
const dotClass = workerDotClass
const computeDesc = workerComputeDesc
function isOnline(w: Worker): boolean {
  return w.status.startsWith('online') || w.status === 'paused'
}

// ── 行内 暂停 / 继续 / 移除 ──
const rowBusy = ref<string | null>(null)
async function togglePause(w: Worker) {
  rowBusy.value = w.id
  try {
    if (w.status === 'paused') {
      await workerStore.resume(w.id)
      showToast('已继续', 'success')
    } else {
      await workerStore.pause(w.id)
      showToast('已暂停', 'success')
    }
  } catch {
    showToast('操作失败', 'error')
  } finally {
    rowBusy.value = null
  }
}
async function removeWorker(w: Worker) {
  if (!confirm(`确定移除 Worker ${w.id}？`)) return
  rowBusy.value = w.id
  try {
    await workerStore.remove(w.id)
    showToast('已移除', 'success')
  } catch {
    showToast('移除失败', 'error')
  } finally {
    rowBusy.value = null
  }
}

// ── 接入新 Worker：mintToken + docker 命令 ──
// 镜像 owner/tag 不写死:优先构建期注入(VITE_WORKER_IMAGE),回退默认值。
const IMAGE = import.meta.env.VITE_WORKER_IMAGE || 'ghcr.io/gwzlchn/flori:latest'
const WORKER_TYPES = ['cpu', 'gpu', 'ai', 'io']
const TABS = [
  { id: 'gateway', label: '分布式' },
  { id: 'docker', label: 'docker run' },
  { id: 'compose', label: 'compose' },
] as const

const newType = ref('cpu')
const newTags = ref('')
const activeTab = ref<(typeof TABS)[number]['id']>('gateway')
const token = ref('')
const minting = ref(false)

const gatewayUrl = computed(() => {
  const o = typeof window !== 'undefined' ? window.location?.origin : ''
  return o && o.startsWith('http') ? o : 'https://<FLORI_HOST>'
})
// 仅 ai 类型订 ai 池、需 AI key;gpu 订 [gpu,scene,cpu,io] 不含 ai,不需 ANTHROPIC_API_KEY。
const needsAiKey = computed(() => newType.value === 'ai')
const tagsArg = computed(() => {
  const t = newTags.value.split(/[\s,]+/).filter(Boolean)
  return t.length ? ` --tags ${t.join(' ')}` : ''
})
const runCmd = computed(() => `python -m worker.main --type ${newType.value}${tagsArg.value}`)
const tokenLine = computed(() => token.value || 'mnw-<生成后填入>')
const gpuFlag = computed(() => (newType.value === 'gpu' ? ' --gpus all' : ''))

const command = computed(() => {
  if (activeTab.value === 'gateway') {
    const aiLine = needsAiKey.value ? '  -e ANTHROPIC_API_KEY=<KEY> \\\n' : ''
    return `docker run -d --restart unless-stopped${gpuFlag.value} \\
  -e GATEWAY_URL=${gatewayUrl.value} \\
  -e GATEWAY_TLS_INSECURE=1 \\
  -e WORKER_REGISTRATION_TOKEN=${tokenLine.value} \\
  -e WORKER_ID_FILE=/data/.worker_id \\
  -e DATA_DIR=/data -e CONFIG_DIR=/app/configs -e WORK_DIR=/tmp/flori-work \\
${aiLine}  -v flori-data:/data \\
  ${IMAGE} \\
  ${runCmd.value}`
  }
  if (activeTab.value === 'compose') {
    const aiLines = needsAiKey.value ? '      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}\n' : ''
    return `# 追加到 docker-compose.yml services:
  worker-${newType.value}-extra:
    image: ${IMAGE}
    restart: unless-stopped
    command: ${runCmd.value}
    volumes: [ "\${FLORI_DATA_DIR:-flori-data}:/data" ]
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATA_DIR=/data
      - CONFIG_DIR=/app/configs
${aiLines}    depends_on: [ redis ]`
  }
  const aiLine = needsAiKey.value ? '  -e ANTHROPIC_API_KEY=<KEY> \\\n' : ''
  return `docker run -d --restart unless-stopped${gpuFlag.value} \\
  -e REDIS_URL=redis://<HOST>:6379/0 \\
  -e MINIO_URL=<HOST>:9000 -e MINIO_ACCESS_KEY=<KEY> -e MINIO_SECRET_KEY=<SECRET> -e MINIO_BUCKET=flori \\
  -e DATA_DIR=/data -e CONFIG_DIR=/app/configs -e WORK_DIR=/tmp/flori-work \\
${aiLine}  -v flori-data:/data \\
  ${IMAGE} \\
  ${runCmd.value}`
})

async function mint() {
  minting.value = true
  try {
    token.value = await workerStore.mintToken()
    showToast('已生成接入 token（仅此一次完整展示，妥善保存）', 'success')
  } catch {
    showToast('生成失败', 'error')
  } finally {
    minting.value = false
  }
}

const copiedToken = ref(false)
const copiedCmd = ref(false)
async function copy(text: string, which: 'token' | 'cmd') {
  try {
    await navigator.clipboard.writeText(text)
    if (which === 'token') { copiedToken.value = true; setTimeout(() => (copiedToken.value = false), 1800) }
    else { copiedCmd.value = true; setTimeout(() => (copiedCmd.value = false), 1800) }
    showToast('已复制', 'success')
  } catch {
    showToast('复制失败', 'error')
  }
}

onMounted(refreshAll)
</script>

<template>
  <section class="page">
    <!-- 页头 -->
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
      <div class="h1"><Server :size="18" />系统与 Worker</div>
      <button class="btn sm" style="margin-left:auto" :disabled="workerStore.loading" @click="refreshAll">
        <RefreshCw :size="13" :class="workerStore.loading ? 'spin' : ''" />刷新
      </button>
    </div>

    <!-- 系统指标 -->
    <div class="grid3" style="margin-bottom:18px">
      <div class="metric"><div class="v">{{ onlineCount }} / {{ workerStore.workers.length }}</div><div class="l">Worker 在线 / 共</div></div>
      <div class="metric"><div class="v">{{ busyCount }}</div><div class="l">忙碌 · 处理中</div></div>
      <div class="metric"><div class="v">{{ doneCount }}</div><div class="l">累计完成 · 吞吐</div></div>
    </div>

    <!-- 资源池 + 磁盘 -->
    <div v-if="statusErr" class="card pad" style="margin-bottom:18px;display:flex;align-items:center;gap:12px">
      <span style="flex:1;font-size:13px;color:var(--ink-700)">{{ statusErr }}</span>
      <button class="btn sm" @click="loadStatus">重试</button>
    </div>
    <template v-else-if="status">
      <div class="seclabel" style="margin-bottom:12px"><Layers :size="14" />资源池 · {{ pools.length }}</div>
      <div class="grid3" style="margin-bottom:14px">
        <div v-for="[name, p] in pools" :key="name" class="card pad" style="padding:13px 15px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <b class="mono" style="font-size:13px;color:var(--ink-900)">{{ name }}</b>
            <span class="badge" :class="p.queue > 0 ? 'b-run' : 'b-mut'">队列 {{ p.queue }}</span>
          </div>
          <div class="dim-g">
            <div class="row-l"><span>占用</span><b>{{ p.used }} / {{ p.capacity }}</b></div>
            <div class="track"><span :style="{ width: `${Math.min(100, p.capacity ? (p.used / p.capacity) * 100 : 0)}%` }"></span></div>
          </div>
        </div>
      </div>
      <!-- 磁盘 + jobs 概览 -->
      <div class="card pad" style="margin-bottom:24px;display:flex;align-items:center;gap:18px;flex-wrap:wrap">
        <span class="badge b-mut"><HardDrive :size="12" />磁盘</span>
        <span style="font-size:13px;color:var(--ink-700)" v-if="status.disk.used_gb >= 0">
          已用 <b>{{ status.disk.used_gb }}GB</b> · 可用 <b>{{ status.disk.available_gb }}GB</b>
        </span>
        <span v-else class="dim" style="font-size:13px">磁盘信息不可用</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span class="badge b-mut"><Database :size="12" />内容</span>
        <span style="font-size:13px;color:var(--ink-700)">
          共 <b>{{ status.jobs.total }}</b> · 处理中 <b>{{ status.jobs.processing }}</b> · 失败 <b>{{ status.jobs.failed }}</b>
        </span>
      </div>
    </template>

    <!-- Worker 列表 -->
    <div class="seclabel" style="margin-bottom:12px"><Cpu :size="14" />Worker · {{ workerStore.workers.length }}</div>

    <!-- 加载态 -->
    <div v-if="workerStore.loading && workerStore.workers.length === 0" class="card pad" style="color:var(--ink-500);font-size:13px;margin-bottom:24px">
      加载中…
    </div>
    <!-- 空态 -->
    <div v-else-if="workerStore.workers.length === 0" class="card pad"
      style="margin-bottom:24px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:36px 18px">
      <Cpu :size="40" :stroke-width="1" style="color:var(--ink-300)" />
      <div style="font-size:14px;color:var(--ink-700);font-weight:600">还没有接入任何 Worker</div>
      <div class="lead" style="max-width:360px">在下方生成接入 token，按命令在任意机器上拉起一个 worker 即可。</div>
    </div>
    <!-- 列表 -->
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
          </div>
          <div class="meta">
            <span v-if="w.hostname">{{ w.hostname }}</span>
            <span v-if="w.hostname" class="sep">·</span>
            <span>{{ computeDesc(w) }}</span><span class="sep">·</span>
            <span>完成 {{ w.tasks_completed }}</span><span class="sep">·</span>
            <span>失败 {{ w.tasks_failed }}</span>
            <template v-if="w.total_duration_sec > 0">
              <span class="sep">·</span><span>运行 {{ fmtDuration(w.total_duration_sec) }}</span>
            </template>
            <span class="sep">·</span><span>心跳 {{ fmtRelative(w.last_heartbeat) }}</span>
          </div>
        </div>
        <!-- 在线/已暂停：暂停/继续 + 备注入口（备注跳详情编辑）；离线：移除 -->
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

    <!-- 接入新 Worker -->
    <div class="card pad">
      <div class="card-h"><Plus :size="15" />接入新 Worker</div>

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

      <!-- 生成 token -->
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

      <!-- 命令 tabs -->
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
  </section>
</template>

<style scoped>
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
