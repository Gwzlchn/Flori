<!--
  所有来源（原型 id="content"）：跨知识库的所有投递。
  分组筛选（按状态 / 来源 / 知识库，组内多选、跨组取交集）+ 内容列表。
  筛选用 Vue 响应式 Set 实现（替代原型 DOM toggle）。
-->
<script setup lang="ts">
import { reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  Inbox,
  Plus,
  X,
  Play,
  FileText,
  Newspaper,
  Headphones,
  ChevronRight,
  RotateCcw,
} from 'lucide-vue-next'
import type { Component } from 'vue'
import StatusBadge from '@/components/base/StatusBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import Chip from '@/components/base/Chip.vue'
import type { JobStatus, ContentType } from '@/types'

const router = useRouter()

// 筛选项示例数据
const statusFilters = [
  { key: '已完成', n: 118 },
  { key: '处理中', n: 7 },
  { key: '失败', n: 3 },
]
const sourceFilters = [
  { key: 'Bilibili', n: 58 },
  { key: 'YouTube', n: 13 },
  { key: 'arXiv', n: 34 },
  { key: '公众号', n: 19 },
  { key: '本地', n: 8 },
]
const kbFilters = [
  { key: '机器学习', n: 43 },
  { key: '系统设计', n: 21 },
  { key: '生物信息学', n: 9 },
]

// 已选筛选（跨组取交集）
const selected = reactive(new Set<string>())
function toggle(key: string) {
  selected.has(key) ? selected.delete(key) : selected.add(key)
}
function clearGroup(keys: string[]) {
  keys.forEach((k) => selected.delete(k))
}
function clearAll() {
  selected.clear()
}
const filterText = computed(() => {
  const arr = [...selected]
  return arr.length ? `已选 ${arr.join(' · ')} —— 跨组取交集` : '未筛选 —— 显示全部 132 条内容'
})

// 内容列表示例数据
// TODO: GET /api/content?status=&source=&kb=（按筛选组合）
interface Item {
  id: string
  title: string
  type: ContentType
  status: JobStatus
  kb: string
  source: string
  extra?: string
  currentStep?: string
  progress?: number
  failStep?: string
  clickable: boolean
}
const items: Item[] = [
  { id: 'c1', title: 'Transformer 架构详解：从注意力机制到 GPT', type: 'video', status: 'done', kb: '机器学习', source: '李沐读论文', extra: '评分 4.6 · 2 小时前', clickable: true },
  { id: 'c2', title: 'Attention Is All You Need (arXiv:1706.03762)', type: 'paper', status: 'processing', kb: '机器学习', source: '', currentStep: '08_智能笔记', progress: 72, clickable: true },
  { id: 'c3', title: '一致性哈希在分布式缓存中的应用', type: 'article', status: 'done', kb: '系统设计', source: '系统设计周刊', extra: '昨天', clickable: true },
  { id: 'c4', title: '播客：大模型训练的工程实践', type: 'audio', status: 'failed', kb: '机器学习', source: '', failStep: '02_字幕转写超时', extra: '3 天前', clickable: false },
]
const TYPE_ICON: Record<ContentType, Component> = {
  video: Play,
  paper: FileText,
  article: Newspaper,
  audio: Headphones,
}

function openContent(id: string) {
  router.push({ name: 'content-detail', params: { id } })
}
</script>

<template>
  <section class="page">
    <div style="display: flex; align-items: flex-end; gap: 12px; margin-bottom: 18px">
      <div>
        <div class="h1"><Inbox :size="18" />所有来源</div>
        <div class="lead">跨知识库的所有投递，可按来源、类型、状态筛选。</div>
      </div>
      <!-- TODO: 打开投递弹窗 -->
      <BaseButton variant="pri" style="margin-left: auto"><Plus :size="14" />投递内容</BaseButton>
    </div>

    <!-- 分组筛选 -->
    <div class="filters">
      <div class="fgroup">
        <span class="flabel">按状态</span>
        <Chip v-for="f in statusFilters" :key="f.key" :active="selected.has(f.key)" @click="toggle(f.key)">
          {{ f.key }} <span class="n">{{ f.n }}</span>
        </Chip>
        <button class="fclear" @click="clearGroup(statusFilters.map((f) => f.key))">
          <X :size="11" />清除
        </button>
      </div>
      <div class="fgroup">
        <span class="flabel">按来源</span>
        <Chip v-for="f in sourceFilters" :key="f.key" :active="selected.has(f.key)" @click="toggle(f.key)">
          {{ f.key }} <span class="n">{{ f.n }}</span>
        </Chip>
        <button class="fclear" @click="clearGroup(sourceFilters.map((f) => f.key))">
          <X :size="11" />清除
        </button>
      </div>
      <div class="fgroup">
        <span class="flabel">按知识库</span>
        <Chip v-for="f in kbFilters" :key="f.key" :active="selected.has(f.key)" @click="toggle(f.key)">
          {{ f.key }} <span class="n">{{ f.n }}</span>
        </Chip>
        <button class="fclear" @click="clearGroup(kbFilters.map((f) => f.key))">
          <X :size="11" />清除
        </button>
      </div>
      <div class="fbar">
        <span>{{ filterText }}</span>
        <button class="ghost" @click="clearAll"><X :size="14" />清除全部</button>
      </div>
    </div>

    <!-- 内容列表 -->
    <div class="list">
      <div
        v-for="it in items"
        :key="it.id"
        class="row"
        :style="it.clickable ? '' : 'cursor:default'"
        @click="it.clickable && openContent(it.id)"
      >
        <span class="type-pill" :class="`t-${it.type}`">
          <component :is="TYPE_ICON[it.type]" :size="17" />
        </span>
        <div class="body">
          <div class="title">{{ it.title }}</div>
          <div class="meta">
            <StatusBadge :status="it.status" />
            <span>{{ it.kb }}</span>
            <template v-if="it.status === 'processing'">
              <span class="sep">·</span>
              <span class="muted">{{ it.currentStep }}</span>
              <span class="bar"><span :style="{ width: `${it.progress}%` }"></span></span>
              <span class="dim">{{ it.progress }}%</span>
            </template>
            <template v-else-if="it.status === 'failed'">
              <span class="sep">·</span>
              <span class="muted" style="color: var(--bad)">{{ it.failStep }}</span>
              <span class="sep">·</span><span class="dim">{{ it.extra }}</span>
            </template>
            <template v-else>
              <span v-if="it.source" class="sep">·</span>
              <span v-if="it.source">{{ it.source }}</span>
              <span class="sep">·</span><span class="dim">{{ it.extra }}</span>
            </template>
          </div>
        </div>
        <!-- TODO: POST /api/content/{id}/retry -->
        <BaseButton v-if="it.status === 'failed'" size="sm" @click.stop>
          <RotateCcw :size="13" />重试
        </BaseButton>
        <ChevronRight v-else-if="it.clickable" :size="16" class="dim" />
      </div>
    </div>

    <div class="load-hint">
      <span class="spinner" style="width: 15px; height: 15px; border-width: 2px"></span>
      滚动到底自动加载更多…
    </div>
  </section>
</template>
