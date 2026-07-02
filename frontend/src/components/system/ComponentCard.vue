<script setup lang="ts">
// 核心组件卡(API/Scheduler/Redis/MinIO):点 + 名 + 状态徽章 / 主标识行 / 次要指标行 + 异常说明。
// 后端算好 status(up/degraded/down/unknown),前端只渲染,不拿本地时钟比时间戳。
import { computed } from 'vue'
import StatusBadge from '../common/StatusBadge.vue'
import { componentDotClass } from '../../utils/worker'
import { fmtDuration, fmtRelative } from '../../utils/datetime'
import { fmtBytes } from '../../utils/format'
import { COMPONENT_KIND_LABELS } from '../../types'
import type { SystemComponent } from '../../types'

const props = defineProps<{ comp: SystemComponent }>()

const kindLabel = computed(() => COMPONENT_KIND_LABELS[props.comp.kind] ?? props.comp.name)
const dotCls = computed(() => componentDotClass(props.comp.status))
const isDown = computed(() => props.comp.status === 'down')
const extra = computed<Record<string, any>>(() => props.comp.extra || {})

// 主标识行(版本 / bucket / —)。
const mainText = computed(() => {
  const c = props.comp
  if (c.kind === 'minio') {
    if (extra.value.mode === 'local') return '本地存储（无对象存储）'
    const b = extra.value.bucket
    const head = b ? `bucket ${b} ${extra.value.bucket_exists ? '✓' : '✗'}` : '对象存储'
    // MinIO 版本是 RELEASE 日期(如 2025-09-07T..),取日期段;无则不显。
    return c.version ? `${head} · 版本 ${c.version.slice(0, 10)}` : head
  }
  // 版本显示:FLORI_VERSION="0.2.0+sha" → v0.2.0;Redis 等(7.4.9)→ v7.4.9;无则 —。
  if (!c.version) return '版本 —'
  const sem = c.version.split('+')[0]
  return `版本 ${/^\d/.test(sem) ? 'v' + sem : sem}`
})

// 次要指标行:统一以「运行 <时长>」起头,时长取 uptime;除 minio 本地盘外各组件都有。之后接各自指标。
const metaText = computed(() => {
  const c = props.comp
  const e = extra.value
  if (c.kind === 'minio' && e.mode === 'local') return ''   // 本地盘无运行时长概念
  const parts: string[] = []
  if (c.uptime_sec != null) parts.push(`运行 ${fmtDuration(c.uptime_sec)}`)
  if (c.kind === 'api') {
    if (e.rss_mb != null) parts.push(`内存 ${e.rss_mb}MB`)
  } else if (c.kind === 'scheduler') {
    parts.push(c.last_heartbeat ? `心跳 ${fmtRelative(c.last_heartbeat)}` : '无心跳')
    if (e.loop_lag_sec != null) parts.push(`loop ${e.loop_lag_sec}s`)
  } else if (c.kind === 'redis') {
    if (e.used_memory_human) parts.push(`内存 ${e.used_memory_human}`)
    if (e.ping_ms != null) parts.push(`ping ${e.ping_ms}ms`)
    if (e.connected_clients != null) parts.push(`${e.connected_clients} conn`)
  } else if (c.kind === 'minio') {
    if (e.objects != null) parts.push(`${e.objects} 对象`)
    if (e.size_bytes != null) parts.push(`容量 ${fmtBytes(e.size_bytes)}`)
    if (e.probe_ms != null) parts.push(`探活 ${e.probe_ms}ms`)
  }
  return parts.join(' · ')
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
