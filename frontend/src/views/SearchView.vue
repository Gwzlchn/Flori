<script setup lang="ts">
// 搜索（原型 #search）：q≥3 字符才查；可按 内容类型 / 知识库(domain) / 集合(collection_id) 收窄。
// 结果跳 /content/:id。snippet 含服务端 <mark> 高亮——但 sqlite snippet() 不转义正文，
// 故这里仍做防御式转义（先整段转义、再仅还原 <mark>），杜绝任何可执行标签注入。
import { ref, watch, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { CONTENT_TYPE_LABELS } from '../types'
import type { SearchResponse, SearchResultItem } from '../types'
import { Search, Play, FileText, Newspaper, Headphones } from 'lucide-vue-next'

const api = useApi()
const router = useRouter()

const q = ref('')
const domain = ref('')
const contentType = ref('')
const collectionId = ref('')
const loading = ref(false)
const error = ref('')
const searched = ref(false)
const result = ref<SearchResponse>({ total: 0, items: [] })

// trigram 至少 3 字符才命中，短于此直接提示而不打 API。
const term = computed(() => q.value.trim())
const tooShort = computed(() => term.value.length > 0 && term.value.length < 3)

// 笔记类型徽章：与后端 note_type 取值对齐（smart|mechanical|transcript）。
const NOTE_TYPE_LABELS: Record<string, string> = {
  smart: '智能笔记',
  mechanical: '机械稿',
  transcript: '逐字稿',
}

// 内容类型 → type-pill 配色类 + 图标。
const PILL_CLASS: Record<string, string> = {
  video: 't-video', paper: 't-paper', article: 't-article', audio: 't-audio',
}
const PILL_ICON: Record<string, any> = {
  video: Play, paper: FileText, article: Newspaper, audio: Headphones,
}

// snippet 安全渲染：整段先转义，再仅还原 <mark> 高亮，确保 v-html 不注入。
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
  p.set('q', term.value)
  if (domain.value.trim()) p.set('domain', domain.value.trim())
  if (contentType.value) p.set('content_type', contentType.value)
  if (collectionId.value.trim()) p.set('collection_id', collectionId.value.trim())
  return p.toString()
}

async function runSearch() {
  if (term.value.length < 3) {
    result.value = { total: 0, items: [] }
    searched.value = term.value.length > 0
    error.value = ''
    return
  }
  loading.value = true
  searched.value = true
  error.value = ''
  try {
    result.value = await api.get<SearchResponse>(`/api/search?${buildQuery()}`)
  } catch (e: any) {
    result.value = { total: 0, items: [] }
    error.value = e?.message || '搜索失败'
  } finally {
    loading.value = false
  }
}

// 输入防抖：停止键入 300ms 后再查。
let timer: ReturnType<typeof setTimeout> | undefined
watch([q, domain, contentType, collectionId], () => {
  if (timer) clearTimeout(timer)
  timer = setTimeout(runSearch, 300)
})

function open(item: SearchResultItem) {
  router.push(`/content/${encodeURIComponent(item.job_id)}`)
}
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom:16px"><Search :size="18" />搜索</div>

    <!-- 搜索框 -->
    <div class="search" style="width:100%;padding:11px 14px;cursor:text">
      <Search :size="17" />
      <input v-model="q" placeholder="搜索笔记内容（至少 3 个字符）" style="font-size:14px" />
      <span class="kbd">⌘K</span>
    </div>

    <!-- 过滤 -->
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">
      <select v-model="contentType" class="input" style="max-width:140px">
        <option value="">全部类型</option>
        <option value="video">视频</option>
        <option value="paper">论文</option>
        <option value="article">文章</option>
        <option value="audio">播客</option>
      </select>
      <input v-model="domain" class="input" placeholder="知识库过滤" style="max-width:160px" />
      <input v-model="collectionId" class="input" placeholder="集合 ID（可选）" style="max-width:160px" />
    </div>

    <!-- 提示态：字数不足 -->
    <div v-if="tooShort" class="note-tip" style="margin-top:18px">输入需 ≥ 3 字才会搜索。</div>

    <!-- 加载态 -->
    <div v-else-if="loading" class="card pad" style="margin-top:18px;color:var(--ink-500);font-size:13px">
      搜索中…
    </div>

    <!-- 错误态 -->
    <div v-else-if="error" class="card pad"
      style="margin-top:18px;display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:32px 18px">
      <div style="font-size:13.5px;color:var(--ink-700)">{{ error }}</div>
      <button class="btn" @click="runSearch">重试</button>
    </div>

    <!-- 空态：已搜但无结果 -->
    <div v-else-if="searched && result.items.length === 0" class="card pad"
      style="margin-top:18px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:40px 18px">
      <Search :size="40" :stroke-width="1" style="color:var(--ink-300)" />
      <div style="font-size:14px;color:var(--ink-700);font-weight:600">没有匹配的笔记</div>
      <div class="lead" style="max-width:360px">换个关键词，或放宽上面的类型 / 知识库 / 集合过滤。</div>
    </div>

    <!-- 初始态：还没搜 -->
    <div v-else-if="!searched" class="note-tip" style="margin-top:18px">输入关键词开始搜索，无匹配会显示空状态。</div>

    <!-- 结果列表 -->
    <template v-else>
      <div class="muted" style="font-size:12.5px;margin:18px 0 12px">共 {{ result.total }} 条结果</div>
      <div class="list">
        <div
          v-for="item in result.items"
          :key="`${item.job_id}-${item.note_type}`"
          class="card pad"
          style="cursor:pointer;display:flex;align-items:flex-start;gap:13px"
          @click="open(item)"
        >
          <span class="type-pill" :class="PILL_CLASS[item.content_type] || 't-article'" style="margin-top:1px">
            <component :is="PILL_ICON[item.content_type] || Newspaper" :size="17" />
          </span>
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:8px">
              <div class="title" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {{ item.title || item.job_id }}
              </div>
              <span class="badge b-mut">{{ NOTE_TYPE_LABELS[item.note_type] || item.note_type }}</span>
            </div>
            <!-- snippet 经 safeSnippet 转义，仅保留 <mark> 高亮，杜绝注入。 -->
            <p class="search-snippet" style="font-size:13px;color:var(--ink-600);margin:6px 0 0" v-html="safeSnippet(item.snippet)"></p>
            <div class="meta" style="margin-top:7px">
              <span>{{ CONTENT_TYPE_LABELS[item.content_type] || item.content_type }}</span>
              <template v-if="item.domain && item.domain !== 'general'">
                <span class="sep">·</span><span>{{ item.domain }}</span>
              </template>
            </div>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.search-snippet :deep(mark) {
  background: var(--brand-50);
  color: var(--brand-700);
  border-radius: 2px;
  padding: 0 1px;
}
</style>
