<script setup lang="ts">
import { AlertTriangle } from 'lucide-vue-next'
import { fmtRelative } from '../../../utils/datetime'
import { eventDot, eventLabel, eventSummary } from '../../../utils/events'
import type { SystemEvent } from '../../../types'
defineProps<{ events: SystemEvent[] }>()
defineEmits<{ openAll: [] }>()
</script>

<template>
  <div class="seclabel section-head"><AlertTriangle :size="14" />系统事件<button @click="$emit('openAll')">查看全部 →</button></div>
  <div class="card pad" style="margin-bottom:24px">
    <div v-if="events.length === 0" class="empty"><span class="dot d-ok" />系统运行平稳，近期无告警</div>
    <div v-else class="list"><div v-for="(event, index) in events.slice(0, 5)" :key="index" class="event-row"><span class="dot" :class="eventDot(event.kind)" /><span class="time">{{ fmtRelative(event.ts * 1000) }}</span><b>{{ eventLabel(event.kind) }}</b><span class="summary">{{ eventSummary(event) }}</span></div></div>
  </div>
</template>

<style scoped>
.section-head { margin-bottom: 10px; display: flex; align-items: center; }
.section-head button { margin-left: auto; font-weight: 400; font-size: 11.5px; color: var(--brand-600); cursor: pointer; text-transform: none; letter-spacing: 0; background: none; }
.empty { display: flex; align-items: center; gap: 8px; color: var(--ink-500); font-size: 13px; }
.event-row { display: flex; align-items: center; gap: 9px; font-size: 12.5px; }
.time { color: var(--ink-500); min-width: 64px; }
.event-row b { color: var(--ink-900); }
.summary { color: var(--ink-600); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
