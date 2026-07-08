<script setup lang="ts">
// Worker 详情页:单个 worker 完整统计 + 基本信息 + 任务(task)历史
// + 备注编辑 + 暂停/移除。worker 主体走 GET /api/workers/{id};task 历史走 store.fetchTasks(id)。
import { ref, computed, onMounted, onBeforeUnmount, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useWorkerStore } from '../stores/workers'
import { useGlobalStore } from '../stores/global'
import { fmtDateTime, fmtDuration, fmtRelative } from '../utils/datetime'
import { fmtBytes } from '../utils/format'
import { workerDotClass, workerComputeDesc } from '../utils/worker'
import StatusBadge from '../components/common/StatusBadge.vue'
import TaskRow from '../components/system/TaskRow.vue'
import type { Worker, WorkerTask } from '../types'
import {
  RefreshCw, Pause, X, Cpu, Info, Layers, Clock, Check,
  Play, MessageSquare, Settings,
} from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const api = useApi()
const workerStore = useWorkerStore()
const global = useGlobalStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const workerId = computed(() => String(route.params.id))

const worker = ref<Worker | null>(null)
const tasks = ref<WorkerTask[]>([])
const loading = ref(true)
const error = ref('')
const busy = ref(false)
const cfgConcurrency = ref(1)
const cfgSaving = ref(false)

function desiredConcurrency(w: Worker): number {
  return w.desired_config?.concurrency ?? w.concurrency ?? 1
}

function applyWorkerSnapshot(w: Worker) {
  worker.value = w
  cfgConcurrency.value = desiredConcurrency(w)
  // 面包屑显真实 worker id(替代通用「Worker 详情」)
  global.setCrumbs([{ t: '系统', to: '/system' }, { t: w.id }])
}

