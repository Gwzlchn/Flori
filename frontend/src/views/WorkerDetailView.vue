<script setup lang="ts">
// Worker 详情（原型 #wdetail）：单个 worker 完整统计 + 基本信息 + 任务历史(recent_tasks)
// + 备注编辑 + 暂停/移除。worker 主体走 GET /api/workers/{id}；历史走 store.fetchJobs(id)。
import { ref, computed, onMounted, onBeforeUnmount, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useWorkerStore } from '../stores/workers'
import { useGlobalStore } from '../stores/global'
import { fmtDateTime, fmtDuration, fmtRelative } from '../utils/datetime'
import { workerDotClass, workerComputeDesc } from '../utils/worker'
import StatusBadge from '../components/common/StatusBadge.vue'
import type { Worker, WorkerJob } from '../types'
import {
  RefreshCw, Pause, X, Cpu, Info, Layers, Clock, Check,
  Play, FileText, Newspaper, Headphones, ChevronRight, MessageSquare,
} from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const api = useApi()
const workerStore = useWorkerStore()
const global = useGlobalStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const workerId = computed(() => String(route.params.id))

const worker = ref<Worker | null>(null)
const tasks = ref<WorkerJob[]>([])
const loading = ref(true)
const error = ref('')
const busy = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 主体 + 历史并行；历史失败不致命（仅置空）。
    const [w, jobs] = await Promise.all([
      api.get<Worker>(`/api/workers/${encodeURIComponent(workerId.value)}`),
      workerStore.fetchJobs(workerId.value).catch(() => [] as WorkerJob[]),
    ])
    worker.value = w
    tasks.value = jobs
    // 面包屑显真实 worker id(替代通用「Worker 详情」)
    global.setCrumbs([{ t: '系统', to: '/system' }, { t: w.id }])
  } catch (e: any) {
    error.value = e?.status === 404 ? 'Worker 不存在或已移除' : (e?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

// ── 派生统计 ──
const isOnline = computed(() => worker.value?.status.startsWith('online') ?? false)
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

// 时长走 utils/datetime.fmtDuration;相对时间(心跳/完成时间)用 fmtRelative(中文单位,超 1 天回退绝对时间)。
const ago = (v: string | null | undefined) => fmtRelative(v, { style: 'cn', absoluteAfterDay: true })

// 算力描述：GPU 名优先，否则按类型;无 worker 时回退 —。
const computeDesc = computed(() => (worker.value ? workerComputeDesc(worker.value) : '—'))

// 任务历史 type-pill（按 step 无内容类型，统一用中性图标兜底）。
const STEP_ICON: Record<string, any> = { download: Play, transcribe: Headphones }
function stepIcon(step: string): any {
  const key = step.replace(/^\d+_/, '')
  return STEP_ICON[key] || FileText
}

// ── 操作：暂停 / 继续 / 移除 / 备注 ──
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
  if (!confirm(`确定移除 Worker ${workerId.value}？离线 worker 重新接入会重新出现。`)) return
  busy.value = true
  try {
    // 在线 worker 需 force 才能移除（避免误删活跃节点）。
    await workerStore.remove(workerId.value, isOnline.value)
    showToast('已移除', 'success')
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
          <button v-if="isOnline || worker.status === 'paused'" class="btn sm" :disabled="busy" @click="togglePause">
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
            <tr><td>算力</td><td>{{ computeDesc }}</td></tr>
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

      <!-- 任务历史 -->
      <div class="seclabel" style="margin:22px 0 11px"><Clock :size="14" />任务历史 · 最近处理</div>

      <div v-if="tasks.length === 0" class="card pad" style="color:var(--ink-500);font-size:13px;text-align:center;padding:28px">
        暂无任务历史
      </div>
      <div v-else class="list">
        <div
          v-for="t in tasks"
          :key="`${t.job_id}-${t.step}-${t.finished_at}`"
          class="row"
          style="cursor:pointer"
          @click="router.push(`/content/${encodeURIComponent(t.job_id)}`)"
        >
          <span class="type-pill" style="background:var(--mut-bg);color:var(--ink-600)">
            <component :is="stepIcon(t.step)" :size="17" />
          </span>
          <div class="body">
            <div class="title mono" style="font-size:13.5px">{{ t.job_id }}</div>
            <div class="meta">
              <span class="mono">{{ t.step }}</span>
              <StatusBadge :status="t.status" />
              <span>{{ fmtDuration(t.duration_sec) }}</span>
              <span class="sep">·</span>
              <span>{{ ago(t.finished_at) }}</span>
            </div>
          </div>
          <ChevronRight :size="16" class="dim" />
        </div>
      </div>
    </template>
  </section>
</template>
