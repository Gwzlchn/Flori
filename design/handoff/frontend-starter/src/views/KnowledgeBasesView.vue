<!--
  知识库总览（原型 id="home"）：
  标题 + 「新建知识库」+ 知识库卡片网格（彩色图标块 / 名 / 订阅角标 / N 集合·N 条·N 概念 / 活跃态）
  + 近期内容列表（跨知识库）。
-->
<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Library,
  Plus,
  ChevronRight,
  Cpu,
  Atom,
  Dna,
  Folder,
  FileText,
  Lightbulb,
  Rss,
  Clock,
  Play,
  Newspaper,
} from 'lucide-vue-next'
import type { Component } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import StatusBadge from '@/components/base/StatusBadge.vue'
import Modal from '@/components/base/Modal.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import type { Content } from '@/types'

const router = useRouter()

// 知识库卡片示例数据
// TODO: GET /api/knowledge-bases
interface KbCard {
  id: string
  name: string
  icon: Component
  gradient: string
  collectionCount: number
  contentCount: number
  conceptCount: number
  subscriptionCount?: number
  activeText: string
  active: boolean
}

const knowledgeBases: KbCard[] = [
  {
    id: 'ml',
    name: '机器学习',
    icon: Cpu,
    gradient: 'linear-gradient(135deg,#6366f1,#4338ca)',
    collectionCount: 5,
    contentCount: 43,
    conceptCount: 128,
    subscriptionCount: 2,
    activeText: '12 分钟前活跃',
    active: true,
  },
  {
    id: 'sysdesign',
    name: '系统设计',
    icon: Atom,
    gradient: 'linear-gradient(135deg,#0ea5e9,#0369a1)',
    collectionCount: 3,
    contentCount: 21,
    conceptCount: 67,
    activeText: '2 小时前活跃',
    active: true,
  },
  {
    id: 'bioinfo',
    name: '生物信息学',
    icon: Dna,
    gradient: 'linear-gradient(135deg,#10b981,#047857)',
    collectionCount: 2,
    contentCount: 9,
    conceptCount: 31,
    subscriptionCount: 1,
    activeText: '3 天前活跃',
    active: false,
  },
]

// 近期内容（跨知识库）示例数据
// TODO: GET /api/content?sort=recent&limit=N
const recentContents: (Content & { icon: Component })[] = [
  {
    id: 'c1',
    title: 'Transformer 架构详解：从注意力机制到 GPT',
    type: 'video',
    status: 'done',
    knowledgeBaseName: '机器学习',
    source: 'Bilibili',
    rating: 4.6,
    icon: Play,
  },
  {
    id: 'c2',
    title: 'Attention Is All You Need (arXiv:1706.03762)',
    type: 'paper',
    status: 'processing',
    knowledgeBaseName: '机器学习',
    currentStep: '08_智能笔记',
    progress: 72,
    icon: FileText,
  },
  {
    id: 'c3',
    title: '一致性哈希在分布式缓存中的应用',
    type: 'article',
    status: 'done',
    knowledgeBaseName: '系统设计',
    source: '公众号',
    icon: Newspaper,
  },
]

const showCreate = ref(false)

function openKb(id: string) {
  router.push({ name: 'knowledge-base', params: { id } })
}
function openContent(id: string) {
  router.push({ name: 'content-detail', params: { id } })
}
</script>

<template>
  <section class="page">
    <div style="display: flex; align-items: flex-end; gap: 12px; margin-bottom: 20px">
      <div>
        <div class="h1"><Library :size="18" />我的知识库</div>
        <div class="lead">投递的每条内容都会自动归入对应知识库，逐步沉淀成体系。</div>
      </div>
      <BaseButton variant="pri" style="margin-left: auto" @click="showCreate = true">
        <Plus :size="14" />新建知识库
      </BaseButton>
      <BaseButton variant="ghost" @click="router.push('/content')">
        所有来源<ChevronRight :size="14" />
      </BaseButton>
    </div>

    <!-- 知识库卡片网格 -->
    <div class="grid3" style="margin-bottom: 28px">
      <a v-for="kb in knowledgeBases" :key="kb.id" class="dcard" @click="openKb(kb.id)">
        <div class="top">
          <span class="ic" :style="{ background: kb.gradient }">
            <component :is="kb.icon" :size="18" />
          </span>
          <h3>{{ kb.name }}</h3>
          <BaseBadge v-if="kb.subscriptionCount" variant="info">
            <Rss :size="12" />{{ kb.subscriptionCount }}
          </BaseBadge>
        </div>
        <div class="stats">
          <span><Folder :size="13" />{{ kb.collectionCount }} 集合</span>
          <span><FileText :size="13" />{{ kb.contentCount }} 条</span>
          <span><Lightbulb :size="13" />{{ kb.conceptCount }} 概念</span>
        </div>
        <div class="foot">
          <span class="dot" :class="kb.active ? 'd-ok' : 'd-mut'"></span>{{ kb.activeText }}
        </div>
      </a>
    </div>

    <!-- 近期内容列表 -->
    <div class="seclabel" style="margin-bottom: 12px"><Clock :size="14" />近期内容 · 跨知识库</div>
    <div class="list">
      <div v-for="c in recentContents" :key="c.id" class="row" @click="openContent(c.id)">
        <span class="type-pill" :class="`t-${c.type}`"><component :is="c.icon" :size="17" /></span>
        <div class="body">
          <div class="title">{{ c.title }}</div>
          <div class="meta">
            <StatusBadge :status="c.status" />
            <span>{{ c.knowledgeBaseName }}</span>
            <template v-if="c.status === 'processing'">
              <span class="sep">·</span>
              <span class="muted">{{ c.currentStep }}</span>
              <span class="bar"><span :style="{ width: `${c.progress}%` }"></span></span>
              <span class="dim">{{ c.progress }}%</span>
            </template>
            <template v-else>
              <span class="sep">·</span>
              <span>{{ c.source }}</span>
              <template v-if="c.rating">
                <span class="sep">·</span><span>评分 {{ c.rating }}</span>
              </template>
            </template>
          </div>
        </div>
        <ChevronRight v-if="c.status !== 'processing'" :size="16" class="dim" />
      </div>
    </div>

    <!-- 新建知识库弹窗（占位，字段后续补全） -->
    <Modal v-model="showCreate" title="新建知识库">
      <div class="field">
        <label>名称</label>
        <input class="input" placeholder="如：强化学习、密码学、宏观经济…" />
        <div class="note-tip">知识库是知识的命名空间，互相隔离。建好后投递内容时选它即可归入。</div>
      </div>
      <template #footer="{ close }">
        <BaseButton @click="close">取消</BaseButton>
        <!-- TODO: POST /api/knowledge-bases -->
        <BaseButton variant="pri" @click="close"><Plus :size="14" />创建知识库</BaseButton>
      </template>
    </Modal>
  </section>
</template>
