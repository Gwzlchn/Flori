<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import JobCard from '../components/job/JobCard.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { fmtDateTime } from '../utils/datetime'
import type { JobSummary } from '../types'
import {
  ArrowLeft, Rss, Library, Star, RefreshCw, ChevronRight,
  Layers, BrainCircuit, AlertTriangle,
} from 'lucide-vue-next'

// 领域工作台：左「情景层(看了什么)」= 集合 + 主题 + 最近内容；右「语义层(学会了什么)」= 概念 + 待确认。
// 数据一次性来自 useDomainStore.workspace(domain)；其余交互(投递/Profile/同步/图谱)在各归口页，本页只做总览与导航。

interface DomainStats {
  collection_count: number
  job_count: number
  concept_count: number
  subscription_count: number
  last_active_at: string | null
}
interface WsCollection {
  id: string
  name: string
  job_count: number
  is_subscription: boolean
  source_id: string | null
  sync_enabled: boolean
}
interface WsConcept {
  term: string
  definition: string
  source_count: number
  status: string
}
interface WsTopic {
  topic: string
  count: number
}
interface Workspace {
  domain: string
  stats: DomainStats
  collections: WsCollection[]
  recent_jobs: JobSummary[]
  top_concepts: WsConcept[]
  topics: WsTopic[]
  suggested_count: number
}

const route = useRoute()
const router = useRouter()
const store = useDomainStore()

