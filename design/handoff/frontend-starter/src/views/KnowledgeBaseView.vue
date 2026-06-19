<!--
  知识库工作台（原型 id="domain"）：
  头部（图标 + 名 + 统计 + 设定按钮）+ tabs（内容 / 概念 / 时间线）。
  内容 tab 按集合分组（col-group）。占位实现，结构对齐原型。
-->
<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowLeft,
  Cpu,
  SlidersHorizontal,
  RefreshCw,
  Folder,
  Lightbulb,
  BarChart3,
  Plus,
  Rss,
  ChevronRight,
  Play,
  FileText,
  Inbox,
} from 'lucide-vue-next'
import type { Component } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import StatusBadge from '@/components/base/StatusBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import Chip from '@/components/base/Chip.vue'
import type { JobStatus, ContentType } from '@/types'

const router = useRouter()

// 路由参数（知识库 id）
// TODO: GET /api/knowledge-bases/{id}
const props = defineProps<{ id?: string }>()
void props.id

type TabKey = 'content' | 'concept' | 'timeline'
const activeTab = ref<TabKey>('content')
const tabs: { key: TabKey; label: string; icon: Component }[] = [
  { key: 'content', label: '内容', icon: Folder },
  { key: 'concept', label: '概念', icon: Lightbulb },
  { key: 'timeline', label: '时间线', icon: BarChart3 },
]

// 内容按集合分组示例数据
// TODO: GET /api/knowledge-bases/{id}/content?group=collection
interface GroupItem {
  id: string
  title: string
  type: ContentType
  status: JobStatus
  meta: string
  currentStep?: string
  progress?: number
}
interface ColGroup {
  id: string
  name: string
  kind: 'subscription' | 'manual' | 'unsorted'
  total: number
  items: GroupItem[]
}
const groups: ColGroup[] = [
  {
    id: 'limu',
    name: '李沐读论文',
    kind: 'subscription',
    total: 18,
    items: [
      { id: 'c1', title: 'Transformer 架构详解：从注意力机制到 GPT', type: 'video', status: 'done', meta: '32:18 · 评分 4.6' },
      { id: 'c2', title: 'Batch Normalization 原论文精读', type: 'paper', status: 'processing', meta: '', currentStep: '06_OCR', progress: 40 },
    ],
  },
  {
    id: '3b1b',
    name: '3Blue1Brown',
    kind: 'manual',
    total: 12,
    items: [{ id: 'c3', title: '反向传播算法的直觉理解', type: 'video', status: 'done', meta: '14:02' }],
  },
  {
    id: 'unsorted',
    name: '未归集合',
    kind: 'unsorted',
    total: 13,
    items: [{ id: 'c4', title: '深度残差学习 ResNet 精读', type: 'paper', status: 'done', meta: 'arXiv' }],
  },
]
const TYPE_ICON: Record<ContentType, Component> = {
  video: Play,
  paper: FileText,
  article: FileText,
  audio: FileText,
}

function openContent(id: string) {
  router.push({ name: 'content-detail', params: { id } })
}
function openCollection(id: string) {
  router.push({ name: 'collection-detail', params: { id } })
}
</script>

<template>
  <section class="page">
    <BaseButton variant="ghost" style="margin-bottom: 14px" @click="router.push('/')">
      <ArrowLeft :size="14" />返回知识库
    </BaseButton>

    <div style="display: flex; align-items: center; gap: 13px; margin-bottom: 6px">
      <span
        class="dcard ic"
        style="width: 42px; height: 42px; background: linear-gradient(135deg, #6366f1, #4338ca); padding: 0"
      >
        <Cpu :size="18" />
      </span>
      <div>
        <div class="h1">机器学习</div>
        <div class="lead">5 集合 · 43 条内容 · 128 概念 · 12 分钟前活跃</div>
      </div>
      <BaseButton size="sm" style="margin-left: auto">
        <!-- TODO: 打开知识库设定弹窗（ProfileEditor） -->
        <SlidersHorizontal :size="13" />知识库设定
      </BaseButton>
      <BaseButton size="sm"><RefreshCw :size="13" />刷新</BaseButton>
    </div>

    <div class="tabs" style="margin-top: 18px">
      <button
        v-for="t in tabs"
        :key="t.key"
        :class="{ on: activeTab === t.key }"
        @click="activeTab = t.key"
      >
        <component :is="t.icon" :size="15" />{{ t.label }}
      </button>
    </div>

    <!-- 内容 tab：按集合分组 -->
    <div v-if="activeTab === 'content'">
      <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; align-items: center">
        <Chip><Plus :size="13" />新建集合 / 订阅</Chip>
        <BaseButton variant="ghost" style="font-size: 12px" @click="router.push('/collections')">
          管理集合
        </BaseButton>
        <span class="divider-v"></span>
        <span class="dim" style="font-size: 12px">主题</span>
        <Chip>#注意力机制</Chip><Chip>#优化器</Chip><Chip>#CNN</Chip>
      </div>

      <div v-for="g in groups" :key="g.id" class="col-group">
        <div class="col-gh" @click="g.kind !== 'unsorted' && openCollection(g.id)">
          <Rss v-if="g.kind === 'subscription'" :size="15" style="color: var(--brand-500)" />
          <Inbox v-else-if="g.kind === 'unsorted'" :size="15" />
          <Folder v-else :size="15" />
          <b>{{ g.name }}</b>
          <BaseBadge v-if="g.kind === 'subscription'" variant="info">订阅</BaseBadge>
          <BaseBadge v-else-if="g.kind === 'manual'" variant="mut">手动</BaseBadge>
          <span class="dim" style="font-size: 12px">{{ g.total }} 条</span>
          <ChevronRight v-if="g.kind !== 'unsorted'" :size="16" class="dim" style="margin-left: auto" />
        </div>
        <div class="list">
          <div v-for="it in g.items" :key="it.id" class="row" @click="openContent(it.id)">
            <span class="type-pill" :class="`t-${it.type}`">
              <component :is="TYPE_ICON[it.type]" :size="17" />
            </span>
            <div class="body">
              <div class="title">{{ it.title }}</div>
              <div class="meta">
                <StatusBadge :status="it.status" />
                <template v-if="it.status === 'processing'">
                  <span class="muted">{{ it.currentStep }}</span>
                  <span class="bar"><span :style="{ width: `${it.progress}%` }"></span></span>
                  <span class="dim">{{ it.progress }}%</span>
                </template>
                <span v-else>{{ it.meta }}</span>
              </div>
            </div>
            <ChevronRight v-if="it.status !== 'processing'" :size="16" class="dim" />
          </div>
        </div>
      </div>
    </div>

    <!-- 概念 tab（占位） -->
    <div v-else-if="activeTab === 'concept'">
      <div class="callout warn" style="margin-bottom: 14px">
        <Lightbulb :size="16" />有 6 个 AI 提取的待确认概念。
        <BaseButton size="sm" style="margin-left: auto" @click="router.push('/glossary')">
          去审阅
        </BaseButton>
      </div>
      <div class="note-tip">（概念列表占位 —— 按佐证强度排序，详见概念库 /glossary。）</div>
    </div>

    <!-- 时间线 tab（占位：图表需后续接入） -->
    <div v-else>
      <div class="card pad">
        <div class="card-h"><BarChart3 :size="15" />概念时间线</div>
        <div class="note-tip">
          （堆叠柱状图占位 —— 按年/季/月统计概念在内容中的出现次数。图表库由后续会话接入。）
        </div>
      </div>
    </div>
  </section>
</template>
