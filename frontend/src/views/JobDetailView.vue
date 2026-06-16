<script setup lang="ts">
import { ref, computed, onMounted, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useJobStore } from '../stores/jobs'
import { useJobWs } from '../composables/useJobWs'
import StepWorkbench from '../components/job/StepWorkbench.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import ConfirmDialog from '../components/common/ConfirmDialog.vue'
import type { JobDetail } from '../types'
import { CONTENT_TYPE_LABELS } from '../types'
import { fmtDateTime } from '../utils/datetime'
import { ArrowLeft, RotateCcw, Play, Trash2, BookOpen, FileText, Video, Newspaper, Headphones, ExternalLink } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const jobStore = useJobStore()
const showToast = inject<(msg: string, type: 'success' | 'error' | 'info') => void>('showToast')!

const jobId = computed(() => route.params.id as string)
const { steps, jobStatus, connected, setInitialSteps } = useJobWs(jobId)

// BV 号:job id 形如 jobs_bili_BV1Kth…,无则不显示(非 B 站来源)。
const bv = computed(() => jobId.value.match(/_(BV[0-9A-Za-z]+)/)?.[1] ?? null)

// 生成时间:开始=所有步骤最早 started_at,结束=最晚 finished_at(有步骤在跑则「进行中」),
// 总耗时=结束-开始;未完成显示 --。
function fmtDur(sec: number | null): string {
  if (sec == null) return '--'
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60)
  return h ? `${h}h${m}m` : m ? `${m}m${s}s` : `${s}s`
}
const anyRunning = computed(() => steps.value.some(s => s.status === 'running'))
const genStart = computed(() => {
  const t = steps.value.map(s => s.started_at).filter(Boolean).map(x => +new Date(x as string))
  return t.length ? Math.min(...t) : null
})
const genEnd = computed(() => {
  if (anyRunning.value) return null
  const t = steps.value.map(s => s.finished_at).filter(Boolean).map(x => +new Date(x as string))
  return t.length ? Math.max(...t) : null
})
const genDurSec = computed(() => (genStart.value && genEnd.value ? (genEnd.value - genStart.value) / 1000 : null))

const job = ref<JobDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const showDelete = ref(false)
const rerunStep = ref('')

