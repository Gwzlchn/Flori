<script setup lang="ts">
// 概念时间线（工作台「时间线」tab）：后端聚合 GET /api/domains/{d}/concept-timeline?granularity=day|week|month。
// Chart.js 堆叠柱（各概念分桶计数）；点柱下钻到该桶内各概念计数 → 概念详情页。
import { ref, computed, onMounted, watch, nextTick, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useDomainStore } from '../stores/domains'
import type { ConceptTimeline, TimelineGranularity } from '../types'
import { BarChart3 } from 'lucide-vue-next'
import {
  Chart, BarController, BarElement, CategoryScale, LinearScale, Tooltip, Legend,
} from 'chart.js'

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip, Legend)

const props = defineProps<{ domain: string }>()
const router = useRouter()
const domainStore = useDomainStore()

const gran = ref<TimelineGranularity>('month')
const loading = ref(true)
const error = ref('')
const data = ref<ConceptTimeline | null>(null)
const selected = ref<string | null>(null)
const canvasEl = ref<HTMLCanvasElement | null>(null)
let chart: Chart | null = null

const PALETTE = ['#2383e2', '#9065b0', '#448361', '#d9730d', '#c0392b', '#2e9b9b', '#cb7b1f', '#6b6f76']
const GRANS: { k: TimelineGranularity; t: string }[] = [
  { k: 'day', t: '按日' }, { k: 'week', t: '按周' }, { k: 'month', t: '按月' },
]

// 取 Top 8 概念作堆叠系列（concepts 已按 total 降序）
const topConcepts = computed(() => (data.value?.concepts ?? []).slice(0, 8))
const isEmpty = computed(() => !loading.value && !error.value && !(data.value?.buckets.length))

async function load() {
  loading.value = true
  error.value = ''
  selected.value = null
  try {
    data.value = await domainStore.conceptTimeline(props.domain, gran.value)
  } catch (e: any) {
    error.value = e?.message || '加载失败'
    data.value = null
  } finally {
    loading.value = false
    await nextTick()
    render()
  }
}

function render() {
  if (chart) { chart.destroy(); chart = null }
  if (!canvasEl.value || !data.value || !data.value.buckets.length) return
  const labels = data.value.buckets
  const datasets = topConcepts.value.map((c, i) => ({
    label: c.term,
    data: labels.map(b => c.buckets[b] ?? 0),
    backgroundColor: PALETTE[i % PALETTE.length],
    stack: 's',
    borderRadius: 3,
    maxBarThickness: 46,
  }))
  chart = new Chart(canvasEl.value, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10, boxHeight: 10, font: { size: 11 }, color: '#605e57' } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: '#787670', font: { size: 11 } } },
        y: { stacked: true, beginAtZero: true, ticks: { precision: 0, color: '#9b9a94', font: { size: 11 } }, grid: { color: '#f4f3f0' } },
      },
      onClick: (_e, els) => { if (els.length) selected.value = String(labels[els[0].index]) },
    },
  })
}

// 下钻：选中桶内各概念计数（降序），来自 concepts[].buckets[selected]
const inBucket = computed(() => {
  if (!selected.value || !data.value) return []
  const b = selected.value
  return data.value.concepts
    .map((c, i) => ({ term: c.term, n: c.buckets[b] ?? 0, i }))
    .filter(x => x.n > 0)
    .sort((a, b2) => b2.n - a.n)
})

function openConcept(term: string) {
  router.push(`/kb/${encodeURIComponent(props.domain)}/concepts/${encodeURIComponent(term)}`)
}

onMounted(load)
watch(() => props.domain, load)
watch(gran, load) // 粒度切换 → 重新向后端请求（后端按粒度聚合）
onBeforeUnmount(() => { if (chart) chart.destroy() })
</script>

<template>
  <div>
    <div class="tl-bar">
      <div class="seclabel"><BarChart3 :size="14" />概念出现趋势</div>
      <div class="tl-grans">
        <button v-for="g in GRANS" :key="g.k" class="chip" :class="{ on: gran === g.k }" @click="gran = g.k">{{ g.t }}</button>
      </div>
    </div>

    <div class="card pad" style="margin-top:12px">
      <div v-if="loading" style="color:var(--ink-500);font-size:13px;padding:24px 0;text-align:center">加载中…</div>
      <div v-else-if="error" style="padding:24px 0;text-align:center">
        <div style="font-size:13px;color:var(--ink-700)">{{ error }}</div>
        <button class="btn sm" style="margin-top:10px" @click="load">重试</button>
      </div>
      <div v-else-if="isEmpty" style="color:var(--ink-400);font-size:13px;padding:24px 0;text-align:center">该知识库暂无概念时间线数据</div>
      <div v-show="!loading && !error && !isEmpty" class="tl-canvas"><canvas ref="canvasEl"></canvas></div>
    </div>

    <!-- 下钻：选中桶内各概念计数 -->
    <div v-if="selected && inBucket.length" class="card pad" style="margin-top:14px">
      <div class="card-h">
        <span>{{ selected }} · 概念出现（{{ inBucket.length }}）</span>
        <button class="btn sm" style="margin-left:auto" @click="selected = null">收起</button>
      </div>
      <div class="list">
        <div v-for="o in inBucket" :key="o.term" class="row" style="cursor:pointer" @click="openConcept(o.term)">
          <span class="ci-dot" :style="{ background: PALETTE[o.i % PALETTE.length] }" />
          <div class="body">
            <div class="title">{{ o.term }}</div>
            <div class="meta"><span>本区间出现 {{ o.n }} 次</span></div>
          </div>
        </div>
      </div>
    </div>
    <div v-else-if="!isEmpty && !loading && !error" class="tl-hint">点击柱子查看该区间内各概念的出现次数</div>
  </div>
</template>

<style scoped>
.tl-bar { display: flex; align-items: center; gap: 12px; }
.tl-grans { margin-left: auto; display: flex; gap: 6px; }
.tl-canvas { height: 300px; position: relative; }
.tl-hint { margin-top: 10px; font-size: 12px; color: var(--ink-400); text-align: center; }
</style>
