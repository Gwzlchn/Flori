<script setup lang="ts">
// 关于 Mnemo（原型 #about）：纯静态，三层心智模型 + 项目说明。无数据请求。
import {
  BookOpen, Info, GitBranch, RefreshCw, FileText, Check, Send,
  Search, List, Layers, Cpu, Play, Newspaper, Headphones, ChevronRight,
} from 'lucide-vue-next'
</script>

<template>
  <section class="page">
    <div class="h1"><BookOpen :size="18" />关于 Mnemo</div>
    <div class="lead">把视频 / 论文 / 文章 / 播客自动转成结构化笔记，沉淀为可检索、互相关联的个人知识库。</div>

    <!-- 这是什么 -->
    <div class="card pad" style="margin-top:18px">
      <div class="card-h"><Info :size="15" />这是什么</div>
      <p style="color:var(--ink-700)">
        Mnemo 把你投递的学习材料自动转成<b>两种笔记</b>：<b>机械版</b>逐字稿 + 关键截图，忠实还原原内容；<b>智能版</b>由 AI 重组成结构化讲解，便于阅读理解。
      </p>
      <p style="color:var(--ink-700);margin-top:9px">
        这些笔记按知识范围归入各个 <b>知识库</b>（Notebook），并通过其中抽取的 <b>概念</b> 互相关联，逐渐织成一张属于你自己的知识网。
      </p>
    </div>

    <!-- 核心循环 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><GitBranch :size="15" />核心循环</div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="badge b-brand"><Send :size="12" />投递 URL/文件</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-info"><RefreshCw :size="12" />自动处理</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-mut"><FileText :size="12" />读智能笔记</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-ok"><Check :size="12" />采纳概念</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-brand"><GitBranch :size="12" />连成知识网</span>
        <ChevronRight :size="14" class="dim" />
        <span class="badge b-mut"><Search :size="12" />搜索回溯</span>
      </div>
      <div class="note-tip" style="margin-top:11px">自动处理含：下载 · 转写 · 截图 · OCR · 抽概念</div>
    </div>

    <!-- 怎么用 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><List :size="15" />怎么用</div>
      <div class="list">
        <div class="row" style="cursor:default">
          <span class="type-pill t-video"><Play :size="17" /></span>
          <div class="body">
            <div class="title">投递</div>
            <div class="meta"><span>粘贴链接或拖文件，丢进收件箱即可</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-paper"><FileText :size="17" /></span>
          <div class="body">
            <div class="title">等待自动处理</div>
            <div class="meta"><span>后台跑流水线，手机可关掉，处理完通知你</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-article"><Newspaper :size="17" /></span>
          <div class="body">
            <div class="title">阅读智能笔记</div>
            <div class="meta"><span>点时间戳回看原片，点概念查定义</span></div>
          </div>
        </div>
        <div class="row" style="cursor:default">
          <span class="type-pill t-audio"><Headphones :size="17" /></span>
          <div class="body">
            <div class="title">采纳概念</div>
            <div class="meta"><span>在评审里把讲清的概念「采纳」进知识库，越攒越成体系</span></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 四个层次 -->
    <div class="card pad" style="margin-top:16px">
      <div class="card-h"><Layers :size="15" />四个层次</div>
      <table class="kv">
        <tbody>
          <tr><td>知识库</td><td>按知识范围分，互相隔离 —— 你知识体系的一级容器</td></tr>
          <tr><td>集合</td><td>知识库内对内容的分组 —— 订阅源（如某 UP 主合集）或手动收藏</td></tr>
          <tr><td>内容</td><td>每条投递的视频 · 论文 · 文章 · 播客，归入某个集合</td></tr>
          <tr><td>概念</td><td>从内容里抽取、你学会的知识点，自动连成网、跨内容互相引用</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 技术栈 -->
    <div class="card pad" style="margin-top:16px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-500)">
        <span class="badge b-mut"><Cpu :size="12" />技术栈</span>
        <span>Vue 3 · FastAPI · SQLite · Docker</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span>全 Docker 自托管，数据完全自有</span>
        <span class="sep" style="color:var(--ink-300)">·</span>
        <span>MIT 开源</span>
      </div>
    </div>
  </section>
</template>
