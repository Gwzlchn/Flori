<script setup lang="ts">
import type { ReadinessState } from '../../../types'
defineProps<{ readiness: ReadinessState | null; failed: boolean; label: string; panelClass: string; hiddenReasonCount: number }>()
</script>

<template>
  <div class="readiness card" :class="panelClass">
    <div class="rd-title">{{ label }}</div>
    <div v-if="failed" class="rd-detail">无法取得最新健康状态,旧快照已清除;请检查 API、隧道和反向代理</div>
    <div v-else-if="readiness?.reasons.length" class="rd-reasons">
      <div v-for="reason in readiness.reasons.slice(0, 4)" :key="reason.code" class="rd-reason"><b>{{ reason.message }}</b><span v-if="reason.recovery">{{ reason.recovery }}</span></div>
      <div v-if="hiddenReasonCount" class="rd-detail">另有 {{ hiddenReasonCount }} 条原因未展开</div>
    </div>
    <div v-else-if="readiness" class="rd-detail">Redis、数据库、存储、调度器、数据盘与必要 Worker 均满足接单条件</div>
    <div v-else class="rd-detail">正在等待最新健康状态</div>
  </div>
</template>

<style scoped>
.readiness { margin-bottom: 18px; padding: 12px 14px; border-left: 3px solid var(--ink-300); }
.readiness.rd-ok { border-left-color: var(--ok); background: color-mix(in srgb, var(--ok) 5%, var(--surface)); }
.readiness.rd-warn { border-left-color: var(--warn); background: color-mix(in srgb, var(--warn) 6%, var(--surface)); }
.readiness.rd-bad { border-left-color: var(--bad); background: color-mix(in srgb, var(--bad) 5%, var(--surface)); }
.rd-title { font-size: 13px; font-weight: 700; color: var(--ink-900); }
.rd-detail { margin-top: 3px; font-size: 12px; color: var(--ink-500); }
.rd-reasons { display: grid; gap: 5px; margin-top: 7px; }
.rd-reason { display: flex; gap: 8px; align-items: baseline; font-size: 11.5px; color: var(--ink-600); }
.rd-reason b { color: var(--ink-800); }
</style>
