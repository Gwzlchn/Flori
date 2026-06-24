import { ref, watch, onUnmounted, type Ref } from 'vue'
import type { StepInfo, WsEvent } from '../types'
import { createWsReconnect } from './useWsReconnect'

export function useJobWs(jobId: Ref<string>) {
  const steps = ref<StepInfo[]>([])
  const jobStatus = ref('processing')

  // 与 global 共用 createWsReconnect 脚手架(退避 / 清理一致);job 端点在终态(done/failed)
  // 自然停连,故不设 maxRetries。
  const conn = createWsReconnect({
    url: () => (jobId.value ? `/api/ws/jobs/${jobId.value}` : null),
    withToken: true,
    shouldReconnect: () => jobStatus.value === 'processing' || jobStatus.value === 'pending',
    onMessage: (data) => handleEvent(JSON.parse(data) as WsEvent),
  })

  function handleEvent(event: WsEvent) {
    const step = steps.value.find(s => s.name === event.step)

    switch (event.event) {
      case 'step_ready':
        if (step) step.status = 'ready'
        break
      case 'step_start':
        if (step) step.status = 'running'
        break
      case 'step_progress':
        if (step) {
          step.status = 'running'
          // message:运行中的实时进度文案(如「扫描关键帧」),供 workbench 显示;契约 §WS。
          step.meta = { ...step.meta, pct: event.pct, current: event.current, total: event.total, message: event.message }
        }
        break
      case 'step_done':
        if (step) {
          step.status = 'done'
          step.duration_sec = event.duration_sec ?? null
          if (event.meta) step.meta = { ...step.meta, ...event.meta }
        }
        break
      case 'step_failed':
        if (step) {
          step.status = 'failed'
          step.error = event.error ?? null
        }
        break
      case 'step_skipped':
        if (step) {
          step.status = 'skipped'
          step.meta = { ...step.meta, reason: event.reason }
        }
        break
      case 'job_done':
        jobStatus.value = 'done'
        break
      case 'job_failed':
        jobStatus.value = 'failed'
        break
    }
  }

  function setInitialSteps(initialSteps: StepInfo[]) {
    steps.value = initialSteps
  }

  watch(jobId, (newId, oldId) => {
    if (oldId) conn.disconnect()
    if (newId) conn.connect()
  }, { immediate: true })

  onUnmounted(conn.disconnect)

  return { steps, jobStatus, connected: conn.connected, setInitialSteps }
}
