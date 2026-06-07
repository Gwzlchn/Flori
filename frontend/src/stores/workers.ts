import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { Worker, WorkerJob } from '../types'

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

  async function drain(workerId: string) {
    await api.put(`/api/workers/${workerId}`, { status: 'draining' })
    await fetchAll()
  }

  async function undrain(workerId: string) {
    await api.put(`/api/workers/${workerId}`, { status: 'idle' })
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

  return {
    workers, loading, fetchAll, drain, undrain,
    updateNote, updateTags, remove, mintToken, fetchJobs,
  }
})