async function reloadWorkerDetail() {
  const w = await api.get<Worker>(`/api/workers/${encodeURIComponent(workerId.value)}`)
  applyWorkerSnapshot(w)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 主体 + 历史并行;历史失败不致命(仅置空)。
    const [w, taskList] = await Promise.all([
      api.get<Worker>(`/api/workers/${encodeURIComponent(workerId.value)}`),
      workerStore.fetchTasks(workerId.value).catch(() => [] as WorkerTask[]),
    ])
    applyWorkerSnapshot(w)
    tasks.value = taskList
  } catch (e: any) {
    error.value = e?.status === 404 ? 'Worker 不存在或已移除' : (e?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

// 派生统计
const isOnline = computed(() => {
  const status = worker.value?.status
  return !!status && (status.startsWith('online') || status === 'paused')
})
const completed = computed(() => worker.value?.tasks_completed ?? 0)
const failed = computed(() => worker.value?.tasks_failed ?? 0)
const successRate = computed(() => {
  const total = completed.value + failed.value
  if (total === 0) return '—'
  return `${((completed.value / total) * 100).toFixed(1)}%`
})

// dot 颜色 / 算力描述统一走 utils/worker(与 WorkersView 单一来源)。
const dotClass = computed(() => workerDotClass(worker.value?.status))

// 类型徽章文案。
const typeLabel = computed(() => (worker.value?.type || '').toUpperCase())

// 时长走 utils/datetime.fmtDuration;心跳/完成时间用 fmtRelative,中文单位,超 1 天回退绝对时间。
const ago = (v: string | null | undefined) => fmtRelative(v, { style: 'cn', absoluteAfterDay: true })

// 算力描述:GPU 名优先,否则按类型;无 worker 时回退 —。
const computeDesc = computed(() => (worker.value ? workerComputeDesc(worker.value) : '—'))

const desiredCfgConcurrency = computed(() => (worker.value ? desiredConcurrency(worker.value) : cfgConcurrency.value))
const cfgSyncState = computed(() => {
  const w = worker.value
  if (!w?.cfg_rev) return ''
  return (w.applied_cfg_rev ?? 0) >= w.cfg_rev ? 'synced' : 'pending'
})

// 机器配置(worker 自报 spec):核数 · 内存 · 平台 · Python。
const machineDesc = computed(() => {
  const s = worker.value?.spec
  if (!s) return ''
  const parts: string[] = []
  if (s.cpu) parts.push(`${s.cpu} 核`)
  if (s.mem_mb) parts.push(`${(s.mem_mb / 1024).toFixed(1)} GB`)
  if (s.platform) parts.push(s.platform)
  if (s.python) parts.push(`Py ${s.python}`)
  return parts.join(' · ')
})

// 操作:暂停 / 继续 / 移除 / 备注
async function togglePause() {
  if (!worker.value) return
  busy.value = true
  try {
    if (worker.value.status === 'paused') {
      await workerStore.resume(workerId.value)
      showToast('已继续', 'success')
    } else {
      await workerStore.pause(workerId.value)
      showToast('已暂停', 'success')
    }
    await load()
  } catch {
    showToast('操作失败', 'error')
  } finally {
    busy.value = false
  }
}

async function removeWorker() {
  if (!worker.value) return
  if (!confirm(`移除 Worker ${workerId.value} 并吊销 worker token？该 worker 会停止接入；需要重新接入时请重新生成临时接入 token。`)) return
  busy.value = true
  try {
    // 在线/paused 管理态需 force,删除会吊销 per-worker token 并让旧连接快速失败。
    await workerStore.remove(workerId.value, isOnline.value)
    showToast('已移除并吊销 worker token', 'success')
    router.push('/system')
  } catch {
    showToast('移除失败', 'error')
    busy.value = false
  }
}

// 备注内联编辑。
const editingNote = ref(false)
const noteDraft = ref('')
function startEditNote() {
  noteDraft.value = worker.value?.admin_note || ''
  editingNote.value = true
}
async function saveNote() {
  busy.value = true
  try {
    await workerStore.updateNote(workerId.value, noteDraft.value.trim())
    editingNote.value = false
    showToast('备注已保存', 'success')
    await load()
  } catch {
    showToast('保存失败', 'error')
  } finally {
    busy.value = false
  }
}

async function saveConfig() {
  if (!worker.value) return
  const concurrency = Math.trunc(Number(cfgConcurrency.value))
  if (!Number.isFinite(concurrency) || concurrency < 1) {
    showToast('并发必须大于 0', 'error')
    return
  }
  cfgSaving.value = true
  try {
    await workerStore.setConfig(workerId.value, { concurrency })
    await reloadWorkerDetail()
    showToast('配置已保存', 'success')
  } catch {
    showToast('配置保存失败', 'error')
  } finally {
    cfgSaving.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => global.setCrumbs(null))
</script>

<template>
  <section class="page">
    <!-- 加载态 -->
    <div v-if="loading" class="card pad" style="color:var(--ink-500);font-size:13px">加载中…</div>

    <!-- 错误态 -->
    <div v-else-if="error" class="card pad"
      style="display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:40px 18px">
      <div style="font-size:13.5px;color:var(--ink-700)">{{ error }}</div>
      <div style="display:flex;gap:8px">
        <button class="btn" @click="load">重试</button>
        <button class="btn" @click="router.push('/system')">返回系统</button>
      </div>
    </div>

    <template v-else-if="worker">
      <!-- 页头 -->
      <div style="display:flex;align-items:center;gap:11px;flex-wrap:wrap">
        <span class="dot" :class="dotClass"></span>
        <div class="h1"><span class="mono">{{ worker.id }}</span></div>
        <StatusBadge :status="worker.status" />
        <span class="badge b-mut"><Cpu :size="12" />{{ typeLabel }}</span>
        <span v-if="worker.status === 'online-busy' && worker.current_step" class="badge b-run">
          当前 {{ worker.current_step }}
          <span v-if="worker.current_job" class="mono">{{ worker.current_job }}</span>
        </span>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn sm" @click="load"><RefreshCw :size="13" />刷新</button>
          <button v-if="isOnline" class="btn sm" :disabled="busy" @click="togglePause">
            <Play v-if="worker.status === 'paused'" :size="13" /><Pause v-else :size="13" />{{ worker.status === 'paused' ? '继续' : '暂停' }}
          </button>
          <button class="btn sm danger" :disabled="busy" @click="removeWorker"><X :size="13" />移除</button>
        </div>
      </div>

      <!-- 统计 -->
      <div class="grid3" style="margin-top:18px">
        <div class="metric"><div class="v">{{ completed }}</div><div class="l">累计完成</div></div>
        <div class="metric"><div class="v">{{ failed }}</div><div class="l">累计失败</div></div>
        <div class="metric"><div class="v">{{ successRate }}</div><div class="l">成功率</div></div>
      </div>

      <!-- 基本信息 -->
      <div class="card pad" style="margin-top:16px">
        <div class="card-h"><Info :size="15" />基本信息</div>
        <table class="kv">
          <tbody>
            <tr><td>主机名</td><td class="mono">{{ worker.hostname || '—' }}</td></tr>
            <tr><td>连接来源</td><td class="mono">{{ worker.remote_addr || '本机(直连)' }}</td></tr>
            <tr v-if="worker.traffic && ((worker.traffic.pull ?? 0) > 0 || (worker.traffic.push ?? 0) > 0)"><td>中转流量</td><td>↓ 出库 {{ fmtBytes(worker.traffic.pull ?? 0) }} · ↑ 入库 {{ fmtBytes(worker.traffic.push ?? 0) }}</td></tr>
            <tr><td>算力</td><td>{{ computeDesc }}</td></tr>
            <tr><td>并发</td><td>{{ worker.concurrency }}</td></tr>
            <tr v-if="worker.spec?.version"><td>版本</td><td class="mono">{{ worker.spec.version.split('+')[0] }}<span v-if="worker.spec.version.includes('+')" class="dim"> · 构建 {{ worker.spec.version.split('+')[1] }}</span></td></tr>
            <tr v-if="machineDesc"><td>机器</td><td>{{ machineDesc }}</td></tr>
            <tr>
              <td>资源池</td>
              <td>
                <template v-if="worker.pools.length">
                  <span v-for="p in worker.pools" :key="p" class="badge b-brand" style="margin-right:6px">
                    <Layers :size="12" />{{ p }}
                  </span>
                </template>
                <span v-else>—</span>
              </td>
            </tr>
            <tr>
              <td>标签</td>
              <td>
                <template v-if="worker.tags.length">
                  <span v-for="t in worker.tags" :key="t" class="tag" style="margin-right:6px">{{ t }}</span>
                </template>
                <span v-else class="dim">无</span>
              </td>
            </tr>
            <tr><td>运行时长</td><td>{{ fmtDuration(worker.total_duration_sec) }}</td></tr>
            <tr><td>上次心跳</td><td>{{ ago(worker.last_heartbeat) }}</td></tr>
            <tr><td>首次接入</td><td>{{ fmtDateTime(worker.first_seen) }}</td></tr>
            <tr>
              <td>备注</td>
              <td>
                <div v-if="!editingNote" style="display:flex;align-items:center;gap:8px">
                  <span>{{ worker.admin_note ? `「${worker.admin_note}」` : '—' }}</span>
                  <button class="ghost" style="font-size:12px" @click="startEditNote">
                    <MessageSquare :size="13" />编辑
                  </button>
                </div>
                <div v-else style="display:flex;flex-direction:column;gap:8px">
                  <input v-model="noteDraft" class="input" placeholder="给这台 worker 加个备注…" />
                  <div style="display:flex;gap:8px">
                    <button class="btn sm pri" :disabled="busy" @click="saveNote"><Check :size="13" />保存</button>
                    <button class="btn sm" @click="editingNote = false">取消</button>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 配置 -->
      <div class="card pad" style="margin-top:16px">
        <div class="card-h"><Settings :size="15" />配置</div>
        <div style="display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap">
          <div class="field" style="margin:0;max-width:150px">
            <label>并发</label>
            <input v-model.number="cfgConcurrency" type="number" min="1" max="64" class="input" />
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:2px">
            <span class="badge b-mut">当前 {{ worker.concurrency }}</span>
            <span class="badge b-brand">期望 {{ desiredCfgConcurrency }}</span>
            <span v-if="cfgSyncState === 'pending'" class="badge b-warn"
              title="等待 worker 心跳应用">待同步</span>
            <span v-else-if="cfgSyncState === 'synced'" class="badge b-mut"
              :title="`rev ${worker.cfg_rev}`">已生效</span>
          </div>
          <button class="btn sm pri" style="margin-left:auto" :disabled="cfgSaving" @click="saveConfig">
            <Check :size="13" />{{ cfgSaving ? '保存中…' : '保存配置' }}
          </button>
        </div>
      </div>

      <!-- 任务历史 -->
      <div class="seclabel" style="margin:22px 0 11px"><Clock :size="14" />任务历史 · 最近处理</div>

      <div v-if="tasks.length === 0" class="card pad" style="color:var(--ink-500);font-size:13px;text-align:center;padding:28px">
        暂无任务历史
      </div>
      <div v-else class="list">
        <TaskRow
          v-for="t in tasks"
          :key="`${t.job_id}-${t.step}-${t.finished_at}`"
          state="completed"
          :job-id="t.job_id"
          :step="t.step"
          :title="t.title"
          :content-type="t.content_type"
          :status="t.status"
          :duration-sec="t.duration_sec"
          :finished-at="t.finished_at"
          @deleted="load"
        />
      </div>
    </template>
  </section>
</template>