async function fetchDetail() {
  loading.value = true
  loadError.value = ''
  try {
    const detail = await jobStore.fetchDetail(jobId.value)
    job.value = detail
    jobStatus.value = detail.status
    setInitialSteps(detail.steps)
  } catch (e: any) {
    loadError.value = e?.status === 404 ? '任务不存在或已删除' : (e?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(fetchDetail)

const contentIcon = computed(() => {
  if (job.value?.content_type === 'video') return Video
  if (job.value?.content_type === 'paper') return FileText
  if (job.value?.content_type === 'article') return Newspaper
  if (job.value?.content_type === 'audio') return Headphones
  return FileText
})

async function retry() {
  try {
    await jobStore.retryJob(jobId.value)
    showToast('已提交重试', 'success')
    jobStatus.value = 'processing'
  } catch (e: any) {
    showToast(e.message, 'error')
  }
}

async function rerun() {
  if (!rerunStep.value) return
  try {
    await jobStore.rerunJob(jobId.value, rerunStep.value)
    showToast(`从 ${rerunStep.value} 开始重跑`, 'success')
    jobStatus.value = 'processing'
  } catch (e: any) {
    showToast(e.message, 'error')
  }
}

async function confirmDelete() {
  try {
    await jobStore.deleteJob(jobId.value)
    showToast('已删除', 'success')
    router.push('/jobs')
  } catch (e: any) {
    showToast(e.message, 'error')
  }
  showDelete.value = false
}
</script>

<template>
  <div class="space-y-4">
    <button @click="router.push('/jobs')" class="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">
      <ArrowLeft :size="16" />
      返回任务列表
    </button>

    <div v-if="loading" class="text-sm text-gray-400 py-8 text-center">加载中...</div>

    <div v-else-if="loadError" class="bg-white border border-gray-200 rounded-xl p-8 flex flex-col items-center text-center">
      <p class="text-sm text-gray-600">{{ loadError }}</p>
      <button @click="fetchDetail" class="mt-4 flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors">
        <RotateCcw :size="14" />
        重新加载
      </button>
    </div>

    <template v-else-if="job">
      <!-- Header -->
      <div class="bg-white border border-gray-200 rounded-xl p-4">
        <div class="flex items-start gap-3">
          <div class="w-10 h-10 rounded-lg bg-gray-100 flex items-center justify-center flex-shrink-0">
            <component :is="contentIcon" :size="20" class="text-gray-500" />
          </div>
          <div class="flex-1 min-w-0">
            <h2 class="text-lg font-bold truncate">{{ job.title || job.job_id }}</h2>
            <div class="flex flex-wrap items-center gap-2 mt-1 text-sm text-gray-500">
              <StatusBadge :status="jobStatus" />
              <span class="px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 text-xs">{{ CONTENT_TYPE_LABELS[job.content_type] || job.content_type }}</span>
              <span v-if="job.source">{{ job.source }}</span>
              <span v-if="job.domain !== 'general'">{{ job.domain }}</span>
            </div>
            <!-- 元信息:BV / 原始链接 / 创建·更新时间 -->
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-xs text-gray-500">
              <span v-if="bv">BV号 <span class="font-mono text-gray-700">{{ bv }}</span></span>
              <a
                v-if="job.url" :href="job.url" target="_blank" rel="noopener"
                class="inline-flex items-center gap-1 text-blue-600 hover:text-blue-700"
              >
                原始链接 <ExternalLink :size="12" />
              </a>
              <span>上传于 {{ fmtDateTime(job.published_at) }}</span>
              <span>
                生成 {{ fmtDateTime(genStart) }} →
                {{ anyRunning ? '进行中' : (genEnd ? fmtDateTime(genEnd) : '--') }}
                · 耗时 {{ genEnd ? fmtDur(genDurSec) : '--' }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- WS connection indicator -->
      <div v-if="jobStatus === 'processing'" class="flex items-center gap-2 text-xs text-gray-500">
        <span class="w-2 h-2 rounded-full" :class="connected ? 'bg-green-500' : 'bg-red-500'" />
        {{ connected ? '实时更新中' : '连接断开，重连中...' }}
      </div>

      <!-- Actions -->
      <div class="flex flex-wrap gap-2">
        <!-- Done: view outputs -->
        <template v-if="jobStatus === 'done'">
          <router-link :to="`/jobs/${jobId}/notes/smart`" class="flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors">
            <BookOpen :size="14" />
            查看笔记
          </router-link>
          <router-link v-if="job.content_type === 'video'" :to="`/jobs/${jobId}/notes/mechanical`" class="flex items-center gap-1.5 px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 transition-colors">
            <FileText :size="14" />
            机械版
          </router-link>
        </template>

        <!-- Failed: retry -->
        <button v-if="jobStatus === 'failed'" @click="retry" class="flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors">
          <RotateCcw :size="14" />
          重试
        </button>

        <!-- Rerun from step -->
        <div v-if="jobStatus === 'done' || jobStatus === 'failed'" class="flex items-center gap-2">
          <select v-model="rerunStep" class="px-2 py-2 border border-gray-300 rounded-lg text-sm bg-white">
            <option value="">从步骤重跑...</option>
            <option v-for="s in steps" :key="s.name" :value="s.name">{{ s.name }}</option>
          </select>
          <button
            v-if="rerunStep"
            @click="rerun"
            class="flex items-center gap-1 px-3 py-2 bg-yellow-500 text-white rounded-lg text-sm hover:bg-yellow-600 transition-colors"
          >
            <Play :size="14" />
            重跑
          </button>
        </div>

        <div class="flex-1" />

        <button @click="showDelete = true" class="flex items-center gap-1 px-3 py-2 text-red-600 hover:bg-red-50 rounded-lg text-sm transition-colors">
          <Trash2 :size="14" />
          删除
        </button>
      </div>

      <!-- 步骤与产物(左右分栏:左步骤时间线,右选中步骤的可读详情+产物) -->
      <StepWorkbench :job-id="jobId" :steps="steps" />
    </template>

    <ConfirmDialog
      v-if="showDelete"
      title="删除任务"
      message="确定要删除此任务及所有产物？此操作不可恢复。"
      confirm-text="删除"
      :danger="true"
      @confirm="confirmDelete"
      @cancel="showDelete = false"
    />
  </div>
</template>
