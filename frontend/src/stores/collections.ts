import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { Collection, JobListResponse } from '../types'

// 集合 CRUD store：列表/建/改/删（删=解绑，job 保留）。
export const useCollectionStore = defineStore('collections', () => {
  const api = useApi()
  const collections = ref<Collection[]>([])
  const loading = ref(false)

  async function fetchAll(domain?: string) {
    loading.value = true
    try {
      const q = domain ? `?domain=${encodeURIComponent(domain)}` : ''
      collections.value = await api.get<Collection[]>(`/api/collections${q}`)
    } finally {
      loading.value = false
    }
  }

  async function get(id: string): Promise<Collection> {
    return await api.get<Collection>(`/api/collections/${id}`)
  }

  async function create(payload: {
    name: string
    domain: string
    description?: string
    tags?: string[]
    source_type?: string   // 订阅集合：bilibili_up
    source_id?: string     // 订阅集合：UP mid
  }): Promise<Collection> {
    const c = await api.post<Collection>('/api/collections', payload)
    await fetchAll()
    return c
  }

  async function update(
    id: string,
    payload: { name?: string; description?: string; tags?: string[] },
  ): Promise<Collection> {
    const c = await api.put<Collection>(`/api/collections/${id}`, payload)
    await fetchAll()
    return c
  }

  async function remove(id: string) {
    await api.del(`/api/collections/${id}`)
    await fetchAll()
  }

  async function fetchJobs(
    id: string,
    limit = 20,
    offset = 0,
  ): Promise<JobListResponse> {
    return await api.get<JobListResponse>(
      `/api/collections/${id}/jobs?limit=${limit}&offset=${offset}`,
    )
  }

  return {
    collections, loading,
    fetchAll, get, create, update, remove, fetchJobs,
  }
})
