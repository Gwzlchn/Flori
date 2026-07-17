import { ref, watch, onUnmounted, type Ref } from 'vue'
import type { JobPartInfo, StepInfo, WsEvent } from '../types'
import { createWsReconnect } from './useWsReconnect'

export function applyJobStepEvent(
  steps: StepInfo[], parts: JobPartInfo[], event: WsEvent,
) {
  const match = event.step?.match(/^part:([^:]+)::(.+)$/)
  const part = match ? parts.find(item => item.part_id === match[1]) : null
  const templateStep = match?.[2] || event.step
  const step = part
    ? part.steps.find(item => item.name === templateStep)
    : steps.find(item => item.name === templateStep)

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
  }
  if (part) {
    const statuses = part.steps.map(item => item.status)
    const completed = statuses.filter(status => status === 'done' || status === 'skipped').length
    part.progress_pct = statuses.length ? Math.round(100 * completed / statuses.length) : 0
    if (statuses.includes('failed')) part.status = 'failed'
    else if (statuses.includes('running')) part.status = 'running'
    else if (statuses.length && statuses.every(status => status === 'done' || status === 'skipped')) part.status = 'done'
    else part.status = 'pending'
  }
}

export function useJobWs(jobId: Ref<string>) {
  const steps = ref<StepInfo[]>([])
  const parts = ref<JobPartInfo[]>([])
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
    applyJobStepEvent(steps.value, parts.value, event)
    switch (event.event) {
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

  function setInitialParts(initialParts: JobPartInfo[]) {
    parts.value = initialParts
  }

  function reset() {
    steps.value = []
    parts.value = []
    jobStatus.value = 'processing'
  }

  watch(jobId, (newId, oldId) => {
    if (oldId) conn.disconnect()
    reset()
    if (newId) conn.connect()
  }, { immediate: true })

  onUnmounted(conn.disconnect)

  return {
    steps, parts, jobStatus, connected: conn.connected,
    setInitialSteps, setInitialParts, reset,
  }
}
