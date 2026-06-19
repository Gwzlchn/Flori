<!-- 概念库（原型 id="glossary"）：待审建议 + 已采纳概念列表。占位实现。 -->
<script setup lang="ts">
import { ref } from 'vue'
import { Lightbulb, Plus, Sparkles, Check, X, CheckCircle2, Bookmark, Pencil, Trash2 } from 'lucide-vue-next'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import Modal from '@/components/base/Modal.vue'

// 待审建议示例数据
// TODO: GET /api/concepts?status=candidate
const suggestions = [
  { id: 's1', name: '位置编码', desc: '向输入嵌入注入序列顺序信息，常用正弦/余弦函数或可学习向量… · 出现于 7 条' },
  { id: 's2', name: '层归一化', desc: '在特征维度上对每个样本做归一化以稳定训练，区别于批归一化… · 出现于 5 条' },
  { id: 's3', name: '学习率调度', desc: '训练过程中按策略动态调整学习率，如 warmup 后线性衰减… · 出现于 4 条' },
]

// 已采纳概念示例数据
// TODO: GET /api/concepts?status=accepted
const accepted = [
  { id: 'a1', name: '注意力机制', desc: '通过 query-key-value 计算序列内元素相关性权重的机制 · 关联 3', isTopic: true },
  { id: 'a2', name: '梯度下降', desc: '沿损失函数负梯度方向迭代更新参数的优化方法 · 关联 2', isTopic: false },
  { id: 'a3', name: '残差连接', desc: '将输入直接加到输出，缓解深层网络梯度消失 · 关联 2', isTopic: false },
  { id: 'a4', name: '反向传播', desc: '利用链式法则从输出层向输入层逐层计算梯度的算法 · 关联 3', isTopic: false },
]

const showCreate = ref(false)
</script>

<template>
  <section class="page">
    <div style="display: flex; align-items: flex-end; gap: 12px; margin-bottom: 20px">
      <div>
        <div class="h1"><Lightbulb :size="18" />概念库</div>
        <div class="lead">AI 从内容中提取候选概念，采纳后沉淀为可检索的知识节点与正文概念链接。</div>
      </div>
    </div>

    <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 22px; flex-wrap: wrap">
      <select class="input" style="max-width: 160px">
        <option>全部知识库</option>
        <option>机器学习</option>
        <option>系统设计</option>
      </select>
      <BaseButton variant="pri" style="margin-left: auto" @click="showCreate = true">
        <Plus :size="14" />新增概念
      </BaseButton>
    </div>

    <!-- 待审建议 -->
    <div class="seclabel" style="margin-bottom: 12px"><Sparkles :size="14" />待审建议 · {{ suggestions.length }}</div>
    <div class="card pad" style="margin-bottom: 26px; border-color: var(--warn-bd)">
      <div v-for="s in suggestions" :key="s.id" class="occ" style="cursor: default">
        <div style="flex: 1; min-width: 0">
          <div style="display: flex; align-items: center; gap: 8px">
            <span style="font-weight: 600; color: var(--ink-900)">{{ s.name }}</span>
            <BaseBadge variant="warn"><Sparkles :size="12" />待确认</BaseBadge>
          </div>
          <div class="d" style="font-size: 12px; color: var(--ink-500); margin-top: 2px">{{ s.desc }}</div>
        </div>
        <!-- TODO: POST /api/concepts/{id}/accept -->
        <button class="btn sm" style="color: var(--ok); border-color: var(--ok-bd)"><Check :size="13" />采纳</button>
        <!-- TODO: POST /api/concepts/{id}/reject -->
        <button class="iconbtn"><X :size="16" /></button>
      </div>
    </div>

    <!-- 已采纳 -->
    <div class="seclabel" style="margin-bottom: 12px"><CheckCircle2 :size="14" />已采纳 · 128</div>
    <div class="card pad">
      <div v-for="a in accepted" :key="a.id" class="occ">
        <div style="flex: 1; min-width: 0">
          <div style="display: flex; align-items: center; gap: 8px">
            <span class="occ-t" style="font-weight: 600; color: var(--ink-900)">{{ a.name }}</span>
            <BaseBadge v-if="a.isTopic" variant="brand"><Bookmark :size="12" />主题概念</BaseBadge>
          </div>
          <div class="d" style="font-size: 12px; color: var(--ink-500); margin-top: 2px">{{ a.desc }}</div>
        </div>
        <button class="iconbtn" @click.stop><Pencil :size="16" /></button>
        <button class="iconbtn" @click.stop><Trash2 :size="16" /></button>
      </div>
    </div>

    <!-- 新增概念弹窗占位 -->
    <Modal v-model="showCreate" title="新增概念">
      <div class="field"><label>概念名</label><input class="input" placeholder="如 注意力机制" /></div>
      <div class="field" style="margin-bottom: 0">
        <label>定义</label>
        <textarea class="input" placeholder="用一两句话说明该概念的核心含义…"></textarea>
      </div>
      <template #footer="{ close }">
        <BaseButton @click="close">取消</BaseButton>
        <!-- TODO: POST /api/concepts -->
        <BaseButton variant="pri" @click="close"><Plus :size="14" />添加</BaseButton>
      </template>
    </Modal>
  </section>
</template>
