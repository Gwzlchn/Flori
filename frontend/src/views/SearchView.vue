<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { useRouter } from 'vue-router'
import { Search } from 'lucide-vue-next'
import { useApi } from '../composables/useApi'
import type { SearchResponse, SearchResultItem } from '../types'

const api = useApi()
const router = useRouter()

const q = ref('')
const domain = ref('')
const contentType = ref('')
const loading = ref(false)
const searched = ref(false)
const result = ref<SearchResponse>({ total: 0, items: [] })

// trigram tokenizer 至少 3 字符才命中，短于此直接提示而不打 API。
const tooShort = computed(() => q.value.trim().length > 0 && q.value.trim().length < 3)

// 笔记类型徽章：与后端 note_type 取值对齐。
const typeLabels: Record<string, string> = {
  smart: '智能笔记',
  mechanical: '机械笔记',
  paper: '论文笔记',
}

const contentTypeLabels: Record<string, string> = {
  video: '视频',
  paper: '论文',
  article: '文章',
  audio: '播客',
}

// snippet 安全渲染：后端 fts5 高亮用 <mark>，正文可能混入原始 HTML。
// 先转义全部 HTML，再仅还原 <mark> 高亮标记，杜绝任何可执行标签。
function safeSnippet(raw: string): string {
  if (!raw) return ''
  const escaped = raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
  return escaped
    .replace(/&lt;mark&gt;/g, '<mark>')
    .replace(/&lt;\/mark&gt;/g, '</mark>')
}

function buildQuery(): string {
  const p = new URLSearchParams()
  p.set('q', q.value.trim())
  if (domain.value) p.set('domain', domain.value)
  if (contentType.value) p.set('content_type', contentType.value)
  return p.toString()
}

async function runSearch() {
  const term = q.value.trim()
  if (term.length < 3) {
    result.value = { total: 0, items: [] }
    searched.value = term.length > 0
    return
  }
  loading.value = true
  searched.value = true
  try {
    result.value = await api.get<SearchResponse>(`/api/search?${buildQuery()}`)
  } catch {
    result.value = { total: 0, items: [] }
  } finally {
    loading.value = false
  }
}

// 输入防抖：停止键入 300ms 后再查。
let timer: ReturnType<typeof setTimeout> | undefined
watch([q, domain, contentType], () => {
  if (timer) clearTimeout(timer)
  timer = setTimeout(runSearch, 300)
})

function openNote(item: SearchResultItem) {
  // 机械笔记跳机械页，其余跳智能笔记页。
  const path = item.note_type === 'mechanical'
    ? `/notes/${item.job_id}/mechanical`
    : `/notes/${item.job_id}`
  router.push(path)
}
</script>

<template>
  <div class="space-y-4">
    <h2 class="text-xl font-bold">搜索</h2>

    <!-- 搜索框 -->
    <div class="relative">
      <Search :size="18" class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
      <input
        v-model="q"
        type="text"
        placeholder="搜索笔记内容（至少 3 个字符）"
        class="w-full pl-10 pr-4 py-2.5 text-sm border border-gray-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-blue-200 focus:border-blue-300"
      />
    </div>

    <!-- facet 过滤 -->
    <div class="flex flex-wrap gap-2">
      <select
        v-model="contentType"
        class="px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-200"
      >
        <option value="">全部类型</option>
        <option value="video">视频</option>
        <option value="paper">论文</option>
        <option value="article">文章</option>
      </select>
      <input
        v-model="domain"
        type="text"
        placeholder="领域过滤（可选）"
        class="px-3 py-1.5 text-sm border border-gray-200 rounded-lg bg-white text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-200"
      />
    </div>

    <!-- 状态/结果 -->
    <p v-if="tooShort" class="text-sm text-gray-400 py-2">请至少输入 3 个字符。</p>
    <div v-else-if="loading" class="text-sm text-gray-400 py-8 text-center">搜索中...</div>
    <div v-else-if="searched && result.items.length === 0" class="text-sm text-gray-400 py-12 text-center">
      未找到匹配的笔记。
    </div>
    <div v-else-if="!searched" class="text-sm text-gray-400 py-12 text-center">
      输入关键词开始搜索。
    </div>

    <div v-else class="space-y-2">
      <p class="text-xs text-gray-400">共 {{ result.total }} 条结果</p>
      <div
        v-for="item in result.items"
        :key="`${item.job_id}-${item.note_type}`"
        @click="openNote(item)"
        class="bg-white border border-gray-200 rounded-xl p-4 cursor-pointer hover:shadow-sm transition-shadow"
      >
        <div class="flex items-center gap-2 mb-1">
          <h4 class="text-sm font-medium truncate flex-1">{{ item.title || item.job_id }}</h4>
          <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
            {{ typeLabels[item.note_type] || item.note_type }}
          </span>
        </div>
        <!-- snippet 经 safeSnippet 转义，仅保留 <mark> 高亮，杜绝注入。 -->
        <p class="text-sm text-gray-600 search-snippet" v-html="safeSnippet(item.snippet)"></p>
        <div class="flex items-center gap-2 mt-2 text-xs text-gray-400">
          <span v-if="item.content_type">{{ contentTypeLabels[item.content_type] || item.content_type }}</span>
          <span v-if="item.domain && item.domain !== 'general'">{{ item.domain }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-snippet :deep(mark) {
  background-color: #fef08a;
  color: inherit;
  border-radius: 2px;
  padding: 0 1px;
}
</style>
