<script setup lang="ts">
import { onMounted, ref, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import JobCard from '../components/job/JobCard.vue'
import EmptyState from '../components/common/EmptyState.vue'
import type { JobSummary } from '../types'
import { ArrowLeft, Hash, RefreshCw } from 'lucide-vue-next'

// 主题页：领域锚下，把某个主题(is_topic 概念)下跨集合/跨来源的内容聚到一处浏览。
// 主题⊆概念：本页不是独立实体，路由挂在 /domains/:domain 下，返回回领域工作台。
const route = useRoute()
const router = useRouter()
const store = useDomainStore()

const domain = computed(() => String(route.params.domain))
const topic = computed(() => String(route.params.topic))

const jobs = ref<JobSummary[]>([])
const total = ref(0)
const loading = ref(false)
const errored = ref(false)
const notFound = ref(false)

// 后端 job_brief 与 JobSummary 字段一致(job_id/content_type/status/created_at/title/
// progress_pct/source/domain)，做一次防御性归一，缺字段时给安全默认，保证 JobCard 可渲染。
function normalizeJob(raw: any): JobSummary {
  return {
    job_id: String(raw?.job_id ?? ''),
    content_type: raw?.content_type ?? 'article',
    status: String(raw?.status ?? 'unknown'),
    created_at: String(raw?.created_at ?? ''),
    title: raw?.title ?? null,
    progress_pct: Number(raw?.progress_pct ?? 0),
    source: raw?.source ?? null,
    domain: String(raw?.domain ?? domain.value),
  }
}

async function load() {
  loading.value = true
  errored.value = false
  notFound.value = false
  try {
    const res = await store.topic(domain.value, topic.value)
    const list: any[] = Array.isArray(res?.jobs) ? res.jobs : []
    jobs.value = list.map(normalizeJob)
    total.value = typeof res?.total === 'number' ? res.total : jobs.value.length
  } catch (e: any) {
    if (String(e?.message ?? '').includes('404')) {
      notFound.value = true
    } else {
      errored.value = true
    }
  } finally {
    loading.value = false
  }
}

function back() {
  router.push(`/domains/${encodeURIComponent(domain.value)}`)
}

onMounted(load)
// 主题/领域参数变化时(如直接改 URL 或站内跳转复用同一组件)重新加载。
watch(() => [route.params.domain, route.params.topic], load)
</script>

<template>
  <div class="space-y-4">
    <!-- 头部：返回 + 面包屑(领域名) + 主题名 -->
    <div class="flex items-center gap-2">
      <button
        @click="back"
        class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
        title="返回领域工作台"
      >
        <ArrowLeft :size="18" />
      </button>
      <h2 class="text-xl font-bold flex items-center gap-2 min-w-0">
        <Hash :size="20" class="text-blue-500 flex-shrink-0" />
        <span class="truncate">{{ topic }}</span>
      </h2>
      <button
        @click="load()"
        class="ml-auto p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
        title="刷新"
      >
        <RefreshCw :size="16" :class="loading ? 'animate-spin' : ''" />
      </button>
    </div>

    <!-- 主题元信息：所属 domain(可点回工作台) + 内容数 -->
    <div class="bg-white border border-gray-200 rounded-xl p-4">
      <div class="flex items-center gap-2 text-xs text-gray-500 flex-wrap">
        <span>主题</span>
        <span class="text-gray-300">·</span>
        <button @click="back" class="text-blue-600 hover:underline">{{ domain }}</button>
        <span class="text-gray-300">·</span>
        <span>{{ total }} 篇内容</span>
      </div>
    </div>

    <!-- 错误态 -->
    <div v-if="errored" class="bg-white border border-gray-200 rounded-xl p-6 text-center">
      <p class="text-sm text-gray-500 mb-3">加载主题内容失败</p>
      <button
        @click="load()"
        class="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
      >
        重试
      </button>
    </div>

    <!-- 主题不存在 -->
    <EmptyState v-else-if="notFound" message="主题不存在或已删除" />

    <!-- 加载态 -->
    <div v-else-if="loading && jobs.length === 0" class="text-sm text-gray-400 py-8 text-center">
      加载中...
    </div>

    <!-- 空态：主题靠抽取自动聚合，不在此直接投递 -->
    <EmptyState v-else-if="jobs.length === 0" message="这个主题还没有内容(内容被解析、抽到该概念时自动聚合)" />

    <!-- 该主题下的内容列表 -->
    <div v-else class="space-y-3">
      <JobCard v-for="j in jobs" :key="j.job_id" :job="j" />
    </div>
  </div>
</template>
