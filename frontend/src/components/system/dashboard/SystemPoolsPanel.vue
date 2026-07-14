<script setup lang="ts">
import { Layers } from 'lucide-vue-next'
import type { PoolStat } from '../../../types'
type Limit = { default: number; override: number | null }
defineProps<{ pools: [string, PoolStat][]; limits: Record<string, Limit>; draft: Record<string, number | null>; busy: string | null; dot: (name: string, pool: PoolStat) => string; badge: (name: string, pool: PoolStat) => { cls: string; text: string } }>()
defineEmits<{ openQueue: [pool?: string]; save: [pool: string]; reset: [pool: string]; updateDraft: [pool: string, value: number | null] }>()
</script>

<template>
  <div class="seclabel section-head"><Layers :size="14" />资源池 · {{ pools.length }}<button @click="$emit('openQueue')">查看队列 →</button></div>
  <div class="grid3" style="margin-bottom:24px"><div v-for="[name, pool] in pools" :key="name" class="card pad pool-card"><div class="pool-head"><span class="dot" :class="dot(name, pool)" /><b class="mono">{{ name }}</b><span class="badge" :class="badge(name, pool).cls" title="查看该池队列" @click="$emit('openQueue', name)">{{ badge(name, pool).text }}</span></div><div class="dim-g"><div class="row-l"><span>在跑任务</span><b>{{ pool.used }} / {{ pool.capacity === 0 ? '暂停' : pool.capacity }}</b></div><div class="track"><span :style="{ width: `${Math.min(100, pool.capacity ? (pool.used / pool.capacity) * 100 : 0)}%` }" /></div></div><div v-if="name in draft" class="limit-row"><span>上限</span><input :value="draft[name]" type="number" min="0" class="input" :placeholder="String(limits[name]?.default ?? '')" @input="$emit('updateDraft', name, Number(($event.target as HTMLInputElement).value))" /><button class="btn sm" :disabled="busy === name" @click="$emit('save', name)">{{ busy === name ? '…' : '保存' }}</button><button v-if="limits[name]?.override != null" class="btn sm" :disabled="busy === name" @click="$emit('reset', name)">默认</button><span :class="{ overridden: limits[name]?.override != null }">{{ limits[name]?.override == null ? '默认' : '已覆盖' }}</span></div></div></div>
</template>

<style scoped>
.section-head { margin-bottom: 12px; display: flex; align-items: center; }.section-head button { margin-left: auto; font-weight: 400; font-size: 11.5px; color: var(--brand-600); cursor: pointer; text-transform: none; letter-spacing: 0; background: none; }
.pool-card { padding: 13px 15px; }.pool-head { display: flex; align-items: center; gap: 7px; margin-bottom: 8px; }.pool-head b { font-size: 13px; color: var(--ink-900); flex: 1; }.pool-head .badge { cursor: pointer; }
.limit-row { display: flex; align-items: center; gap: 6px; margin-top: 9px; flex-wrap: wrap; }.limit-row > span { font-size: 11px; color: var(--ink-600); }.limit-row input { width: 64px; padding: 3px 7px; font-size: 12px; }.limit-row span:last-child { color: var(--ink-400); }.limit-row span.overridden { color: var(--brand, #7c3aed); }
</style>
