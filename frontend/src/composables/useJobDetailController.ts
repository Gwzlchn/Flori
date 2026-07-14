import { computed, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { useRoute } from 'vue-router'
import { useJobStore } from '../stores/jobs'
import { useGlobalStore } from '../stores/global'
import { useJobWs } from './useJobWs'
import type { JobDetail } from '../types'

type LoadedHook = (detail: JobDetail, requestId: number) => void | Promise<void>

interface JobDetailControllerOptions {
  onReset?: () => void
  onLoaded?: LoadedHook
}

const TERMINAL = new Set(['done', 'failed', 'cancelled'])

// 路由代次、请求取消和 WS 状态合并集中在这里,各 panel 只消费当前 job 的状态。
export function useJobDetailController(options: JobDetailControllerOptions = {}) {
  const route = useRoute()
  const jobStore = useJobStore()
  const global = useGlobalStore()
  const jobId = computed(() => String(route.params.id || ''))
  const job = ref<JobDetail | null>(null)
  const loading = ref(true)
  const loadError = ref('')
  const requestEpoch = ref(0)
  const ws = useJobWs(jobId as ComputedRef<string>)
  const jobStatus = ref('processing')
  let activeRequest: AbortController | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null

  function isCurrent(requestId: number, expectedJobId: string): boolean {
    return requestId === requestEpoch.value && expectedJobId === jobId.value
  }

  function mergeStatus(status: string) {
    // 同一路由内终态不可被较晚到达的 HTTP processing 快照降级。
    if (TERMINAL.has(jobStatus.value) && !TERMINAL.has(status)) return
    jobStatus.value = status
  }

  watch(ws.jobStatus, mergeStatus)

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer)
    pollTimer = null
  }

  function startPolling(callback: () => void, intervalMs: number) {
    stopPolling()
    pollTimer = setInterval(callback, intervalMs)
  }

  async function fetchDetail() {
    const expectedJobId = jobId.value
    const requestId = ++requestEpoch.value
    activeRequest?.abort()
    activeRequest = new AbortController()
    loading.value = true
    loadError.value = ''
    try {
      const detail = await jobStore.fetchDetail(expectedJobId, activeRequest.signal)
      if (!isCurrent(requestId, expectedJobId)) return null
      job.value = detail
      mergeStatus(detail.status)
      ws.setInitialSteps(detail.steps)
      global.setCrumbs([
        { t: '知识库', to: '/' },
        ...(detail.domain ? [{ t: detail.domain, to: `/kb/${encodeURIComponent(detail.domain)}` }] : []),
        { t: detail.title || expectedJobId },
      ])
      await options.onLoaded?.(detail, requestId)
      return detail
    } catch (error: any) {
      if (error?.name === 'AbortError' || !isCurrent(requestId, expectedJobId)) return null
      loadError.value = error?.status === 404
        ? '内容不存在或已删除'
        : (error?.message || '加载失败')
      job.value = null
      return null
    } finally {
      if (isCurrent(requestId, expectedJobId)) loading.value = false
    }
  }

  function resetRoute() {
    ++requestEpoch.value
    activeRequest?.abort()
    activeRequest = null
    stopPolling()
    job.value = null
    loading.value = true
    loadError.value = ''
    jobStatus.value = 'processing'
    ws.reset?.()
    options.onReset?.()
  }

  onMounted(fetchDetail)
  watch(jobId, () => {
    resetRoute()
    void fetchDetail()
  })
  onBeforeUnmount(() => {
    ++requestEpoch.value
    activeRequest?.abort()
    stopPolling()
    global.setCrumbs(null)
  })

  return {
    jobId,
    job,
    loading,
    loadError,
    requestEpoch,
    steps: ws.steps,
    connected: ws.connected,
    jobStatus,
    fetchDetail,
    isCurrent,
    startPolling,
    stopPolling,
  }
}
