<script setup lang="ts">
// 概念时间线（原型 #domain 时间线 tab）：Chart.js 堆叠柱状图，粒度 月/季/年，
// 点柱下钻到该桶的概念出现明细。真实数据由 glossary 的 occurrences ⋈ 工作台 recent_jobs 的日期拼出；
// 数据不足时退化为演示数据并标注。【需后端新增】专用聚合端点 GET /api/domains/{domain}/concept-timeline 可使其精确高效。
import { ref, computed, onMounted, watch, nextTick, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { BarChart3, Info } from 'lucide-vue-next'
import {
  Chart, BarController, BarElement, CategoryScale, LinearScale, Tooltip, Legend,
} from 'chart.js'

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip, Legend)

const props = defineProps<{ domain: string }>()
const router = useRouter()
const api = useApi()

type Gran = 'month' | 'quarter' | 'year'
const gran = ref<Gran>('quarter')
const loading = ref(true)
const isDemo = ref(false)
const selected = ref<string | null>(null)
const canvasEl = ref<HTMLCanvasElement | null>(null)
let chart: Chart | null = null

interface Occ { term: string; jobId: string; title: string; date: Date }
const occ = ref<Occ[]>([])

const PALETTE = ['#2383e2', '#9065b0', '#448361', '#d9730d', '#c0392b', '#2e9b9b', '#cb7b1f', '#6b6f76']

const GRANS: { k: Gran; t: string }[] = [
  { k: 'month', t: '按月' }, { k: 'quarter', t: '按季' }, { k: 'year', t: '按年' },
]

async function loadData() {
  loading.value = true
  selected.value = null
  try {
    const [terms, ws] = await Promise.all([
      api.get<any[]>(`/api/glossary?domain=${encodeURIComponent(props.domain)}`).catch(() => [] as any[]),
      api.get<any>(`/api/domains/${encodeURIComponent(props.domain)}`).catch(() => ({} as any)),
    ])
    const jobDate: Record<string, { d: Date; t: string }> = {}
    for (const j of (ws?.recent_jobs || [])) {
      jobDate[j.job_id] = { d: new Date(j.created_at), t: j.title || j.job_id }
    }
    const list: Occ[] = []
    for (const term of (terms || [])) {
      for (const o of (term.occurrences || [])) {
        const jd = jobDate[o.job_id]
        if (jd && !isNaN(jd.d.getTime())) list.push({ term: term.term, jobId: o.job_id, title: jd.t, date: jd.d })
      }
    }
    if (list.length >= 4) { occ.value = list; isDemo.value = false }
    else { occ.value = demoData(); isDemo.value = true }
  } catch {
    occ.value = demoData(); isDemo.value = true
  } finally {
    loading.value = false
    await nextTick()
    render()
  }
}

function demoData(): Occ[] {
  const terms = ['注意力机制', 'Transformer', '优化器', 'CNN', '正则化', '梯度下降']
  const out: Occ[] = []
  const now = new Date()
  for (let i = 0; i < 64; i++) {
    const d = new Date(now)
    d.setMonth(d.getMonth() - Math.floor(Math.random() * 24))
    const t = terms[Math.floor(Math.random() * terms.length)]
    out.push({ term: t, jobId: 'demo_' + i, title: t + ' · 示例内容', date: d })
  }
  return out
}

function bucketKey(d: Date, g: Gran): string {
  const y = d.getFullYear()
  if (g === 'year') return `${y}`
  if (g === 'quarter') return `${y} Q${Math.floor(d.getMonth() / 3) + 1}`
  return `${y}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

const topTerms = computed(() => {
  const by: Record<string, number> = {}
  for (const o of occ.value) by[o.term] = (by[o.term] || 0) + 1
  return Object.keys(by).sort((a, b) => by[b] - by[a]).slice(0, 6)
})

const buckets = computed(() =>
  Array.from(new Set(occ.value.map(o => bucketKey(o.date, gran.value)))).sort(),
)

function render() {
  if (!canvasEl.value) return
  const labels = buckets.value
  const datasets = topTerms.value.map((t, i) => ({
    label: t,
    data: labels.map(k => occ.value.filter(o => o.term === t && bucketKey(o.date, gran.value) === k).length),
    backgroundColor: PALETTE[i % PALETTE.length],
    stack: 's',
    borderRadius: 3,
    maxBarThickness: 46,
  }))
  if (chart) chart.destroy()
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

const occInBucket = computed(() =>
  selected.value
    ? occ.value
        .filter(o => bucketKey(o.date, gran.value) === selected.value)
        .sort((a, b) => b.date.getTime() - a.date.getTime())
    : [],
)

function openOcc(o: Occ) {
  if (!isDemo.value && !o.jobId.startsWith('demo_')) router.push(`/content/${o.jobId}`)
}

onMounted(loadData)
watch(() => props.domain, loadData)
watch(gran, () => { selected.value = null; render() })
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

    <div v-if="isDemo" class="tl-demo"><Info :size="13" />演示数据（真实概念时间线聚合端点待后端接入）</div>

    <div class="card pad" style="margin-top:12px">
      <div v-if="loading" style="color:var(--ink-500);font-size:13px;padding:24px 0;text-align:center">加载中…</div>
      <div v-show="!loading" class="tl-canvas"><canvas ref="canvasEl"></canvas></div>
    </div>

    <!-- 下钻：选中桶的出现明细 -->
    <div v-if="selected" class="card pad" style="margin-top:14px">
      <div class="card-h">
        <span>{{ selected }} · 出现明细（{{ occInBucket.length }}）</span>
        <button class="btn sm" style="margin-left:auto" @click="selected = null">收起</button>
      </div>
      <div class="list">
        <div v-for="(o, i) in occInBucket" :key="i" class="row" :style="{ cursor: isDemo ? 'default' : 'pointer' }" @click="openOcc(o)">
          <span class="ci-dot" :style="{ background: PALETTE[topTerms.indexOf(o.term) % PALETTE.length] || 'var(--ink-300)' }" />
          <div class="body">
            <div class="title">{{ o.term }}</div>
            <div class="meta"><span>{{ o.title }}</span></div>
          </div>
        </div>
        <div v-if="!occInBucket.length" style="color:var(--ink-400);font-size:13px;padding:8px">该区间无出现记录</div>
      </div>
    </div>
    <div v-else class="tl-hint">点击柱子查看该区间的概念出现明细</div>
  </div>
</template>

<style scoped>
.tl-bar { display: flex; align-items: center; gap: 12px; }
.tl-grans { margin-left: auto; display: flex; gap: 6px; }
.tl-canvas { height: 300px; position: relative; }
.tl-demo { display: flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 12px; color: var(--warn); }
.tl-hint { margin-top: 10px; font-size: 12px; color: var(--ink-400); text-align: center; }
</style>
