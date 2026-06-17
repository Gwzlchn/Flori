<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useDomainStore } from '../stores/domains'
import { useApi } from '../composables/useApi'
import JobSubmitForm from '../components/job/JobSubmitForm.vue'
import JobCard from '../components/job/JobCard.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { fmtDateTime } from '../utils/datetime'
import type { DomainOverview, JobSummary, JobListResponse } from '../types'
import {
  Layers,
  ListTodo,
  ChevronRight,
  ChevronDown,
  Library,
  FileText,
  Lightbulb,
  Rss,
  RefreshCw,
  AlertCircle,
} from 'lucide-vue-next'

const router = useRouter()
const api = useApi()
const domainStore = useDomainStore()
const { domains, loading } = storeToRefs(domainStore)

// 领域网格加载错误态（store.fetchAll 不吞错，这里捕获展示）
const error = ref('')

// 「快速投递」收起式：默认展开，投递是高频入口（容器内含 JobSubmitForm，其根带 data-submit-form，
// 底部导航「投递」按钮 scrollIntoView 靠它定位 —— 故始终渲染，仅折叠时高度收起）。
const submitOpen = ref(true)

// 近期内容（跨域）：可选区块，纯展示，失败不阻塞领域网格。
const recentJobs = ref<JobSummary[]>([])
const recentLoading = ref(true)

// 领域是否有内容（无领域时走大空态引导）
const hasDomains = computed(() => domains.value.length > 0)

// 活跃时间相对展示（last_active_at 可能为 null）
function activeAgo(v: string | null): string {
  if (!v) return '从未活跃'
  const diff = Date.now() - new Date(v).getTime()
  if (isNaN(diff)) return '从未活跃'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚活跃'
  if (mins < 60) return `${mins} 分钟前活跃`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前活跃`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前活跃`
  return `活跃于 ${fmtDateTime(v)}`
}

async function loadDomains() {
  error.value = ''
  try {
    await domainStore.fetchAll()
  } catch (e: any) {
    error.value = e?.message || '加载领域失败'
  }
}

async function loadRecent() {
  recentLoading.value = true
  try {
    const res = await api.get<JobListResponse>('/api/jobs?limit=8')
    recentJobs.value = res.items || []
  } catch {
    // 近期内容为辅助区块，失败时静默降级为空。
    recentJobs.value = []
  } finally {
    recentLoading.value = false
  }
}

function openDomain(d: DomainOverview) {
  router.push(`/domains/${encodeURIComponent(d.domain)}`)
}

