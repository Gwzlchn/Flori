import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useApi } from '../composables/useApi'
import type { DomainOverview, TopicConcept, CreateDomainPayload, ConceptTimeline, TimelineGranularity, ConceptGraph } from '../types'

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

  // 新建知识库：写 profile 元数据,领域随即出现在总览;建后刷新列表。
  async function create(payload: CreateDomainPayload): Promise<DomainOverview> {
    const created = await api.post<DomainOverview>('/api/domains', payload)
    await fetchAll()
    return created
  }

  // 概念时间线聚合(按粒度分桶)。
  async function conceptTimeline(domain: string, granularity: TimelineGranularity = 'month'): Promise<ConceptTimeline> {
    return api.get<ConceptTimeline>(`/api/domains/${encodeURIComponent(domain)}/concept-timeline?granularity=${granularity}`)
  }

  // 概念图谱:节点=概念,边=共现(两概念引用同一 job),权重=共享 job 数。{nodes, edges, stats}。
  async function conceptGraph(domain: string): Promise<ConceptGraph> {
    return api.get<ConceptGraph>(`/api/domains/${encodeURIComponent(domain)}/concept-graph`)
  }

  // 改知识库展示元数据(显示名/图标/配色),不改英文 domain key;改后刷新列表让侧栏即时更新。
  // 复用已有 PUT /api/profiles/{domain}(部分合并 display_name/icon/color,保留 terminology);
  // 不另开端点避免同一份 yaml meta 持久化两处分叉。空值经侧栏 display_name||domain / resolveIcon 回退。
  async function updateMeta(
    domain: string,
    patch: { display_name?: string; icon?: string; color?: string },
  ): Promise<void> {
    await api.put(`/api/profiles/${encodeURIComponent(domain)}`, patch)
    await fetchAll()
  }

  // 改英文 domain key(二期 issue1-b):事务迁移该领域下所有 job/集合/术语 + profile 文件。
  // 后端校验 new 合法且不与现有领域冲突(否则 409)。改后刷新列表;返回新 key 供调用方跳转。
  async function renameKey(domain: string, newDomain: string): Promise<string> {
    const r = await api.post<{ new: string }>(
      `/api/domains/${encodeURIComponent(domain)}/rename`, { new_domain: newDomain },
    )
    await fetchAll()
    return r.new
  }

  return { domains, loading, fetchAll, workspace, term, topic, topicConcepts, create, conceptTimeline, conceptGraph, updateMeta, renameKey }
})
