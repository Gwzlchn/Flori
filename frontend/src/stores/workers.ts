import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { Worker, WorkerJob, FullStatus, SystemEvent, UsageAggregate, PricingStatus } from '../types'

export const useWorkerStore = defineStore('workers', () => {
  const api = useApi()
  const workers = ref<Worker[]>([])
  const loading = ref(false)

  async function fetchAll() {
    loading.value = true
    try {
      workers.value = await api.get<Worker[]>('/api/workers')
    } finally {
      loading.value = false
    }
  }

  async function pause(workerId: string) {
    await api.put(`/api/workers/${workerId}`, { status: 'paused' })
    await fetchAll()
  }

  async function resume(workerId: string) {
    await api.put(`/api/workers/${workerId}`, { status: 'active' })
    await fetchAll()
  }

  async function updateNote(workerId: string, note: string) {
    await api.put(`/api/workers/${workerId}`, { admin_note: note })
    await fetchAll()
  }

  async function updateTags(workerId: string, tags: string[]) {
    await api.put(`/api/workers/${workerId}`, { tags })
    await fetchAll()
  }

  async function remove(workerId: string, force = false) {
    await api.del(`/api/workers/${workerId}${force ? '?force=true' : ''}`)
    await fetchAll()
  }

  async function mintToken(): Promise<string> {
    const res = await api.post<{ token: string }>('/api/workers/registration-token', {})
    return res.token
  }

  async function fetchJobs(workerId: string): Promise<WorkerJob[]> {
    return await api.get<WorkerJob[]>(`/api/workers/${workerId}/jobs`)
  }

  // 系统池上限:default(pools.yaml)+ override(redis 运行时覆盖,可为 null)。
  async function fetchPoolLimits(): Promise<Record<string, { default: number; override: number | null }>> {
    return await api.get('/api/config/pool-limits')
  }
  async function savePoolLimits(body: Record<string, number | null>) {
    await api.put('/api/config/pool-limits', body)
  }

  // 系统健康总览页:全量状态(version + 组件 + 四段 + throughput)、事件流、AI 用量聚合。
  async function fetchFullStatus(): Promise<FullStatus> {
    return await api.get<FullStatus>('/api/status')
  }
  async function fetchEvents(limit = 50): Promise<{ events: SystemEvent[] }> {
    return await api.get(`/api/events?limit=${limit}`)
  }
  async function fetchUsage(): Promise<UsageAggregate> {
    return await api.get<UsageAggregate>('/api/usage')
  }

  // LiteLLM 价表:状态(更新时间 + 模型数)、手动更新(拉最新)。
  async function fetchPricing(): Promise<PricingStatus> {
    return await api.get<PricingStatus>('/api/pricing')
  }
  async function refreshPricing(): Promise<PricingStatus> {
    return await api.post<PricingStatus>('/api/pricing/refresh', {})
  }

  return {
    workers, loading, fetchAll, pause, resume,
    updateNote, updateTags, remove, mintToken, fetchJobs,
    fetchPoolLimits, savePoolLimits,
    fetchFullStatus, fetchEvents, fetchUsage,
    fetchPricing, refreshPricing,
  }
})
