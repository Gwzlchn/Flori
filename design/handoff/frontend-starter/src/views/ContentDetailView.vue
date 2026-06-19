<!--
  内容详情（原型 id="detail"）：
  头卡 + tabs（笔记 / 概念 / 流水线 / 元信息），用 v-if 切换。
  - 笔记 tab：评审面板占位 + Markdown 容器占位 + 章节 TOC。
  - 概念 tab：本内容概念反查列表（点进去可看全库哪些内容也讲过它）。
  - 流水线 tab：步骤时间线占位。
  - 元信息 tab：kv 表。
-->
<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowLeft,
  Play,
  BookOpen,
  Lightbulb,
  GitBranch,
  Info,
  RefreshCw,
  Star,
  ExternalLink,
  Check,
  Loader,
  Minus,
  ChevronRight,
  RotateCcw,
  Trash2,
} from 'lucide-vue-next'
import type { Component } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import StatusBadge from '@/components/base/StatusBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import type { ContentType } from '@/types'

const router = useRouter()

// 路由参数（内容 id），真实数据按 id 拉取
// TODO: GET /api/content/{id}
const props = defineProps<{ id?: string }>()
void props.id

type TabKey = 'notes' | 'concepts' | 'pipeline' | 'info'
const activeTab = ref<TabKey>('notes')

const tabs: { key: TabKey; label: string; icon: Component }[] = [
  { key: 'notes', label: '笔记', icon: BookOpen },
  { key: 'concepts', label: '概念', icon: Lightbulb },
  { key: 'pipeline', label: '流水线', icon: GitBranch },
  { key: 'info', label: '元信息', icon: Info },
]

// 笔记版本（智能版 / 机械版）
const noteVariant = ref<'smart' | 'raw'>('smart')

// 评审 8 维度示例数据
// TODO: GET /api/content/{id}/review
const reviewDims = [
  { label: '完整性', score: 5 },
  { label: '准确性', score: 5 },
  { label: '结构', score: 4 },
  { label: '概念', score: 5 },
  { label: '配图', score: 4 },
  { label: '可读性', score: 5 },
  { label: '公式', score: 4 },
  { label: '图表引用', score: 5 },
]

// 章节 TOC 示例数据
const toc = [
  { label: '1 · 为什么需要注意力机制', active: true, sub: false },
  { label: '2 · 自注意力的计算', active: false, sub: false },
  { label: '2.1 多头注意力', active: false, sub: true },
  { label: '3 · 位置编码', active: false, sub: false },
  { label: '4 · 完整架构与训练', active: false, sub: false },
]

// 本内容涉及的概念（反查全库）示例数据
// TODO: GET /api/content/{id}/concepts
interface ContentConcept {
  id: string
  name: string
  desc: string
  isTopic?: boolean
  accepted: boolean
}
const concepts: ContentConcept[] = [
  { id: 't1', name: '注意力机制', desc: '首次出现 02:34 · 全库 14 条内容讲过', isTopic: true, accepted: true },
  { id: 't2', name: 'Query-Key-Value', desc: '05:18 · 全库 9 条', accepted: true },
  { id: 't3', name: '多头注意力', desc: '08:12 · 全库 7 条', accepted: true },
  { id: 't4', name: '位置编码', desc: '11:40 · 候选，待确认', accepted: false },
  { id: 't5', name: '残差连接', desc: '15:02 · 全库 5 条', accepted: true },
  { id: 't6', name: '层归一化', desc: '16:20 · 候选，待确认', accepted: false },
  { id: 't7', name: '前馈网络', desc: '18:30 · 全库 6 条', accepted: true },
]

// 流水线步骤示例数据
// TODO: GET /api/content/{id}/pipeline （步骤时间线）
type StepState = 'done' | 'running' | 'skipped' | 'waiting' | 'failed'
const steps: { name: string; key: string; state: StepState; meta: string }[] = [
  { name: '下载', key: '01_download', state: 'done', meta: '完成 · 18s' },
  { name: '语音转写', key: '02_transcribe', state: 'done', meta: '完成 · 2m10s' },
  { name: '弹幕', key: '07_danmaku', state: 'skipped', meta: '已跳过 · 无弹幕' },
  { name: '智能版笔记', key: '10_smart', state: 'done', meta: '完成 · 3m02s' },
  { name: '质量评审', key: '11_review', state: 'running', meta: '运行中 · 45%' },
]
const STEP_ICON: Record<StepState, Component> = {
  done: Check,
  running: Loader,
  skipped: Minus,
  waiting: Minus,
  failed: Minus,
}

