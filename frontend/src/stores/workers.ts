import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { Worker, WorkerTask, FullStatus, SystemEvent, UsageAggregate, PricingStatus, QueueStatus } from '../types'

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

  async function fetchTasks(workerId: string): Promise<WorkerTask[]> {
    return await api.get<WorkerTask[]>(`/api/workers/${workerId}/tasks`)
  }

  // 任务队列只读视图:各池排队中 + 运行中 task。pool 给则只看单池。
  async function fetchQueue(pool?: string): Promise<QueueStatus> {
    const q = pool ? `?pool=${encodeURIComponent(pool)}` : ''
    return await api.get<QueueStatus>(`/api/queue${q}`)
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
  // 通联富时间线(按节点切片画趋势):/api/link-traffic/history → samples[](最近在前)。
  async function fetchLinkTrafficHistory(): Promise<Array<{ ts: number; gw?: any; tun?: any; t?: any; w?: any }>> {
    const r = await api.get<{ samples?: any[] }>('/api/link-traffic/history?limit=120')
    return r?.samples ?? []
  }

  // LiteLLM 价表:状态(更新时间 + 模型数)、手动更新(拉最新)。
  async function fetchPricing(): Promise<PricingStatus> {
    return await api.get<PricingStatus>('/api/pricing')
  }
  async function refreshPricing(): Promise<PricingStatus> {
    return await api.post<PricingStatus>('/api/pricing/refresh', {})
  }
  // 四条内容流水线只读视图(= configs/pipelines.yaml 单一事实源);AboutView 动态渲染,不再硬编码。
  async function fetchPipelines(): Promise<{ name: string; steps: { key: string; label: string | null; pool: string | null; needs: string[] }[] }[]> {
    const r = await api.get<{ pipelines?: any[] }>('/api/pipelines')
    return Array.isArray(r) ? r : (r?.pipelines ?? [])
  }

  return {
    workers, loading, fetchAll, pause, resume,
    updateNote, updateTags, remove, mintToken, fetchTasks, fetchQueue,
    fetchPoolLimits, savePoolLimits,
    fetchFullStatus, fetchEvents, fetchUsage, fetchLinkTrafficHistory,
    fetchPricing, refreshPricing, fetchPipelines,
  }
})
