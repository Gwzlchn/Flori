<script setup lang="ts">
import { computed } from 'vue'

// 状态枚举 → 中文 + mnemo.css 徽章色(.badge .b-*)。全站统一,勿自造。
// kind 可选(仅用于语义标注);status 在各域间基本唯一,扁平表即可解析。
const props = defineProps<{ status: string; kind?: 'job' | 'step' | 'worker' | 'concept' }>()

const MAP: Record<string, [string, string]> = {
  // job
  pending: ['等待', 'b-mut'], downloading: ['下载中', 'b-info'], processing: ['处理中', 'b-run'],
  done: ['已完成', 'b-ok'], failed: ['失败', 'b-bad'],
  // step
  waiting: ['等待', 'b-mut'], ready: ['就绪', 'b-mut'], running: ['运行中', 'b-run'], skipped: ['跳过', 'b-mut'],
  // worker（idle/busy 为旧态兼容）
  idle: ['空闲', 'b-ok'], busy: ['忙碌', 'b-info'],
  'online-idle': ['空闲', 'b-ok'], 'online-busy': ['忙碌', 'b-info'], draining: ['排空中', 'b-warn'],
  offline: ['离线', 'b-mut'], stale: ['失联', 'b-bad'],
  // concept
  suggested: ['候选', 'b-warn'], accepted: ['已采纳', 'b-ok'],
}

const cfg = computed<[string, string]>(() => MAP[props.status] ?? [props.status, 'b-mut'])
</script>

<template>
  <span class="badge" :class="[cfg[1], { 'b-dashed': status === 'skipped' }]">{{ cfg[0] }}</span>
</template>

<style scoped>
/* skipped 用虚线灰,视觉上绝不能像 failed */
.b-dashed { border: 1px dashed var(--ink-300); background: transparent; color: var(--ink-500); }
</style>