const contentType: ContentType = 'video'

function goConcept(id: string) {
  void id
  // TODO: 概念详情路由（暂指回知识库工作台占位）
  router.push({ name: 'knowledge-base', params: { id: 'ml' } })
}
</script>

<template>
  <section class="page wide">
    <BaseButton variant="ghost" style="margin-bottom: 12px" @click="router.back()">
      <ArrowLeft :size="14" />机器学习 · 李沐读论文
    </BaseButton>

    <!-- 头卡 -->
    <div class="card pad" style="margin-bottom: 16px">
      <div style="display: flex; align-items: flex-start; gap: 13px">
        <span class="type-pill t-video" style="width: 42px; height: 42px"><Play :size="17" /></span>
        <div style="flex: 1; min-width: 0">
          <div class="h1 sm">Transformer 架构详解：从注意力机制到 GPT</div>
          <div class="meta" style="margin-top: 4px">
            <StatusBadge status="done" />
            <BaseBadge variant="mut">视频</BaseBadge>
            <span>Bilibili</span><span class="sep">·</span><span>机器学习</span>
            <span class="sep">·</span><span class="mono dim">BV1Kt4y1...</span>
            <span class="sep">·</span>
            <a class="ghost" style="font-size: 12px; color: var(--info)">
              原始链接<ExternalLink :size="14" />
            </a>
          </div>
          <div class="dim" style="font-size: 12px; margin-top: 2px">
            上传于 2025/05/30 · 生成 06/15 14:20 → 14:32 · 耗时 12m
          </div>
        </div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      <button
        v-for="t in tabs"
        :key="t.key"
        :class="{ on: activeTab === t.key }"
        @click="activeTab = t.key"
      >
        <component :is="t.icon" :size="15" />{{ t.label }}
      </button>
    </div>

    <!-- TAB 笔记 -->
    <div v-if="activeTab === 'notes'">
      <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap">
        <div class="seg">
          <button :class="{ on: noteVariant === 'smart' }" @click="noteVariant = 'smart'">
            智能版
          </button>
          <button :class="{ on: noteVariant === 'raw' }" @click="noteVariant = 'raw'">机械版</button>
        </div>
        <span class="dim" style="font-size: 12px; margin-left: 6px">版本</span>
        <span class="chip on">claude/opus · 06/15 <Star :size="11" style="color: var(--amber)" />4.6</span>
        <span class="chip">deepseek · 06/12 <Star :size="11" style="color: var(--amber)" />4.2</span>
        <BaseButton size="sm" style="margin-left: auto">
          <!-- TODO: POST /api/content/{id}/rerun?provider=... -->
          <RefreshCw :size="13" />换 provider 重跑
        </BaseButton>
      </div>

      <!-- 评审面板占位 -->
      <div class="review" style="margin-bottom: 16px">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <b style="font-size: 13.5px; color: var(--ink-900)">质量评审</b>
          <BaseBadge variant="warn"><Star :size="12" />4.6 / 5</BaseBadge>
          <span class="dim" style="font-size: 12px">claude / claude-opus · 06/15</span>
        </div>
        <div class="dims">
          <div v-for="d in reviewDims" :key="d.label" class="dim-g">
            <div class="row-l">{{ d.label }}<b>{{ d.score }}</b></div>
            <div class="track"><span :style="{ width: `${(d.score / 5) * 100}%` }"></span></div>
          </div>
        </div>
        <div style="font-size: 12.5px; color: var(--ink-600)">
          <span class="dim">已讲清的概念（可采纳）：</span>
          <b style="color: var(--ink-900)">注意力机制</b>
          <!-- TODO: POST /api/content/{id}/concepts/{name}/accept -->
          <BaseButton size="sm" style="padding: 2px 8px; margin: 0 6px">采纳</BaseButton>
          <b style="color: var(--ink-900)">位置编码</b>
          <BaseButton size="sm" style="padding: 2px 8px; margin-left: 6px">采纳</BaseButton>
        </div>
      </div>

      <!-- Markdown 容器占位 + 章节 TOC -->
      <div class="notes-wrap">
        <div class="card pad prose">
          <!-- TODO: 渲染 GET /api/content/{id}/notes/{variant}.md（Markdown -> HTML） -->
          <h2>1 · 为什么需要注意力机制</h2>
          <p>
            传统 RNN 按时间步串行处理序列，难以并行，且<span class="term-link">长程依赖</span>会随距离衰减。<span class="ts">[02:34]</span>
            视频用一句话翻译示例说明了这个痛点。
          </p>
          <h2>2 · 自注意力的计算</h2>
          <p>
            核心是 <span class="term-link">query-key-value</span> 三组向量。<span class="ts">[08:12]</span>
            点积得相关性权重，softmax 归一化后对 value 加权求和。
          </p>
          <div class="note-tip" style="margin-top: 12px">
            （Markdown 正文容器占位 —— 实际由后端笔记文件渲染。蓝色虚线词 = 已收录概念，[时间戳] 可跳转。）
          </div>
        </div>
        <nav class="toc">
          <div class="seclabel"><BookOpen :size="14" />章节</div>
          <a
            v-for="(t, i) in toc"
            :key="i"
            :class="{ on: t.active, sub: t.sub }"
          >{{ t.label }}</a>
        </nav>
      </div>
    </div>

    <!-- TAB 概念（本内容概念反查全库） -->
    <div v-else-if="activeTab === 'concepts'">
      <div class="card pad">
        <div class="card-h"><Lightbulb :size="15" />本内容涉及的概念 · {{ concepts.length }}</div>
        <p class="lead" style="margin: -4px 0 12px">
          从这条内容抽取 / 出现的概念。点进去可反查它在整个知识库里——还有哪些内容也讲过它。
        </p>
        <div v-for="c in concepts" :key="c.id" class="concept" @click="goConcept(c.id)">
          <Lightbulb v-if="c.isTopic" class="pin" :size="14" />
          <span v-else style="width: 14px"></span>
          <div style="flex: 1; min-width: 0">
            <div class="t">
              {{ c.name }}
              <BaseBadge v-if="c.isTopic" variant="brand" style="margin-left: 4px">主题概念</BaseBadge>
            </div>
            <div class="d">{{ c.desc }}</div>
          </div>
          <BaseBadge :variant="c.accepted ? 'ok' : 'warn'" style="flex: none">
            {{ c.accepted ? '已收录' : '候选' }}
          </BaseBadge>
          <ChevronRight :size="16" class="dim" style="flex: none" />
        </div>
      </div>
    </div>

    <!-- TAB 流水线（步骤时间线占位） -->
    <div v-else-if="activeTab === 'pipeline'">
      <div class="card pad" style="max-width: 320px">
        <div class="card-h"><GitBranch :size="15" />步骤时间线 · video</div>
        <div class="timeline">
          <div v-for="s in steps" :key="s.key" class="tl-step" :class="{ on: s.state === 'running' }">
            <div class="tl-ic" :class="s.state"><component :is="STEP_ICON[s.state]" :size="13" /></div>
            <div>
              <div class="tl-name">{{ s.name }}</div>
              <div class="tl-key">{{ s.key }}</div>
              <div class="tl-meta" :class="{ dim: s.state === 'skipped' }">{{ s.meta }}</div>
            </div>
          </div>
        </div>
      </div>
      <div class="note-tip" style="margin-top: 12px">
        （步骤工作台占位 —— 右侧应展示选中步骤的产物 / 日志 / 重跑入口。）
      </div>
    </div>

    <!-- TAB 元信息 -->
    <div v-else-if="activeTab === 'info'">
      <div class="card pad" style="max-width: 560px">
        <div class="card-h"><Info :size="15" />元信息</div>
        <table class="kv">
          <tbody>
            <tr><td>标题</td><td>Transformer 架构详解：从注意力机制到 GPT</td></tr>
            <tr><td>类型</td><td>视频</td></tr>
            <tr><td>来源</td><td>Bilibili</td></tr>
            <tr><td>知识库</td><td>机器学习</td></tr>
            <tr><td>集合</td><td>李沐读论文</td></tr>
            <tr><td>BV 号</td><td class="mono">BV1Kt4y1A7e8</td></tr>
            <tr><td>状态</td><td><StatusBadge status="done" /></td></tr>
          </tbody>
        </table>
        <div style="margin-top: 16px; display: flex; gap: 8px">
          <!-- TODO: POST /api/content/{id}/resubmit -->
          <BaseButton><RotateCcw :size="14" />重新提交</BaseButton>
          <!-- TODO: DELETE /api/content/{id} -->
          <BaseButton variant="danger"><Trash2 :size="14" />删除内容</BaseButton>
        </div>
      </div>
    </div>
    <!-- contentType 预留给后续按类型渲染（视频/论文/文章/播客差异） -->
    <span hidden>{{ contentType }}</span>
  </section>
</template>
