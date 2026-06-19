<!-- 集合详情（原型 id="collection"）：集合信息 + 订阅源 + 投递入口 + 内容列表。占位实现。 -->
<script setup lang="ts">
import { useRouter } from 'vue-router'
import {
  ArrowLeft,
  Rss,
  RefreshCw,
  Info,
  ExternalLink,
  Check,
  Send,
  LayoutList,
  Play,
  ChevronRight,
} from 'lucide-vue-next'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import StatusBadge from '@/components/base/StatusBadge.vue'

const router = useRouter()

// 路由参数（集合 id）
// TODO: GET /api/collections/{id}
const props = defineProps<{ id?: string }>()
void props.id

// 内容列表示例数据
const contents = [
  { id: 'c1', title: 'Transformer 架构详解：从注意力机制到 GPT', meta: '32:18 · 评分 4.6 · 2 小时前' },
  { id: 'c2', title: 'BERT 论文精读：双向预训练语言模型', meta: '48:55 · 评分 4.5 · 昨天' },
  { id: 'c3', title: 'ResNet：深度残差网络逐段精读', meta: '53:10 · 评分 4.7 · 3 天前' },
]

function openContent(id: string) {
  router.push({ name: 'content-detail', params: { id } })
}
</script>

<template>
  <section class="page">
    <BaseButton variant="ghost" style="margin-bottom: 14px" @click="router.push('/collections')">
      <ArrowLeft :size="14" />返回集合
    </BaseButton>

    <div style="display: flex; align-items: center; gap: 13px; margin-bottom: 6px">
      <span
        class="cic sub"
        style="width: 42px; height: 42px; border-radius: 11px"
      >
        <Rss :size="16" />
      </span>
      <div>
        <div class="h1">
          李沐读论文 <BaseBadge variant="info" style="margin-left: 4px"><Rss :size="12" />订阅</BaseBadge>
        </div>
        <div class="lead">机器学习 · 18 条内容 · 2 小时前同步</div>
      </div>
      <BaseButton size="sm" style="margin-left: auto"><RefreshCw :size="13" />刷新</BaseButton>
    </div>

    <div class="grid2" style="margin-top: 18px; align-items: start">
      <div class="card pad">
        <div class="card-h"><Info :size="15" />集合信息</div>
        <table class="kv">
          <tbody>
            <tr><td>ID</td><td class="mono">col_limu_paper</td></tr>
            <tr><td>知识库</td><td>机器学习</td></tr>
            <tr><td>标签</td><td><span class="tag">paper-reading</span> <span class="tag">lecture</span></td></tr>
            <tr><td>内容</td><td>18 条</td></tr>
          </tbody>
        </table>
      </div>

      <div class="card pad">
        <div class="card-h"><Rss :size="15" />订阅源</div>
        <table class="kv" style="margin-bottom: 13px">
          <tbody>
            <tr><td>来源</td><td>B站 UP 主</td></tr>
            <tr>
              <td>UP 主页</td>
              <td><a class="ghost" style="color: var(--info)">space.bilibili.com/12345<ExternalLink :size="14" /></a></td>
            </tr>
            <tr><td>上次同步</td><td>2 小时前</td></tr>
            <tr><td>追更状态</td><td><BaseBadge variant="ok"><Check :size="12" />追更中</BaseBadge></td></tr>
          </tbody>
        </table>
        <div style="display: flex; align-items: center; gap: 12px; padding-top: 11px; border-top: 1px solid var(--line-soft)">
          <!-- TODO: POST /api/collections/{id}/sync -->
          <BaseButton size="sm"><RefreshCw :size="13" />立即同步</BaseButton>
          <span style="margin-left: auto; display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink-600)">
            自动同步<div class="switch on"></div>
          </span>
        </div>
      </div>
    </div>

    <div class="card pad" style="margin-top: 16px">
      <div class="card-h"><Send :size="15" />投递到此集合</div>
      <div style="display: flex; gap: 10px; align-items: center">
        <input class="input" placeholder="粘贴 Bilibili 视频链接…" />
        <!-- TODO: POST /api/content（collection_id 自动继承本集合） -->
        <BaseButton variant="pri" style="flex: none"><Send :size="14" />投递</BaseButton>
      </div>
      <div class="note-tip">知识库 / 集合自动继承本集合（机器学习 · 李沐读论文）。</div>
    </div>

    <div class="seclabel" style="margin: 22px 0 12px"><LayoutList :size="14" />内容 · 18</div>
    <div class="list">
      <div v-for="c in contents" :key="c.id" class="row" @click="openContent(c.id)">
        <span class="type-pill t-video"><Play :size="17" /></span>
        <div class="body">
          <div class="title">{{ c.title }}</div>
          <div class="meta"><StatusBadge status="done" /><span>{{ c.meta }}</span></div>
        </div>
        <ChevronRight :size="16" class="dim" />
      </div>
    </div>
  </section>
</template>
