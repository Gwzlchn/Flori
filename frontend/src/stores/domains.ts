import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { DomainOverview, TopicConcept } from '../types'

// 领域 store：领域是派生视图（来自 jobs ∪ collections ∪ glossary 的 distinct domain）。
export const useDomainStore = defineStore('domains', () => {
  const api = useApi()
  const domains = ref<DomainOverview[]>([])
  const loading = ref(false)

  async function fetchAll() {
    loading.value = true
    try {
      domains.value = (await api.get<{ domains: DomainOverview[] }>('/api/domains')).domains
    } finally {
      loading.value = false
    }
  }

  // 领域工作台聚合 {domain, stats, collections, recent_jobs, top_concepts, topics, suggested_count}
  async function workspace(domain: string): Promise<any> {
    return api.get(`/api/domains/${encodeURIComponent(domain)}`)
  }
  // 术语详情 {domain, term, definition, related, sources/occurrences, ...}
  async function term(domain: string, t: string): Promise<any> {
    return api.get(`/api/domains/${encodeURIComponent(domain)}/terms/${encodeURIComponent(t)}`)
  }
  // 主题页 {domain, topic, jobs, total}
  async function topic(domain: string, t: string): Promise<any> {
    return api.get(`/api/domains/${encodeURIComponent(domain)}/topics/${encodeURIComponent(t)}`)
  }
  // 概念主题：域内 is_topic=1 的概念列表（空则 []）。
  async function topicConcepts(domain: string): Promise<TopicConcept[]> {
    return api.get<TopicConcept[]>(`/api/domains/${encodeURIComponent(domain)}/topic-concepts`)
  }

  return { domains, loading, fetchAll, workspace, term, topic, topicConcepts }
})
