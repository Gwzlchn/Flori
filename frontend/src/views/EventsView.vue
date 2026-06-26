<script setup lang="ts">
// 系统事件全量页(/system/events):从 /system 概览的「查看全部」进入。类型 + 时间区间筛选,时间倒序。
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useWorkerStore } from '../stores/workers'
import { useGlobalStore } from '../stores/global'
import { fmtDateTime, fmtRelative } from '../utils/datetime'
import { eventLabel, eventDot, eventSummary, EVENT_KINDS } from '../utils/events'
import type { SystemEvent } from '../types'
import { AlertTriangle, RefreshCw, ArrowLeft } from 'lucide-vue-next'

const router = useRouter()
const workerStore = useWorkerStore()
const global = useGlobalStore()

const events = ref<SystemEvent[]>([])
const loading = ref(false)
const kindFilter = ref('')   // '' = 全部
const rangeFilter = ref('')  // '' = 全部 / 1h / 24h / 7d

const RANGES = [
  { id: '', label: '全部时间' },
  { id: '1h', label: '近 1 小时' },
  { id: '24h', label: '近 24 小时' },
  { id: '7d', label: '近 7 天' },
]
const RANGE_SEC: Record<string, number> = { '1h': 3600, '24h': 86400, '7d': 604800 }

async function load() {
  loading.value = true
  try {
    events.value = (await workerStore.fetchEvents(500)).events
  } catch { /* 非致命:留空 */ } finally {
    loading.value = false
  }
}

const filtered = computed(() => {
  const cutoff = rangeFilter.value ? Date.now() / 1000 - RANGE_SEC[rangeFilter.value] : 0
  return events.value.filter(e =>
    (!kindFilter.value || e.kind === kindFilter.value) &&
    (!cutoff || e.ts >= cutoff),
  )
})
// 类型下拉:已知类型 + 数据里出现过的未知类型(并集),去重。
const kindOptions = computed(() => {
  const seen = new Set<string>(EVENT_KINDS)
  for (const e of events.value) seen.add(e.kind)
  return [...seen]
})

onMounted(() => {
  global.setCrumbs([{ t: '系统', to: '/system' }, { t: '事件' }])
  load()
})
</script>

<template>
  <section class="page">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
      <button class="btn sm" @click="router.push('/system')"><ArrowLeft :size="14" />系统</button>
      <div class="h1"><AlertTriangle :size="18" />系统事件</div>
      <button class="btn sm" style="margin-left:auto" :disabled="loading" @click="load">
        <RefreshCw :size="13" :class="loading ? 'spin' : ''" />刷新
      </button>
    </div>

    <!-- 筛选 -->
    <div class="card pad" style="margin-bottom:16px;display:flex;align-items:center;gap:10px 16px;flex-wrap:wrap">
      <div class="field" style="margin:0">
        <label>类型</label>
        <select v-model="kindFilter" class="input" style="min-width:140px">
          <option value="">全部类型</option>
          <option v-for="k in kindOptions" :key="k" :value="k">{{ eventLabel(k) }}</option>
        </select>
      </div>
      <div class="field" style="margin:0">
        <label>时间</label>
        <select v-model="rangeFilter" class="input" style="min-width:140px">
          <option v-for="r in RANGES" :key="r.id" :value="r.id">{{ r.label }}</option>
        </select>
      </div>
      <span style="margin-left:auto;font-size:12.5px;color:var(--ink-500)">{{ filtered.length }} / {{ events.length }} 条</span>
    </div>

    <!-- 列表 -->
    <div class="card pad">
      <div v-if="loading && events.length === 0" style="color:var(--ink-500);font-size:13px">加载中…</div>
      <div v-else-if="filtered.length === 0" style="display:flex;align-items:center;gap:8px;color:var(--ink-500);font-size:13px">
        <span class="dot d-ok"></span>{{ events.length === 0 ? '系统运行平稳，近期无告警' : '当前筛选无匹配事件' }}
      </div>
      <div v-else class="list">
        <div v-for="(e, i) in filtered" :key="i" class="ev-row">
          <span class="dot" :class="eventDot(e.kind)"></span>
          <span class="ev-time" :title="fmtDateTime(e.ts * 1000)">{{ fmtRelative(e.ts * 1000) }}</span>
          <b class="ev-kind">{{ eventLabel(e.kind) }}</b>
          <span class="ev-sum">{{ eventSummary(e) }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.ev-row { display: flex; align-items: center; gap: 10px; font-size: 12.5px; padding: 5px 0; border-bottom: 1px solid var(--line-soft); }
.ev-row:last-child { border-bottom: 0; }
.ev-time { color: var(--ink-500); min-width: 78px; flex: none; }
.ev-kind { color: var(--ink-900); min-width: 72px; flex: none; }
.ev-sum { color: var(--ink-600); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
