<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import type { StepInfo } from '../../types'
import { useApi } from '../../composables/useApi'
import { Check, X, Minus, Loader, ChevronDown, ChevronRight, Download } from 'lucide-vue-next'

const props = defineProps<{ steps: StepInfo[]; jobId: string }>()
const api = useApi()

const statusIcon: Record<string, any> = {
  done: Check,
  failed: X,
  skipped: Minus,
  running: Loader,
}

const statusColor: Record<string, string> = {
  done: 'bg-green-500 text-white',
  failed: 'bg-red-500 text-white',
  running: 'bg-blue-500 text-white animate-pulse',
  skipped: 'bg-gray-300 text-gray-500',
  waiting: 'bg-gray-200 text-gray-400',
  ready: 'bg-yellow-400 text-white',
}

const lineColor: Record<string, string> = {
  done: 'bg-green-500',
  failed: 'bg-red-500',
  running: 'bg-blue-500',
  skipped: 'bg-gray-300',
}

const statusText: Record<string, string> = {
  done: '完成',
  failed: '失败',
  running: '进行中',
  skipped: '跳过',
  waiting: '等待',
  ready: '就绪',
}

const expanded = ref<Record<string, boolean>>({})
const logs = ref<Record<string, string>>({})
const logLoading = ref<Record<string, boolean>>({})
const logError = ref<Record<string, string>>({})

// 运行中步骤展开后轮询日志,做准实时 tail;done/failed 停轮询并最后拉一次冻结。
const logTimers: Record<string, ReturnType<typeof setInterval>> = {}

function canExpand(step: StepInfo): boolean {
  return step.status !== 'waiting' && step.status !== 'ready'
}

function logUrl(step: string, raw = false): string {
  return `/api/jobs/${props.jobId}/steps/${step}/log${raw ? '?raw=1' : ''}`
}

async function fetchLog(step: string): Promise<void> {
  try {
    logs.value[step] = await api.getText(logUrl(step))
    logError.value[step] = ''
  } catch (e: any) {
    if (logs.value[step] === undefined) {
      logError.value[step] = e?.status === 404 ? '该步骤暂无日志' : (e?.message || '日志加载失败')
    }
  }
}

function stopLogPolling(step: string): void {
  if (logTimers[step]) {
    clearInterval(logTimers[step])
    delete logTimers[step]
  }
}

function stepByName(name: string): StepInfo | undefined {
  return props.steps.find(s => s.name === name)
}

// 步骤跑完后停止轮询并最后拉一次,把日志冻结在终态。
watch(
  () => props.steps.map(s => `${s.name}:${s.status}`).join(','),
  () => {
    for (const name of Object.keys(logTimers)) {
      const s = stepByName(name)
      if (!s || s.status !== 'running') {
        stopLogPolling(name)
        if (expanded.value[name]) fetchLog(name)
      }
    }
  },
)

onUnmounted(() => {
  for (const name of Object.keys(logTimers)) stopLogPolling(name)
})

function stepPct(step: StepInfo): number | null {
  if (step.status === 'running' && step.meta?.pct != null) return step.meta.pct
  return null
}

function formatDuration(sec: number | null): string {
  if (sec == null) return ''
  if (sec < 60) return `${sec.toFixed(1)}s`
  return `${Math.floor(sec / 60)}m${Math.floor(sec % 60)}s`
}

async function toggle(step: StepInfo) {
  if (!canExpand(step)) return
  const n = step.name
  expanded.value[n] = !expanded.value[n]
  if (!expanded.value[n]) {
    stopLogPolling(n)
    return
  }
  if (logs.value[n] === undefined && !logLoading.value[n]) {
    logLoading.value[n] = true
    logError.value[n] = ''
    await fetchLog(n)
    logLoading.value[n] = false
  }
  // 运行中步骤展开后每 2.5s 拉一次,准实时 tail。
  if (step.status === 'running' && !logTimers[n]) {
    logTimers[n] = setInterval(() => fetchLog(n), 2500)
  }
}
</script>

<template>
  <!-- 纵向时间线:桌面/移动统一,完整步骤名 + 状态 + 失败原因 + 可展开日志 -->
  <div class="space-y-0">
    <div v-for="(step, idx) in steps" :key="step.name" class="flex gap-3">
      <!-- 节点 + 连接线 -->
      <div class="flex flex-col items-center">
        <button
          type="button"
          @click="toggle(step)"
          :class="[statusColor[step.status] || statusColor.waiting, canExpand(step) ? 'cursor-pointer' : 'cursor-default']"
          class="w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0"
        >
          <component :is="statusIcon[step.status]" v-if="statusIcon[step.status]" :size="14" />
          <span v-else class="text-xs">{{ idx + 1 }}</span>
        </button>
        <div v-if="idx < steps.length - 1" class="w-0.5 flex-1 min-h-[1.25rem] my-0.5" :class="lineColor[step.status] || 'bg-gray-200'" />
      </div>

      <!-- 内容 -->
      <div class="pb-4 min-w-0 flex-1">
        <div
          class="flex items-center gap-2 flex-wrap"
          :class="canExpand(step) ? 'cursor-pointer' : ''"
          @click="toggle(step)"
        >
          <span class="text-sm font-medium" :class="step.status === 'waiting' ? 'text-gray-400' : 'text-gray-800'">
            {{ step.name }}
          </span>
          <span class="text-xs px-1.5 py-0.5 rounded" :class="statusColor[step.status] || statusColor.waiting">
            {{ statusText[step.status] || step.status }}
          </span>
          <span v-if="step.duration_sec" class="text-xs text-gray-400">{{ formatDuration(step.duration_sec) }}</span>
          <component
            v-if="canExpand(step)"
            :is="expanded[step.name] ? ChevronDown : ChevronRight"
            :size="14"
            class="text-gray-400 ml-auto"
          />
        </div>

        <!-- 运行进度 -->
        <div v-if="stepPct(step) != null" class="mt-1 w-full bg-gray-200 rounded-full h-1.5">
          <div class="bg-blue-500 h-full rounded-full transition-all" :style="{ width: `${stepPct(step)}%` }" />
        </div>

        <!-- 失败原因(桌面也可见) -->
        <p v-if="step.error" class="text-xs text-red-600 mt-1 break-all">✗ {{ step.error }}</p>

        <!-- 可展开日志 -->
        <div v-if="expanded[step.name]" class="mt-2">
          <div v-if="logLoading[step.name]" class="text-xs text-gray-400">加载日志...</div>
          <div v-else-if="logError[step.name]" class="text-xs text-gray-400">{{ logError[step.name] }}</div>
          <template v-else>
            <div class="flex items-center justify-between mb-1">
              <span v-if="step.status === 'running'" class="text-xs text-blue-500">实时刷新中...</span>
              <span v-else></span>
              <a
                :href="logUrl(step.name, true)"
                target="_blank"
                rel="noopener"
                class="text-xs text-gray-500 hover:text-gray-700 inline-flex items-center gap-1"
              >
                <Download :size="12" /> 下载 raw
              </a>
            </div>
            <pre class="text-xs bg-gray-900 text-gray-100 rounded-lg p-3 max-h-80 overflow-auto whitespace-pre-wrap break-all">{{ logs[step.name] }}</pre>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>
