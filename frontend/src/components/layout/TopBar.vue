<script setup lang="ts">
import { ref, computed, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Search, Menu } from 'lucide-vue-next'
import { useApi } from '../../composables/useApi'
import { useGlobalStore } from '../../stores/global'
import { contentTypeIcon, contentTypePill, noteTypeLabel } from '../../utils/contentType'
import type { BreadcrumbSeg, SearchResponse, SearchResultItem } from '../../types'

// 移动端汉堡:开合左侧抽屉。桌面端隐藏(CSS 媒体查询控制)。
defineEmits<{ (e: 'toggle-mobile'): void }>()

const route = useRoute()
const router = useRouter()
const api = useApi()
const global = useGlobalStore()
const q = ref('')

// 面包屑:详情页加载到真实数据后可经 global.crumbOverride 覆盖(如内容标题/领域);
// 否则按路由名派生通用文案。段类型 BreadcrumbSeg 与 store.crumbOverride 共用(types/index.ts)。
const crumbs = computed<BreadcrumbSeg[]>(() => {
  if (global.crumbOverride?.length) return global.crumbOverride
  const p = route.params as any
  const root: BreadcrumbSeg = { t: '知识库', to: '/' }
  switch (route.name) {
    case 'knowledge-bases': return [{ t: '知识库' }]
    case 'knowledge-base': return [root, { t: String(p.domain) }]
    case 'concept-detail': return [root, { t: String(p.domain), to: `/kb/${p.domain}` }, { t: String(p.term) }]
    case 'topic': return [root, { t: String(p.domain), to: `/kb/${p.domain}` }, { t: '主题 · ' + String(p.topic) }]
    case 'content': return [root, { t: '所有来源' }]
    case 'content-detail': return [root, { t: '所有来源', to: '/content' }, { t: '内容详情' }]
    case 'collections': return [root, { t: '集合' }]
    case 'collection-detail': return [{ t: '集合', to: '/collections' }, { t: '集合详情' }]
    case 'glossary': return [root, { t: '概念库' }]
    case 'study': return [root, { t: '学习' }]
    case 'search': return [{ t: '搜索' }]
    case 'system':
      return route.query.from === 'settings'
        ? [{ t: '设置', to: '/settings' }, { t: '系统' }]
        : [{ t: '系统' }]
    case 'worker-detail': return [{ t: '系统', to: '/system' }, { t: 'Worker 详情' }]
    case 'settings': return [{ t: '设置' }]
    case 'settings-prompts': return [{ t: '设置', to: '/settings' }, { t: 'AI 工作流' }]
    case 'about': return [{ t: '设置', to: '/settings' }, { t: '关于' }]
    default: return [{ t: '知识库', to: '/' }]
  }
})

const canBack = computed(() => crumbs.value.length > 1)
function goBack() {
  const cs = crumbs.value
  for (let i = cs.length - 2; i >= 0; i--) if (cs[i].to) { router.push(cs[i].to!); return }
  router.push('/')
}

// 顶栏搜索:就地展开 + 下拉建议(不直接跳转)
const expanded = ref(false)
const suggestions = ref<SearchResultItem[]>([])
const loading = ref(false)
const wrapEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLInputElement | null>(null)

// 笔记类型徽章与内容类型图标/配色统一走 utils/contentType,与 SearchView 共用单一来源。

const term = computed(() => q.value.trim())

function expand() {
  expanded.value = true
}

function collapse() {
  expanded.value = false
}

// 输入防抖:停止键入 ~250ms 后再查;q≥3 字符才打 API。
let timer: ReturnType<typeof setTimeout> | undefined
function onInput() {
  if (timer) clearTimeout(timer)
  if (term.value.length < 3) {
    suggestions.value = []
    loading.value = false
    return
  }
  loading.value = true
  timer = setTimeout(fetchSuggestions, 250)
}

async function fetchSuggestions() {
  const v = term.value
  if (v.length < 3) { suggestions.value = []; loading.value = false; return }
  try {
    const r = await api.get<SearchResponse>(`/api/search?q=${encodeURIComponent(v)}&limit=5`)
    // 仅在查询词未变时回填,避免乱序竞态。
    if (v === term.value) suggestions.value = r.items.slice(0, 5)
  } catch {
    if (v === term.value) suggestions.value = []
  } finally {
    if (v === term.value) loading.value = false
  }
}

// 点建议 → 内容详情。
function openItem(item: SearchResultItem) {
  collapse()
  router.push(`/content/${encodeURIComponent(item.job_id)}`)
}

// 回车或点底部 "查看全部" → 搜索页。
function runSearch() {
  const v = term.value
  if (!v) return
  collapse()
  router.push(`/search?q=${encodeURIComponent(v)}`)
}

function onEsc() {
  collapse()
  inputEl.value?.blur()
}

// 点外部收起。
function onDocClick(e: MouseEvent) {
  if (!expanded.value) return
  if (wrapEl.value && !wrapEl.value.contains(e.target as Node)) collapse()
}
document.addEventListener('click', onDocClick)
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <div class="topbar">
    <button class="hamburger" title="菜单" @click="$emit('toggle-mobile')"><Menu :size="18" /></button>
    <button v-if="canBack" class="crumb-back" title="返回" @click="goBack"><ArrowLeft :size="16" /></button>
    <div class="crumb">
      <template v-for="(s, i) in crumbs" :key="i">
        <span v-if="i" class="crumb-sep">/</span>
        <span v-if="s.to" class="crumb-link" @click="router.push(s.to)"><b>{{ s.t }}</b></span>
        <b v-else :class="i === crumbs.length - 1 ? 'seg-last' : ''">{{ s.t }}</b>
      </template>
    </div>

    <div
      ref="wrapEl"
      class="search"
      :class="{ expanded }"
      @click="expand"
      @keydown.enter="runSearch"
      @keydown.esc="onEsc"
    >
      <Search :size="15" />
      <input
        ref="inputEl"
        v-model="q"
        placeholder="搜索概念或内容…"
        @focus="expand"
        @input="onInput"
      />

      <div v-if="expanded" class="search-pop" @click.stop>
        <div class="sp-hd">
          <template v-if="term.length < 3">输入至少 3 个字符开始搜索</template>
          <template v-else-if="loading">搜索中…</template>
          <template v-else-if="suggestions.length">结果 “{{ term }}”</template>
          <template v-else>没有匹配「{{ term }}」的内容</template>
        </div>

        <a
          v-for="item in suggestions"
          :key="`${item.job_id}-${item.note_type}`"
          class="sp-row"
          @click="openItem(item)"
        >
          <span class="sp-tag">{{ noteTypeLabel(item.note_type) }}</span>
          <span class="sp-pill type-pill" :class="contentTypePill(item.content_type)">
            <component :is="contentTypeIcon(item.content_type)" :size="13" />
          </span>
          <div style="min-width:0;flex:1">
            <div class="sp-t">{{ item.title || item.job_id }}</div>
            <div class="sp-d">{{ item.domain && item.domain !== 'general' ? item.domain : '通用' }}</div>
          </div>
        </a>

        <a v-if="term.length >= 3" class="sp-foot" @click="runSearch">
          在搜索页查看全部结果 <Search :size="13" />
        </a>
      </div>
    </div>
  </div>
</template>

<style scoped>
.crumb-sep { color: var(--ink-300); }
/* 下拉里的小 type-pill:缩到 22px,配色沿用 .type-pill 既有类。 */
.sp-pill { width: 22px; height: 22px; border-radius: 6px; }
</style>
