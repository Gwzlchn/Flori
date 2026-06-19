<!--
  顶栏：面包屑 + 可展开搜索框。对应原型 .topbar（crumb + search）。
  搜索：点击展开为横栏 + 下拉建议（不直接跳转），点页面其它处收起。
-->
<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, ArrowRight } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()

interface Crumb {
  label: string
  to?: string
}

// 面包屑：按路由派生（示例文案，真实数据应来自当前实体名称）
// TODO: 详情页面包屑里的实体名应取自已加载的数据（知识库名 / 内容标题 / 集合名）
const crumbs = computed<Crumb[]>(() => {
  const name = route.name
  switch (name) {
    case 'knowledge-bases':
      return [{ label: '知识库' }]
    case 'knowledge-base':
      return [{ label: '知识库', to: '/' }, { label: '机器学习' }]
    case 'content-detail':
      return [
        { label: '所有来源', to: '/content' },
        { label: '内容详情' },
      ]
    case 'content-list':
      return [{ label: '所有来源' }]
    case 'collections':
      return [{ label: '集合' }]
    case 'collection-detail':
      return [{ label: '集合', to: '/collections' }, { label: '李沐读论文' }]
    case 'search':
      return [{ label: '搜索' }]
    case 'glossary':
      return [{ label: '概念库' }]
    case 'system':
      return [{ label: '系统' }]
    case 'settings':
      return [{ label: '设置' }]
    case 'about':
      return [{ label: '设置', to: '/settings' }, { label: '关于' }]
    default:
      return [{ label: '知识库' }]
  }
})

const expanded = ref(false)
const keyword = ref('')
const rootEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLInputElement | null>(null)

// 下拉建议示例数据
// TODO: GET /api/search/suggest?q={keyword}
const suggestions = [
  { tag: '概念', title: '注意力机制', desc: '机器学习 · 14 条内容讲过' },
  { tag: '内容', title: 'Transformer 架构详解：从注意力到 GPT', desc: '机器学习 · 视频' },
  { tag: '内容', title: 'Attention Is All You Need', desc: '机器学习 · 论文' },
]

function expand() {
  expanded.value = true
  // 等 DOM 更新后聚焦
  requestAnimationFrame(() => inputEl.value?.focus())
}
function collapse() {
  expanded.value = false
}
function goSearch() {
  collapse()
  router.push('/search')
}

// 点击搜索框外部收起
function onDocClick(e: MouseEvent) {
  if (rootEl.value && !rootEl.value.contains(e.target as Node)) collapse()
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div class="topbar">
    <div class="crumb">
      <template v-for="(c, i) in crumbs" :key="i">
        <span v-if="i > 0"> / </span>
        <RouterLink v-if="c.to" :to="c.to" class="crumb-link"><b>{{ c.label }}</b></RouterLink>
        <b v-else>{{ c.label }}</b>
      </template>
    </div>

    <div ref="rootEl" class="search" :class="{ expanded }" @click.stop="expand">
      <Search :size="15" />
      <input
        ref="inputEl"
        v-model="keyword"
        placeholder="搜索内容、概念…（⌘K）"
        @focus="expand"
      />
      <span class="kbd">⌘K</span>
      <div class="search-pop">
        <div class="sp-hd">
          {{ keyword ? `结果 "${keyword}"` : '快速找概念或内容（演示数据）' }}
        </div>
        <a v-for="(s, i) in suggestions" :key="i" class="sp-row" @click="goSearch">
          <span class="sp-tag">{{ s.tag }}</span>
          <div>
            <div class="sp-t">{{ s.title }}</div>
            <div class="sp-d">{{ s.desc }}</div>
          </div>
        </a>
        <a class="sp-foot" @click="goSearch">在搜索页查看全部结果 <ArrowRight :size="13" /></a>
      </div>
    </div>
  </div>
</template>