// 空态「投递一条」：展开快速投递并滚动到表单（与底部导航「投递」一致用 data-submit-form 定位）。
function scrollToSubmit() {
  submitOpen.value = true
  requestAnimationFrame(() => {
    document.querySelector('[data-submit-form]')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

onMounted(() => {
  loadDomains()
  loadRecent()
})
</script>

<template>
  <div class="space-y-6">
    <!-- 头部：标题 + 全部内容入口 -->
    <div class="flex items-center gap-2">
      <h2 class="text-xl font-bold flex items-center gap-2">
        <Layers :size="22" class="text-gray-700" />
        我的知识领域
      </h2>
      <button
        @click="router.push('/jobs')"
        class="ml-auto flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800 transition-colors"
      >
        <ListTodo :size="15" />
        全部内容
        <ChevronRight :size="15" />
      </button>
    </div>

    <!-- 快速投递（收起式）：容器内嵌 JobSubmitForm（其根带 data-submit-form） -->
    <div class="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <button
        @click="submitOpen = !submitOpen"
        class="w-full flex items-center gap-2 px-4 py-3 text-sm font-semibold text-gray-700 hover:bg-gray-50 transition-colors"
      >
        <span>快速投递</span>
        <span class="text-xs font-normal text-gray-400">粘贴 URL / 上传文件，自动归入领域</span>
        <component :is="submitOpen ? ChevronDown : ChevronRight" :size="16" class="ml-auto text-gray-400" />
      </button>
      <div v-show="submitOpen" class="border-t border-gray-100">
        <!-- JobSubmitForm 根元素自带 data-submit-form，底部导航「投递」按钮以此定位 -->
        <JobSubmitForm />
      </div>
    </div>

    <!-- 领域网格 -->
    <section>
      <!-- 加载骨架 -->
      <div v-if="loading && domains.length === 0" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div
          v-for="i in 3"
          :key="i"
          class="bg-white border border-gray-200 rounded-xl p-4 animate-pulse space-y-3"
        >
          <div class="h-4 w-24 bg-gray-100 rounded" />
          <div class="h-3 w-32 bg-gray-100 rounded" />
          <div class="h-3 w-20 bg-gray-100 rounded" />
        </div>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="error"
        class="bg-white border border-gray-200 rounded-xl p-6 flex flex-col items-center text-center gap-3"
      >
        <AlertCircle :size="32" class="text-red-400" />
        <p class="text-sm text-gray-600">{{ error }}</p>
        <button
          @click="loadDomains"
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-gray-300 text-gray-600 hover:bg-gray-50 transition-colors"
        >
          <RefreshCw :size="14" />
          重试
        </button>
      </div>

      <!-- 领域为空：大空态引导 -->
      <div
        v-else-if="!hasDomains"
        class="bg-white border border-gray-200 rounded-xl py-10 px-6 flex flex-col items-center text-center"
      >
        <EmptyState message="还没有知识领域" />
        <p class="-mt-6 text-xs text-gray-400 max-w-sm">
          从一条视频 / 论文 / 文章开始，系统会自动把内容归入对应领域，逐步沉淀成你的知识体系。
        </p>
        <button
          @click="scrollToSubmit"
          class="mt-4 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          快速投递一条内容
        </button>
      </div>

      <!-- 领域卡片网格 -->
      <div v-else class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <button
          v-for="d in domains"
          :key="d.domain"
          @click="openDomain(d)"
          class="text-left bg-white border border-gray-200 rounded-xl p-4 hover:shadow-sm hover:-translate-y-0.5 transition-all"
        >
          <div class="flex items-center gap-2 mb-3">
            <div class="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center flex-shrink-0">
              <Layers :size="16" class="text-gray-500" />
            </div>
            <h3 class="text-sm font-semibold truncate min-w-0">{{ d.domain }}</h3>
            <span
              v-if="d.subscription_count > 0"
              class="ml-auto flex items-center gap-0.5 text-[11px] text-blue-600 flex-shrink-0"
              :title="`${d.subscription_count} 个订阅集合在自动追更`"
            >
              <Rss :size="12" />{{ d.subscription_count }}
            </span>
          </div>

          <div class="flex items-center gap-3 text-xs text-gray-500 mb-2 flex-wrap">
            <span class="flex items-center gap-1">
              <Library :size="13" class="text-gray-400" />
              {{ d.collection_count }} 集合
            </span>
            <span class="flex items-center gap-1">
              <FileText :size="13" class="text-gray-400" />
              {{ d.job_count }} 篇
            </span>
            <span class="flex items-center gap-1">
              <Lightbulb :size="13" class="text-gray-400" />
              {{ d.concept_count }} 概念
            </span>
          </div>

          <div class="flex items-center gap-1.5 text-[11px] text-gray-400">
            <span
              class="w-1.5 h-1.5 rounded-full flex-shrink-0"
              :class="d.last_active_at ? 'bg-green-500' : 'bg-gray-300'"
            />
            {{ activeAgo(d.last_active_at) }}
          </div>
        </button>
      </div>
    </section>

    <!-- 近期内容（跨域，可选辅助区块） -->
    <section v-if="hasDomains">
      <div class="flex items-center gap-2 mb-2">
        <h3 class="text-sm font-semibold text-gray-700">近期内容</h3>
        <span class="text-xs text-gray-400">跨领域</span>
        <button
          @click="router.push('/jobs')"
          class="ml-auto flex items-center gap-1 text-xs text-gray-400 hover:text-gray-700 transition-colors"
        >
          全部内容
          <ChevronRight :size="13" />
        </button>
      </div>

      <div v-if="recentLoading && recentJobs.length === 0" class="text-sm text-gray-400 py-4 text-center">
        加载中...
      </div>
      <div v-else-if="recentJobs.length === 0">
        <EmptyState message="还没有内容，去上方投递一条" />
      </div>
      <div v-else class="space-y-2">
        <JobCard v-for="job in recentJobs" :key="job.job_id" :job="job" />
      </div>
    </section>
  </div>
</template>
