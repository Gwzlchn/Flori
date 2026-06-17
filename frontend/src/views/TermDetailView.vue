<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import EmptyState from '../components/common/EmptyState.vue'
import type { TermOccurrence } from '../types'
import { ArrowLeft, ChevronRight, Network, Link2, FileBox } from 'lucide-vue-next'

// 术语详情 /domains/:domain/terms/:term —— 概念节点的知识页：
// 头(概念名+domain+状态) / 定义(可空占位) / 关联概念 related(chips) / 出现处 occurrences(类型化)。
// 后端形状: {domain, term, definition, occurrences:[{job_id,content_type,location}], related:[term...], status}
const route = useRoute()
const router = useRouter()
const store = useDomainStore()

const domain = computed(() => String(route.params.domain))
const term = computed(() => String(route.params.term))

const data = ref<any>(null)
const loading = ref(false)
const notFound = ref(false)
const errored = ref(false)

// 状态徽标中文 —— 与 GlossaryView 语义对齐（accepted/suggested）。
const STATUS_LABELS: Record<string, string> = {
  accepted: '已采纳',
  suggested: '待确认',
}
const statusLabel = computed(() => {
  const s = data.value?.status ?? ''
  return STATUS_LABELS[s] ?? s
})

const related = computed<string[]>(() => (Array.isArray(data.value?.related) ? data.value.related : []))
const occurrences = computed<TermOccurrence[]>(() => (Array.isArray(data.value?.occurrences) ? data.value.occurrences : []))

async function load() {
  loading.value = true
  notFound.value = false
  errored.value = false
  data.value = null
  try {
    data.value = await store.term(domain.value, term.value)
  } catch (e: any) {
    const msg = String(e?.message ?? '')
    if (msg.includes('404')) notFound.value = true
    else errored.value = true
  } finally {
    loading.value = false
  }
}

function goBack() {
  router.push(`/domains/${encodeURIComponent(domain.value)}`)
}
function goRelated(name: string) {
  router.push(`/domains/${encodeURIComponent(domain.value)}/terms/${encodeURIComponent(name)}`)
}
function goJob(jobId: string) {
  router.push(`/jobs/${encodeURIComponent(jobId)}`)
}

onMounted(load)
// 同页内（关联概念 chip）切换 term 时重新加载。
watch(() => [route.params.domain, route.params.term], load)
</script>

<template>
  <div class="space-y-4">
    <!-- 头部：返回 + 面包屑（领域 ▸ 术语） -->
    <div class="flex items-center gap-2">
      <button
        @click="goBack"
        class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
        title="返回领域工作台"
      >
        <ArrowLeft :size="18" />
      </button>
      <div class="flex items-center gap-1 text-sm text-gray-500 min-w-0">
        <button @click="goBack" class="hover:text-gray-700 truncate">{{ domain }}</button>
        <ChevronRight :size="14" class="flex-shrink-0" />
        <span class="text-gray-400">术语</span>
      </div>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="text-sm text-gray-400 py-12 text-center">加载中...</div>

    <!-- 404：概念不存在 / 已删除 -->
    <div v-else-if="notFound" class="space-y-4">
      <EmptyState message="概念不存在或已删除" />
      <div class="text-center">
        <button @click="goBack" class="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          返回领域工作台
        </button>
      </div>
    </div>

    <!-- 其它错误：可重试 -->
    <div v-else-if="errored" class="space-y-4">
      <EmptyState message="加载失败" />
      <div class="text-center">
        <button @click="load" class="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          重试
        </button>
      </div>
    </div>

    <template v-else-if="data">
      <!-- 概念名 + domain + 状态 -->
      <div class="bg-white border border-gray-200 rounded-xl p-5">
        <div class="flex items-start gap-3 flex-wrap">
          <h1 class="text-2xl font-bold text-gray-800 break-all min-w-0">{{ data.term }}</h1>
          <span
            v-if="data.status"
            class="mt-1.5 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
            :class="data.status === 'suggested' ? 'bg-amber-100 text-amber-700' : 'bg-green-100 text-green-700'"
          >{{ statusLabel }}</span>
        </div>
        <div class="mt-2 flex items-center gap-3 text-xs text-gray-500 flex-wrap">
          <span>
            domain：<button @click="goBack" class="text-blue-600 hover:underline">{{ data.domain }}</button>
          </span>
          <span>{{ occurrences.length }} 处出现</span>
          <span>{{ related.length }} 个关联</span>
        </div>
      </div>

      <!-- 定义（可空显示占位） -->
      <section class="bg-white border border-gray-200 rounded-xl p-4 space-y-2">
        <h2 class="text-sm font-semibold text-gray-700">定义</h2>
        <p v-if="data.definition" class="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap break-words">
          {{ data.definition }}
        </p>
        <p v-else class="text-sm text-gray-400">暂无定义</p>
      </section>

      <!-- 关联概念 related（仅同域，点进同域术语详情） -->
      <section class="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
        <h2 class="text-sm font-semibold text-gray-700 flex items-center gap-1.5">
          <Network :size="15" class="text-gray-400" />
          关联概念
          <span class="text-xs text-gray-400 font-normal">（仅同域）</span>
        </h2>
        <div v-if="related.length > 0" class="flex flex-wrap gap-2">
          <button
            v-for="r in related"
            :key="r"
            @click="goRelated(r)"
            class="inline-flex items-center gap-1 px-2.5 py-1 bg-gray-100 text-gray-700 text-sm rounded-full hover:bg-blue-50 hover:text-blue-700 transition-colors"
          >
            <Link2 :size="13" />
            <span class="break-all">{{ r }}</span>
          </button>
        </div>
        <p v-else class="text-sm text-gray-400">暂无关联概念</p>
      </section>

      <!-- 出现处 occurrences（类型化：内容 + 类型 + 位置，链接到 /jobs/:job_id） -->
      <section class="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
        <h2 class="text-sm font-semibold text-gray-700 flex items-center gap-1.5">
          <FileBox :size="15" class="text-gray-400" />
          出现处
          <span class="text-xs text-gray-400 font-normal">（{{ occurrences.length }} 处）</span>
        </h2>
        <div v-if="occurrences.length > 0" class="space-y-2">
          <button
            v-for="o in occurrences"
            :key="o.job_id + (o.location || '')"
            @click="goJob(o.job_id)"
            class="w-full flex items-center gap-2 text-left px-3 py-2.5 border border-gray-200 rounded-lg hover:border-blue-300 hover:bg-blue-50/40 transition-colors"
          >
            <FileBox :size="15" class="text-gray-400 flex-shrink-0" />
            <span class="text-sm text-gray-700 font-mono break-all min-w-0 flex-1">{{ o.job_id }}</span>
            <span v-if="o.content_type" class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 flex-shrink-0">{{ o.content_type }}</span>
            <span v-if="o.location" class="text-xs text-gray-400 flex-shrink-0">{{ o.location }}</span>
            <ChevronRight :size="15" class="text-gray-300 flex-shrink-0" />
          </button>
        </div>
        <EmptyState v-else message="还没有内容提到这个概念" />
      </section>
    </template>
  </div>
</template>
