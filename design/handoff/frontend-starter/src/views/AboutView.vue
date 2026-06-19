<!-- 关于 Mnemo（原型 id="about"）：项目说明 + 核心循环 + 怎么用 + 四个层次。占位实现。 -->
<script setup lang="ts">
import { useRouter } from 'vue-router'
import {
  ArrowLeft,
  BookOpen,
  Info,
  GitBranch,
  Send,
  RefreshCw,
  FileText,
  Check,
  Search,
  ChevronRight,
  List,
  Layers,
  Cpu,
} from 'lucide-vue-next'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const router = useRouter()

// 怎么用 —— 四步
const steps = [
  { type: 'video', n: 1, title: '投递', desc: '粘贴链接或拖文件，丢进收件箱即可' },
  { type: 'paper', n: 2, title: '等待自动处理', desc: '后台跑流水线，手机可关掉，处理完通知你' },
  { type: 'article', n: 3, title: '阅读智能笔记', desc: '点时间戳回看原片，点概念查定义' },
  { type: 'audio', n: 4, title: '采纳概念', desc: '在评审里把讲清的概念「采纳」进知识库，越攒越成体系' },
]

// 四个层次 —— IA 命名：知识库 ⊃ 集合 ⊃ 内容；概念为知识层
const levels = [
  { name: '知识库', desc: '按知识范围分，互相隔离 —— 你知识体系的一级容器' },
  { name: '集合', desc: '知识库内对内容的分组 —— 订阅源（如某 UP 主合集）或手动收藏' },
  { name: '内容', desc: '每条投递的视频 · 论文 · 文章 · 播客，归入某个集合' },
  { name: '概念', desc: '从内容里抽取、你学会的知识点，自动连成网、跨内容互相引用' },
]
</script>

<template>
  <section class="page">
    <BaseButton variant="ghost" style="margin-bottom: 12px" @click="router.push('/settings')">
      <ArrowLeft :size="14" />返回设置
    </BaseButton>

    <div class="h1"><BookOpen :size="18" />关于 Mnemo</div>
    <div class="lead">把视频 / 论文 / 文章 / 播客自动转成结构化笔记，沉淀为可检索、互相关联的个人知识库。</div>

    <div class="card pad" style="margin-top: 18px">
      <div class="card-h"><Info :size="15" />这是什么</div>
      <p style="color: var(--ink-700)">
        Mnemo 把你投递的学习材料自动转成<b>两种笔记</b>：<b>机械版</b>逐字稿 + 关键截图，忠实还原原内容；<b>智能版</b>由 AI 重组成结构化讲解，便于阅读理解。
      </p>
      <p style="color: var(--ink-700); margin-top: 9px">
        这些笔记按知识范围归入各个 <b>知识库</b>，并通过其中抽取的 <b>概念</b> 互相关联，逐渐织成一张属于你自己的知识网。
      </p>
    </div>

    <div class="card pad" style="margin-top: 16px">
      <div class="card-h"><GitBranch :size="15" />核心循环</div>
      <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
        <BaseBadge variant="brand"><Send :size="12" />投递 URL/文件</BaseBadge>
        <ChevronRight :size="16" class="dim" />
        <BaseBadge variant="info"><RefreshCw :size="12" />自动处理</BaseBadge>
        <ChevronRight :size="16" class="dim" />
        <BaseBadge variant="mut"><FileText :size="12" />读智能笔记</BaseBadge>
        <ChevronRight :size="16" class="dim" />
        <BaseBadge variant="ok"><Check :size="12" />采纳概念</BaseBadge>
        <ChevronRight :size="16" class="dim" />
        <BaseBadge variant="brand"><GitBranch :size="12" />连成知识网</BaseBadge>
        <ChevronRight :size="16" class="dim" />
        <BaseBadge variant="mut"><Search :size="12" />搜索回溯</BaseBadge>
      </div>
      <div class="note-tip" style="margin-top: 11px">自动处理含：下载 · 转写 · 截图 · OCR · 抽概念</div>
    </div>

    <div class="card pad" style="margin-top: 16px">
      <div class="card-h"><List :size="15" />怎么用</div>
      <div class="list">
        <div v-for="s in steps" :key="s.n" class="row" style="cursor: default">
          <span class="type-pill" :class="`t-${s.type}`">
            <span :style="{ fontWeight: 700, color: `var(--t-${s.type})` }">{{ s.n }}</span>
          </span>
          <div class="body">
            <div class="title">{{ s.title }}</div>
            <div class="meta"><span>{{ s.desc }}</span></div>
          </div>
        </div>
      </div>
    </div>

    <div class="card pad" style="margin-top: 16px">
      <div class="card-h"><Layers :size="15" />四个层次</div>
      <table class="kv">
        <tbody>
          <tr v-for="l in levels" :key="l.name"><td>{{ l.name }}</td><td>{{ l.desc }}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card pad" style="margin-top: 16px">
      <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12.5px; color: var(--ink-500)">
        <BaseBadge variant="mut"><Cpu :size="12" />技术栈</BaseBadge>
        <span>Vue 3 · FastAPI · SQLite · Docker</span>
        <span class="sep" style="color: var(--ink-300)">·</span>
        <span>全 Docker 自托管，数据完全自有</span>
        <span class="sep" style="color: var(--ink-300)">·</span>
        <span>MIT 开源</span>
      </div>
    </div>
  </section>
</template>
