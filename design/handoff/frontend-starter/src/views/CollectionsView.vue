<!-- 集合列表（原型 id="collections"）：集合卡片网格。占位实现。 -->
<script setup lang="ts">
import { useRouter } from 'vue-router'
import { Folder, RefreshCw, Plus, Rss, FileText, Cpu, Atom, Layers } from 'lucide-vue-next'
import type { Component } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import type { CollectionKind } from '@/types'

const router = useRouter()

// 集合卡片示例数据
// TODO: GET /api/collections
interface ColCard {
  id: string
  name: string
  kind: CollectionKind
  count: number
  kb: string
  kbIcon: Component
  tags: string[]
  desc: string
  syncText: string
  synced: boolean
}
const collections: ColCard[] = [
  { id: 'limu', name: '李沐读论文', kind: 'subscription', count: 18, kb: '机器学习', kbIcon: Cpu, tags: ['paper-reading', 'lecture'], desc: '逐段精读经典论文，配套手写公式推导与直觉解释。', syncText: '2 小时前同步', synced: true },
  { id: 'sysweekly', name: '系统设计周刊', kind: 'subscription', count: 21, kb: '系统设计', kbIcon: Atom, tags: ['architecture', 'distributed'], desc: '每周精选分布式系统与架构实践长文，自动入库。', syncText: '6 小时前同步', synced: true },
  { id: '3b1b', name: '3Blue1Brown', kind: 'manual', count: 12, kb: '机器学习', kbIcon: Cpu, tags: ['animated', 'math-visual'], desc: '手动收藏的可视化数学讲解视频，重直觉、轻公式。', syncText: '手动集合', synced: false },
  { id: 'manual', name: '手动收藏', kind: 'manual', count: 13, kb: '多知识库', kbIcon: Layers, tags: ['misc'], desc: '临时随手收藏，未归入固定订阅源的零散内容。', syncText: '手动集合', synced: false },
]

function open(id: string) {
  router.push({ name: 'collection-detail', params: { id } })
}
</script>

<template>
  <section class="page">
    <div style="display: flex; align-items: flex-end; gap: 12px; margin-bottom: 20px">
      <div>
        <div class="h1"><Folder :size="18" />集合</div>
        <div class="lead">订阅 UP 主自动追更，或手动收藏归集——投递时自动继承集合的知识库与标签。</div>
      </div>
      <BaseButton size="sm" style="margin-left: auto"><RefreshCw :size="13" />刷新</BaseButton>
      <!-- TODO: 打开新建集合弹窗 -->
      <BaseButton variant="pri"><Plus :size="14" />新建</BaseButton>
    </div>

    <div class="grid3">
      <div v-for="c in collections" :key="c.id" class="card pad col-card" @click="open(c.id)">
        <div class="chead">
          <span class="cic" :class="c.kind === 'subscription' ? 'sub' : 'man'">
            <Rss v-if="c.kind === 'subscription'" :size="16" />
            <Folder v-else :size="16" />
          </span>
          <div class="cname-wrap"><div class="cname">{{ c.name }}</div></div>
          <BaseBadge :variant="c.kind === 'subscription' ? 'info' : 'mut'" style="flex: none">
            <Rss v-if="c.kind === 'subscription'" :size="12" />{{ c.kind === 'subscription' ? '订阅' : '手动' }}
          </BaseBadge>
        </div>
        <div class="stats">
          <span style="display: flex; align-items: center; gap: 5px">
            <FileText :size="13" style="color: var(--ink-400)" />{{ c.count }} 条
          </span>
          <span style="display: flex; align-items: center; gap: 5px">
            <component :is="c.kbIcon" :size="13" style="color: var(--ink-400)" />{{ c.kb }}
          </span>
        </div>
        <div class="taglist" style="margin-bottom: 10px">
          <span v-for="t in c.tags" :key="t" class="tag">{{ t }}</span>
        </div>
        <p class="cdesc">{{ c.desc }}</p>
        <div class="cfoot" @click.stop>
          <span class="cfoot-tag">
            <span class="dot" :class="c.synced ? 'd-ok' : 'd-mut'"></span>{{ c.syncText }}
          </span>
        </div>
      </div>
    </div>
  </section>
</template>