const domain = computed(() => String(route.params.domain))
const data = ref<Workspace | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const ws = (await store.workspace(domain.value)) as Workspace
    data.value = ws
  } catch (e: any) {
    error.value = String(e?.message ?? '') || '加载失败'
    data.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
// 同一组件复用时(切换领域)重新拉取。
watch(domain, load)

// 概念按 source_count 降序，强度 ★(1~5) 由源数派生。
const sortedConcepts = computed<WsConcept[]>(() =>
  [...(data.value?.top_concepts ?? [])].sort((a, b) => b.source_count - a.source_count),
)
function strength(sourceCount: number): number {
  return Math.max(1, Math.min(5, sourceCount))
}

const stats = computed<DomainStats | null>(() => data.value?.stats ?? null)

function goCollection(id: string) {
  router.push(`/collections/${id}`)
}
function goTopic(topic: string) {
  router.push(`/domains/${encodeURIComponent(domain.value)}/topics/${encodeURIComponent(topic)}`)
}
function goTerm(term: string) {
  router.push(`/domains/${encodeURIComponent(domain.value)}/terms/${encodeURIComponent(term)}`)
}
</script>

<template>
  <div class="space-y-4">
    <!-- 头部：返回 + 领域名 + 统计 -->
    <div class="flex items-center gap-2">
      <button
        @click="router.push('/')"
        class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
        title="返回领域总览"
      >
        <ArrowLeft :size="18" />
      </button>
      <div class="min-w-0">
        <h2 class="text-xl font-bold truncate">{{ domain }}</h2>
        <div v-if="stats" class="flex items-center gap-2 text-xs text-gray-500 flex-wrap mt-0.5">
          <span>{{ stats.collection_count }} 集合</span>
          <span class="text-gray-300">·</span>
          <span>{{ stats.job_count }} 内容</span>
          <span class="text-gray-300">·</span>
          <span>{{ stats.concept_count }} 概念</span>
          <span v-if="stats.last_active_at" class="text-gray-300">·</span>
          <span v-if="stats.last_active_at">{{ fmtDateTime(stats.last_active_at) }} 活跃</span>
        </div>
      </div>
      <button
        @click="load()"
        class="ml-auto p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
        title="刷新"
      >
        <RefreshCw :size="16" :class="loading ? 'animate-spin' : ''" />
      </button>
    </div>

    <!-- 加载态 -->
    <div v-if="loading && !data" class="text-sm text-gray-400 py-12 text-center">加载中...</div>

    <!-- 错误态 -->
    <div v-else-if="error && !data" class="bg-white border border-gray-200 rounded-xl p-8 text-center">
      <p class="text-sm text-red-600">{{ error }}</p>
      <button
        @click="load()"
        class="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors"
      >
        <RefreshCw :size="14" />重试
      </button>
    </div>

    <!-- 空领域态 -->
    <div v-else-if="data && stats && stats.job_count === 0 && stats.collection_count === 0">
      <EmptyState message="该领域还没有内容" />
    </div>

    <!-- 两栏：左情景层 / 右语义层 -->
    <div v-else-if="data" class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <!-- 左：情景层 -->
      <div class="space-y-4">
        <div class="flex items-center gap-1.5 text-sm font-semibold text-gray-700">
          <Layers :size="15" class="text-gray-400" />
          情景层 · 看了什么
        </div>

        <!-- 集合 -->
        <div class="bg-white border border-gray-200 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-gray-700 mb-3">集合</h3>
          <div v-if="data.collections.length === 0">
            <EmptyState message="暂无集合" />
          </div>
          <div v-else class="space-y-2">
            <button
              v-for="c in data.collections"
              :key="c.id"
              @click="goCollection(c.id)"
              class="w-full flex items-center gap-3 text-left p-2 rounded-lg hover:bg-gray-50 transition-colors"
            >
              <div
                class="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
                :class="c.is_subscription ? 'bg-blue-50' : 'bg-gray-100'"
              >
                <Rss v-if="c.is_subscription" :size="16" class="text-blue-500" />
                <Library v-else :size="16" class="text-gray-500" />
              </div>
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2">
                  <span class="text-sm font-medium truncate">{{ c.name }}</span>
                  <span
                    v-if="c.is_subscription"
                    class="text-[10px] px-1.5 py-0.5 rounded flex-shrink-0"
                    :class="c.sync_enabled ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-400'"
                  >{{ c.sync_enabled ? '订阅' : '已暂停' }}</span>
                </div>
                <div class="text-xs text-gray-400">{{ c.job_count }} 篇</div>
              </div>
              <ChevronRight :size="16" class="text-gray-300 flex-shrink-0" />
            </button>
          </div>
        </div>

        <!-- 主题 -->
        <div class="bg-white border border-gray-200 rounded-xl p-4">
          <h3 class="text-sm font-semibold text-gray-700 mb-3">主题</h3>
          <div v-if="data.topics.length === 0">
            <EmptyState message="暂无可浏览主题" />
          </div>
          <div v-else class="flex flex-wrap gap-2">
            <button
              v-for="t in data.topics"
              :key="t.topic"
              @click="goTopic(t.topic)"
              class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-gray-100 text-gray-700 text-xs hover:bg-gray-200 transition-colors"
            >
              <span>#{{ t.topic }}</span>
              <span class="text-gray-400">{{ t.count }}</span>
            </button>
          </div>
        </div>

        <!-- 最近内容 -->
        <div>
          <h3 class="text-sm font-semibold text-gray-700 mb-2">最近内容</h3>
          <div v-if="data.recent_jobs.length === 0">
            <EmptyState message="暂无内容" />
          </div>
          <div v-else class="space-y-2">
            <JobCard v-for="j in data.recent_jobs" :key="j.job_id" :job="j" />
          </div>
        </div>
      </div>

      <!-- 右：语义层 -->
      <div class="space-y-4">
        <div class="flex items-center gap-1.5 text-sm font-semibold text-gray-700">
          <BrainCircuit :size="15" class="text-gray-400" />
          语义层 · 学会了什么
        </div>

        <!-- 概念 -->
        <div class="bg-white border border-gray-200 rounded-xl p-4">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-semibold text-gray-700">概念</h3>
            <span class="text-xs text-gray-400">按佐证 ★</span>
          </div>
          <div v-if="sortedConcepts.length === 0">
            <EmptyState message="暂无概念" />
          </div>
          <div v-else class="space-y-1">
            <button
              v-for="c in sortedConcepts"
              :key="c.term"
              @click="goTerm(c.term)"
              class="w-full flex items-center gap-3 text-left p-2 rounded-lg hover:bg-gray-50 transition-colors"
            >
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium truncate">{{ c.term }}</div>
                <div v-if="c.definition" class="text-xs text-gray-400 truncate">{{ c.definition }}</div>
              </div>
              <div class="flex items-center gap-0.5 flex-shrink-0" :title="`佐证强度 ${strength(c.source_count)}/5`">
                <Star
                  v-for="i in 5"
                  :key="i"
                  :size="12"
                  :class="i <= strength(c.source_count) ? 'text-amber-400 fill-amber-400' : 'text-gray-200'"
                />
              </div>
              <span class="text-xs text-gray-400 flex-shrink-0 w-8 text-right">{{ c.source_count }} 源</span>
            </button>
          </div>
        </div>

        <!-- 待确认概念提示 -->
        <div
          v-if="data.suggested_count > 0"
          class="bg-amber-50 border border-amber-200 rounded-xl p-4 flex items-center gap-2"
        >
          <AlertTriangle :size="16" class="text-amber-500 flex-shrink-0" />
          <span class="text-sm text-amber-800">
            有 {{ data.suggested_count }} 个待确认概念
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
