<!--
  全文搜索（原型 id="search"）：搜索框 + 类型/知识库过滤 + 结果列表。
  交互：输入 ≥3 字才搜索；即时过滤示例数据；无匹配显示空状态。
-->
<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { Search, PencilLine, SearchX, Play, FileText, Newspaper } from 'lucide-vue-next'
import type { Component } from 'vue'
import EmptyState from '@/components/base/EmptyState.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import type { ContentType } from '@/types'

const router = useRouter()

const keyword = ref('注意力机制')

// 搜索结果示例数据
// TODO: GET /api/search?q={keyword}&type=&kb=
interface Result {
  id: string
  title: string
  type: ContentType
  noteKind: string
  snippet: string
  meta: string
}
const allResults: Result[] = [
  { id: 'c1', title: 'Transformer 架构详解：从注意力机制到 GPT', type: 'video', noteKind: '智能笔记', snippet: '核心是 query-key-value 三组向量，注意力机制通过点积计算序列内元素的相关性权重，softmax 归一化后对 value 加权求和…', meta: '视频 · 机器学习 · Bilibili' },
  { id: 'c2', title: 'Attention Is All You Need (arXiv:1706.03762)', type: 'paper', noteKind: '智能笔记', snippet: '本文提出完全基于注意力机制的 Transformer，摒弃循环与卷积，在机器翻译任务上取得更优表现且更易并行…', meta: '论文 · 机器学习 · arXiv' },
  { id: 'c3', title: '直观理解自注意力：可视化讲解', type: 'video', noteKind: '机械稿', snippet: '[08:12] 我们用动画一步步拆开注意力机制，看 query 如何在所有 key 上打分、再加权聚合 value…', meta: '视频 · 机器学习 · 3Blue1Brown' },
  { id: 'c4', title: '从 Seq2Seq 到 Transformer 的演进', type: 'article', noteKind: '智能笔记', snippet: '早期 Seq2Seq 引入了对齐注意力机制缓解长程依赖，最终 self-attention 让模型彻底摆脱循环结构…', meta: '文章 · 机器学习 · 公众号' },
]
const TYPE_ICON: Record<ContentType, Component> = {
  video: Play,
  paper: FileText,
  article: Newspaper,
  audio: FileText,
}

// 即时过滤（演示）：标题或摘要包含关键词
const filtered = computed(() => {
  const q = keyword.value.trim()
  if (q.length < 3) return []
  return allResults.filter((r) => (r.title + r.snippet).includes(q))
})
const tooShort = computed(() => keyword.value.trim().length < 3)

function openContent(id: string) {
  router.push({ name: 'content-detail', params: { id } })
}
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom: 16px"><Search :size="18" />搜索</div>

    <div class="search" style="width: 100%; padding: 11px 14px">
      <Search :size="17" />
      <input v-model="keyword" style="font-size: 14px" />
      <span class="kbd">⌘K</span>
    </div>
    <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px">
      <select class="input" style="max-width: 140px">
        <option>全部类型</option>
        <option>视频</option>
        <option>论文</option>
        <option>文章</option>
        <option>播客</option>
      </select>
      <input class="input" placeholder="知识库过滤" style="max-width: 160px" />
    </div>

    <!-- 空状态：输入太短 -->
    <EmptyState v-if="tooShort" :icon="PencilLine" text="请至少输入 3 个字符再搜索" style="margin-top: 18px" />

    <!-- 空状态：无匹配 -->
    <EmptyState
      v-else-if="filtered.length === 0"
      :icon="SearchX"
      :text="`没有匹配「${keyword.trim()}」的内容`"
      style="margin-top: 18px"
    />

    <!-- 结果列表 -->
    <template v-else>
      <div class="muted" style="font-size: 12.5px; margin: 18px 0 12px">共 {{ filtered.length }} 条结果</div>
      <div class="list">
        <div
          v-for="r in filtered"
          :key="r.id"
          class="card pad"
          style="cursor: pointer; display: flex; align-items: flex-start; gap: 13px"
          @click="openContent(r.id)"
        >
          <span class="type-pill" :class="`t-${r.type}`" style="margin-top: 1px">
            <component :is="TYPE_ICON[r.type]" :size="17" />
          </span>
          <div style="flex: 1; min-width: 0">
            <div style="display: flex; align-items: center; gap: 8px">
              <div class="title" style="flex: 1; min-width: 0">{{ r.title }}</div>
              <BaseBadge variant="mut">{{ r.noteKind }}</BaseBadge>
            </div>
            <p style="font-size: 13px; color: var(--ink-600); margin: 6px 0 0">{{ r.snippet }}</p>
            <div class="meta" style="margin-top: 7px"><span>{{ r.meta }}</span></div>
          </div>
        </div>
      </div>
    </template>

    <div class="note-tip" style="margin-top: 14px">
      输入需 ≥ 3 字才会搜索；改上方关键词即时过滤（演示），无匹配显示空状态。
    </div>
  </section>
</template>
