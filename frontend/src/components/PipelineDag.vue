<script setup lang="ts">
// 流水线分层拓扑 DAG:按 needs 最长路径分层 → 横向列(同列=可并行);依赖关系用 SVG 贝塞尔连线画出
// (源步右缘 → 目标步左缘),不再用「⟵合」文字。节点标池(cpu/ai/io/gpu);AI 步附 provider + 开销。
// statusByKey(每个 job 视图)给定时点按状态着色,否则按池。纯 CSS + SVG,无图布局库;过宽横滚。
import { computed, ref, onMounted, onUnmounted, nextTick, watch } from 'vue'

interface Step { key: string; label: string | null; pool: string | null; needs: string[] }
interface UsageInfo { provider: string; cost: number; equiv: boolean }
const props = defineProps<{
  steps: Step[]
  statusByKey?: Record<string, string>
  selected?: string
  usageByStep?: Record<string, UsageInfo>
}>()
const emit = defineEmits<{ (e: 'select', key: string): void }>()

const byKey = computed<Record<string, Step>>(() => {
  const m: Record<string, Step> = {}
  for (const s of props.steps) m[s.key] = s
  return m
})

// 最长路径分层:layer(s)=0(无依赖)或 1+max(layer(依赖步))。带环保护。
const layers = computed<Step[][]>(() => {
  const lay: Record<string, number> = {}
  const visit = (key: string, stack: Set<string>): number => {
    if (key in lay) return lay[key]
    if (stack.has(key)) return 0
    stack.add(key)
    const needs = (byKey.value[key]?.needs || []).filter(n => n in byKey.value)
    const l = needs.length ? 1 + Math.max(...needs.map(n => visit(n, stack))) : 0
    stack.delete(key)
    lay[key] = l
    return l
  }
  for (const s of props.steps) visit(s.key, new Set())
  const max = props.steps.reduce((m, s) => Math.max(m, lay[s.key] ?? 0), 0)
  const cols: Step[][] = Array.from({ length: max + 1 }, () => [])
  for (const s of props.steps) cols[lay[s.key] ?? 0].push(s)
  return cols
})

function dotCls(s: Step): string {
  if (props.statusByKey) return 'st-' + (props.statusByKey[s.key] || 'waiting')
  return 'pl-' + (s.pool || 'io')
}
const fmtCost = (v: number) => `$${(v ?? 0).toFixed(4)}`
// hover 提示:步骤 · 池 · (AI 步)provider 开销。节点本体保持窄,详情挪到 tooltip。
function nodeTitle(s: Step): string {
  let t = `${s.label || s.key} · ${s.pool || 'io'} 池`
  const u = props.usageByStep?.[s.key]
  if (u) t += ` · ${u.provider} ${fmtCost(u.cost)}${u.equiv ? '（等价）' : ''}`
  return t
}

// ── SVG 依赖连线:渲染后量取各节点位置,源右缘→目标左缘画贝塞尔 ──
const container = ref<HTMLElement | null>(null)
const edges = ref<{ d: string; sel: boolean }[]>([])
const svgW = ref(0)
const svgH = ref(0)

function measure() {
  const cont = container.value
  if (!cont) return
  const cr = cont.getBoundingClientRect()
  const pos: Record<string, { x: number; y: number; w: number; h: number }> = {}
  cont.querySelectorAll<HTMLElement>('.dag-node[data-key]').forEach(el => {
    const r = el.getBoundingClientRect()
    pos[el.dataset.key as string] = {
      x: r.left - cr.left + cont.scrollLeft,
      y: r.top - cr.top + cont.scrollTop,
      w: r.width, h: r.height,
    }
  })
  svgW.value = cont.scrollWidth
  svgH.value = cont.scrollHeight
  const out: { d: string; sel: boolean }[] = []
  for (const s of props.steps) {
    const t = pos[s.key]
    if (!t) continue
    for (const n of (s.needs || [])) {
      const so = pos[n]
      if (!so) continue
      const sx = so.x + so.w, sy = so.y + so.h / 2
      const tx = t.x, ty = t.y + t.h / 2
      const dx = Math.max(14, (tx - sx) / 2)
      out.push({
        d: `M ${sx} ${sy} C ${sx + dx} ${sy} ${tx - dx} ${ty} ${tx} ${ty}`,
        sel: props.selected != null && (props.selected === s.key || props.selected === n),
      })
    }
  }
  edges.value = out
}

