<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Search } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const q = ref('')

interface Seg { t: string; to?: string }

// 面包屑由路由派生(Phase 1 用路由名;后续可接视图里的真实标题)
const crumbs = computed<Seg[]>(() => {
  const p = route.params as any
  const root: Seg = { t: '知识库', to: '/' }
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
    case 'search': return [{ t: '搜索' }]
    case 'system': return [{ t: '系统' }]
    case 'worker-detail': return [{ t: '系统', to: '/system' }, { t: 'Worker 详情' }]
    case 'settings': return [{ t: '设置' }]
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
function runSearch() {
  const v = q.value.trim()
  if (v) router.push(`/search?q=${encodeURIComponent(v)}`)
}
</script>

<template>
  <div class="topbar">
    <button v-if="canBack" class="crumb-back" title="返回" @click="goBack"><ArrowLeft :size="16" /></button>
    <div class="crumb">
      <template v-for="(s, i) in crumbs" :key="i">
        <span v-if="i" class="crumb-sep">/</span>
        <span v-if="s.to" class="crumb-link" @click="router.push(s.to)"><b>{{ s.t }}</b></span>
        <b v-else :class="i === crumbs.length - 1 ? 'seg-last' : ''">{{ s.t }}</b>
      </template>
    </div>
    <div class="search" @keydown.enter="runSearch">
      <Search :size="15" />
      <input v-model="q" placeholder="搜索概念或内容…" />
    </div>
  </div>
</template>

<style scoped>
.crumb-sep { color: var(--ink-300); }
</style>
