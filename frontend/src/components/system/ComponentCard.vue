<script setup lang="ts">
// 核心组件卡（API/Scheduler/Redis/MinIO）：点 + 名 + 状态徽章 / 主标识行 / 次要指标行 + 异常说明。
// 后端算好 status（up/degraded/down/unknown），前端只渲染（不拿本地时钟比时间戳）。
import { computed } from 'vue'
import StatusBadge from '../common/StatusBadge.vue'
import { componentDotClass } from '../../utils/worker'
import { fmtDuration, fmtRelative, fmtBytes } from '../../utils/datetime'
import { COMPONENT_KIND_LABELS } from '../../types'
import type { SystemComponent } from '../../types'

const props = defineProps<{ comp: SystemComponent }>()

const kindLabel = computed(() => COMPONENT_KIND_LABELS[props.comp.kind] ?? props.comp.name)
const dotCls = computed(() => componentDotClass(props.comp.status))
const isDown = computed(() => props.comp.status === 'down')
const extra = computed<Record<string, any>>(() => props.comp.extra || {})

// 主标识行（版本 / bucket / —）。
const mainText = computed(() => {
  const c = props.comp
  if (c.kind === 'minio') {
    if (extra.value.mode === 'local') return '本地存储（无对象存储）'
    const b = extra.value.bucket
    return b ? `bucket ${b} ${extra.value.bucket_exists ? '✓' : '✗'}` : '对象存储'
  }
  // sha 版本截短 7 位(与 SystemView shortSha 一致);Redis 等语义版本(<7 位)原样。
  return c.version ? `版本 ${c.version.slice(0, 7)}` : '版本 —'
})

// 次要指标行（uptime / 心跳·loop / 内存·ping·conn / 探活）。
const metaText = computed(() => {
  const c = props.comp
  const e = extra.value
  if (c.kind === 'api') {
    const parts: string[] = []
    if (c.uptime_sec != null) parts.push(`运行 ${fmtDuration(c.uptime_sec)}`)
    if (e.rss_mb != null) parts.push(`内存 ${e.rss_mb}MB`)
    return parts.join(' · ')
  }
  if (c.kind === 'scheduler') {
    const hb = c.last_heartbeat ? `心跳 ${fmtRelative(c.last_heartbeat)}` : '无心跳'
    const lag = e.loop_lag_sec != null ? ` · loop ${e.loop_lag_sec}s` : ''
    return hb + lag
  }
  if (c.kind === 'redis') {
    const parts: string[] = []
    if (e.used_memory_human) parts.push(`内存 ${e.used_memory_human}`)
    if (e.ping_ms != null) parts.push(`ping ${e.ping_ms}ms`)
    if (e.connected_clients != null) parts.push(`${e.connected_clients} conn`)
    return parts.join(' · ')
  }
  if (c.kind === 'minio') {
    if (e.mode === 'local') return ''
    const parts: string[] = []
    if (e.objects != null) parts.push(`${e.objects} 对象`)
    if (e.size_bytes != null) parts.push(`容量 ${fmtBytes(e.size_bytes)}`)
    if (e.probe_ms != null) parts.push(`探活 ${e.probe_ms}ms`)
    return parts.join(' · ')
  }
  return ''
})
</script>

<template>
  <div class="card pad comp-card" :class="{ 'is-down': isDown }">
    <div class="comp-hd">
      <span class="dot" :class="dotCls"></span>
      <span class="name">{{ kindLabel }}</span>
      <StatusBadge :status="comp.status" />
    </div>
    <div class="comp-main" :class="{ mono: comp.kind !== 'minio' }">{{ mainText }}</div>
    <div v-if="metaText" class="comp-meta">{{ metaText }}</div>
    <div v-if="comp.detail" class="comp-note">{{ comp.detail }}</div>
  </div>
</template>
