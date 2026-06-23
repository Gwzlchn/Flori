<script setup lang="ts">
import { computed } from 'vue'
import { statusLabel } from '../../utils/status'

// 状态枚举 → flori.css 徽章色(.badge .b-*)。文案统一走 utils/status 的 statusLabel(单一来源)。
// status 在各域间基本唯一,扁平表即可解析。
const props = defineProps<{ status: string }>()

// 状态 → 徽章配色类(视觉,本组件专属;文案见 utils/status)。
const COLOR: Record<string, string> = {
  pending: 'b-mut', downloading: 'b-info', processing: 'b-run', done: 'b-ok', failed: 'b-bad',
  waiting: 'b-mut', ready: 'b-mut', running: 'b-run', skipped: 'b-mut',
  idle: 'b-ok', busy: 'b-info',
  'online-idle': 'b-ok', 'online-busy': 'b-info', paused: 'b-warn', offline: 'b-mut', stale: 'b-bad',
  // 系统组件四态 → 复用既有徽章色(无新颜色)
  up: 'b-ok', degraded: 'b-warn', down: 'b-bad', unknown: 'b-mut',
  suggested: 'b-warn', accepted: 'b-ok',
}

const color = computed(() => COLOR[props.status] ?? 'b-mut')
const label = computed(() => statusLabel(props.status))
</script>

<template>
  <span class="badge" :class="[color, { 'b-dashed': status === 'skipped' }]">{{ label }}</span>
</template>

<style scoped>
/* skipped 用虚线灰,视觉上绝不能像 failed */
.b-dashed { border: 1px dashed var(--ink-300); background: transparent; color: var(--ink-500); }
</style>
