import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { JobSummary, JobDetail, JobListResponse, JobFacets, JobConcept } from '../types'
import { contentTypeForUpload } from '../constants/sources'

export const useJobStore = defineStore('jobs', () => {
  const api = useApi()
  const list = ref<JobSummary[]>([])
  const total = ref(0)
  const loading = ref(false)

  async function fetchList(params: { status?: string; domain?: string; source?: string; collection_id?: string; limit?: number; offset?: number; append?: boolean } = {}) {
    loading.value = true
    try {
      const qs = new URLSearchParams()
      if (params.status) qs.set('status', params.status)
      if (params.domain) qs.set('domain', params.domain)
      if (params.source) qs.set('source', params.source)
      if (params.collection_id) qs.set('collection_id', params.collection_id)
      qs.set('limit', String(params.limit ?? 20))
      qs.set('offset', String(params.offset ?? 0))
      const data = await api.get<JobListResponse>(`/api/jobs?${qs}`)
      if (params.append) {
        list.value.push(...data.items)
      } else {
        list.value = data.items
      }
      total.value = data.total
    } finally {
      loading.value = false
    }
  }

  async function fetchDetail(jobId: string, signal?: AbortSignal): Promise<JobDetail> {
    if (signal) return api.get<JobDetail>(`/api/jobs/${jobId}`, signal)
    return api.get<JobDetail>(`/api/jobs/${jobId}`)
  }

  async function createJob(payload: { url?: string; content_type?: string; domain?: string; style_tags?: string[]; collection_id?: string; smart_note?: boolean }) {
    return api.post<{ job_id: string }>('/api/jobs', payload)
  }

  async function uploadJob(file: File, domain: string, styleTags: string[]) {
    const contentType = contentTypeForUpload(file.name)
    if (!contentType) throw new Error('不支持的上传文件类型')
    const form = new FormData()
    form.append('file', file)
    form.append('domain', domain)
    form.append('style_tags', JSON.stringify(styleTags))
    return api.upload<{ job_id: string }>(
      `/api/jobs/upload?content_type=${encodeURIComponent(contentType)}`,
      form,
    )
  }

  async function retryJob(jobId: string) {
    return api.post(`/api/jobs/${jobId}/retry`)
  }

  async function retryAllFailed(): Promise<{ retried: number }> {
    return api.post<{ retried: number }>('/api/jobs/retry-failed')
  }

  // 仅重试某集合下的失败 job(scoped 批量重试,复用 retry-failed + collection_id 过滤)。
  async function retryFailedInCollection(collectionId: string): Promise<{ retried: number }> {
    return api.post<{ retried: number }>(
      `/api/jobs/retry-failed?collection_id=${encodeURIComponent(collectionId)}`,
    )
  }

  async function rerunJob(jobId: string, fromStep: string) {
    return api.post(`/api/jobs/${jobId}/rerun`, { from_step: fromStep })
  }

  // 重建为新快照:fork 父 job,只重跑分叉步及下游;旧快照保留供 A/B 对比。返回新 job_id。
  async function rebuildJob(jobId: string): Promise<{ job_id: string }> {
    return api.post<{ job_id: string }>(`/api/jobs/${jobId}/rebuild`)
  }

  // 批量重建所有"过期"(pipeline 定义已变)的 current job 为新快照。
  async function rebuildStale(): Promise<{ rebuilt: number }> {
    return api.post<{ rebuilt: number }>('/api/jobs/rebuild-stale')
  }

  async function deleteJob(jobId: string) {
    await api.del(`/api/jobs/${jobId}`)
  }

  // 批量删除:逐条走单删端点(各自后端精准级联);串行避免瞬时打爆。返回成功删除数。
  async function deleteJobs(ids: string[]): Promise<number> {
    let ok = 0
    for (const id of ids) {
      await deleteJob(id)
      ok++
    }
    return ok
  }

  // 分面计数(后端聚合,供过滤 chip)
  async function fetchFacets() {
    return api.get<JobFacets>('/api/jobs/facets')
  }

  // 本内容命中的概念(反查)
  async function fetchConcepts(jobId: string) {
    return api.get<JobConcept[]>(`/api/jobs/${encodeURIComponent(jobId)}/concepts`)
  }

  return { list, total, loading, fetchList, fetchDetail, createJob, uploadJob, retryJob, retryAllFailed, retryFailedInCollection, rerunJob, rebuildJob, rebuildStale, deleteJob, deleteJobs, fetchFacets, fetchConcepts }
})