let ro: ResizeObserver | null = null
onMounted(() => {
  nextTick(measure)
  // 容器尺寸变化(含从 tab 隐藏→显示、窗口缩放)重量边。jsdom/SSR 无此 API 时跳过。
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => measure())
    if (container.value) ro.observe(container.value)
  }
})
onUnmounted(() => ro?.disconnect())
watch(
  [() => props.steps, () => props.statusByKey, () => props.selected, () => props.usageByStep],
  () => nextTick(measure),
  { deep: true },
)
</script>

<template>
  <div ref="container" class="dag">
    <svg class="dag-edges" :width="svgW" :height="svgH" :viewBox="`0 0 ${svgW} ${svgH}`">
      <path v-for="(e, i) in edges" :key="i" :d="e.d" :class="{ sel: e.sel }" />
    </svg>
    <div v-for="(col, ci) in layers" :key="ci" class="dag-col">
      <div
        v-for="s in col" :key="s.key" class="dag-node" :data-key="s.key"
        :class="['pe-' + (s.pool || 'io'), { 'is-sel': s.key === selected }]"
        :title="nodeTitle(s)" @click="emit('select', s.key)"
      >
        <span class="dag-dot" :class="dotCls(s)"></span>
        <span class="dag-text">
          <span class="dag-label">{{ s.label || s.key }}</span>
          <span v-if="usageByStep && usageByStep[s.key]" class="dag-cost">{{ usageByStep[s.key].equiv ? '≈' : '' }}{{ fmtCost(usageByStep[s.key].cost) }}</span>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dag { position: relative; display: flex; align-items: stretch; gap: 18px; overflow-x: auto; padding: 4px 0 8px; }
.dag-edges { position: absolute; top: 0; left: 0; pointer-events: none; z-index: 0; overflow: visible; }
.dag-edges path { fill: none; stroke: var(--ink-300); stroke-width: 1.4; }
.dag-edges path.sel { stroke: var(--brand-400); stroke-width: 2; }
.dag-col { position: relative; z-index: 1; display: flex; flex-direction: column; justify-content: center; gap: 10px; flex: none; }
.dag-node {
  display: flex; align-items: center; gap: 6px; padding: 5px 9px; position: relative;
  border: 1px solid var(--line); border-left-width: 3px; border-left-color: var(--ink-200);
  border-radius: var(--r-sm); background: var(--surface);
  white-space: nowrap; cursor: pointer; transition: border-color .12s, background .12s;
}
.dag-node:hover { border-color: var(--ink-300); }
.dag-node.is-sel { border-color: var(--brand-500); background: var(--brand-50); }
/* 池=左边框色(不占宽):io/cpu 灰、ai 蓝、gpu 琥珀 */
.pe-io { border-left-color: var(--ink-200); }
.pe-cpu { border-left-color: var(--ink-400); }
.pe-ai { border-left-color: var(--info); }
.pe-gpu { border-left-color: var(--warn); }
.dag-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.dag-text { display: flex; flex-direction: column; line-height: 1.2; gap: 1px; }
.dag-label { font-size: 12px; color: var(--ink-800); }
.dag-cost { font-size: 10px; color: var(--ink-500); }
/* 定义视图(无 statusByKey)按池着点 */
.pl-io { background: var(--ink-300); }
.pl-cpu { background: var(--ink-500); }
.pl-ai { background: var(--info); }
.pl-gpu { background: var(--warn); }
/* job 视图按步骤状态着点 */
.st-done { background: var(--ok); }
.st-running { background: var(--run); }
.st-ready { background: var(--warn); }
.st-failed { background: var(--bad); }
.st-skipped { background: var(--ink-300); }
.st-waiting { background: var(--ink-200); }
</style>
