<script setup lang="ts">
// 通联拓扑树:数据驱动(workers 按 remote_addr 归父 + link_traffic),非硬编码盒子 → 加任何 worker/边缘自动进树。
// 结构分三层:NAS 是中心 hub;第二层是直连的本地 worker 和走隧道的 ECS 边缘;第三层是经网关接入的远程 worker。
// 分层横向 + SVG 贝塞尔连线(同 PipelineDag 范式)。节点可点击,选中 emit;详情(趋势)由父组件出面板。
import { computed, ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import type { Worker, LinkTraffic } from '../../types'
import { fmtBytes } from '../../utils/format'
import { Database, Server, Cpu } from 'lucide-vue-next'

interface Node {
  id: string
  kind: 'nas' | 'ecs' | 'worker'
  label: string
  sub: string
  layer: number
  parent: string | null
  pull: number
  push: number
  pool: string | null
}
const props = defineProps<{ workers: Worker[]; link: LinkTraffic | null; selected?: string }>()
const emit = defineEmits<{ (e: 'select', id: string): void }>()

// 由数据构树
const nodes = computed<Node[]>(() => {
  const out: Node[] = []
  const mk = (p: Partial<Node> & { id: string; kind: Node['kind']; label: string; layer: number; parent: string | null }): Node =>
    ({ sub: '', pull: 0, push: 0, pool: null, ...p })
  out.push(mk({ id: 'nas', kind: 'nas', label: 'NAS', sub: 'api·redis·minio·scheduler', layer: 0, parent: null }))
  const local = props.workers.filter(w => !w.remote_addr)
  const remote = props.workers.filter(w => w.remote_addr)
  const hasEdge = !!props.link || remote.length > 0
  if (hasEdge) {
    out.push(mk({
      id: 'ecs', kind: 'ecs', label: 'ECS 边缘', sub: '公网入口·网关·隧道', layer: 1, parent: 'nas',
      pull: props.link?.tunnel.rx ?? 0, push: props.link?.tunnel.tx ?? 0,
    }))
  }
  for (const w of local) {
    out.push(mk({
      id: 'w:' + w.id, kind: 'worker', label: w.hostname || w.id, sub: '直连', layer: 1, parent: 'nas',
      pull: w.traffic?.pull ?? 0, push: w.traffic?.push ?? 0, pool: (w.pools || [])[0] ?? null,
    }))
  }
  for (const w of remote) {
    out.push(mk({
      id: 'w:' + w.id, kind: 'worker', label: w.hostname || w.id, sub: w.remote_addr || '网关',
      layer: 2, parent: hasEdge ? 'ecs' : 'nas',
      pull: w.traffic?.pull ?? 0, push: w.traffic?.push ?? 0, pool: (w.pools || [])[0] ?? null,
    }))
  }
  return out
})
const layers = computed<Node[][]>(() => {
  const max = nodes.value.reduce((m, n) => Math.max(m, n.layer), 0)
  const cols: Node[][] = Array.from({ length: max + 1 }, () => [])
  for (const n of nodes.value) cols[n.layer].push(n)
  return cols
})
const iconFor = (k: Node['kind']) => (k === 'nas' ? Database : k === 'ecs' ? Server : Cpu)
const tunnelUp = computed(() => props.link?.tunnel.up ?? false)

// SVG 连线:渲染后量节点位置,父右缘→子左缘画贝塞尔;选中节点相关边高亮
const container = ref<HTMLElement | null>(null)
const edges = ref<{ d: string; sel: boolean }[]>([])
const svgW = ref(0)
const svgH = ref(0)
function measure() {
  const cont = container.value
  if (!cont) return
  const cr = cont.getBoundingClientRect()
  const pos: Record<string, { x: number; y: number; w: number; h: number }> = {}
  cont.querySelectorAll<HTMLElement>('.tp-node[data-id]').forEach(el => {
    const r = el.getBoundingClientRect()
    pos[el.dataset.id as string] = { x: r.left - cr.left + cont.scrollLeft, y: r.top - cr.top + cont.scrollTop, w: r.width, h: r.height }
  })
  svgW.value = cont.scrollWidth
  svgH.value = cont.scrollHeight
  const out: { d: string; sel: boolean }[] = []
  for (const n of nodes.value) {
    if (!n.parent) continue
    const c = pos[n.id]
    const p = pos[n.parent]
    if (!c || !p) continue
    const sx = p.x + p.w, sy = p.y + p.h / 2
    const tx = c.x, ty = c.y + c.h / 2
    const dx = Math.max(14, (tx - sx) / 2)
    out.push({
      d: `M ${sx} ${sy} C ${sx + dx} ${sy} ${tx - dx} ${ty} ${tx} ${ty}`,
      sel: props.selected != null && (props.selected === n.id || props.selected === n.parent),
    })
  }
  out.sort((a, b) => Number(a.sel) - Number(b.sel))
  edges.value = out
}
let ro: ResizeObserver | null = null
onMounted(() => {
  nextTick(measure)
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => measure())
    if (container.value) ro.observe(container.value)
  }
})
onUnmounted(() => ro?.disconnect())
watch([() => props.workers, () => props.link, () => props.selected], () => nextTick(measure), { deep: true })
</script>

<template>
  <div ref="container" class="tp">
    <svg class="tp-edges" :width="svgW" :height="svgH" :viewBox="`0 0 ${svgW} ${svgH}`">
      <path v-for="(e, i) in edges" :key="i" :d="e.d" :class="{ sel: e.sel }" />
    </svg>
    <div v-for="(col, ci) in layers" :key="ci" class="tp-col">
      <div
        v-for="n in col" :key="n.id" class="tp-node" :data-id="n.id"
        :class="[n.kind === 'ecs' ? 'is-ecs' : '', n.kind === 'nas' ? 'is-nas' : '', { 'is-sel': n.id === selected }]"
        @click="emit('select', n.id)"
      >
        <div class="tp-h"><component :is="iconFor(n.kind)" :size="13" />{{ n.label }}</div>
        <div v-if="n.kind === 'worker'" class="tp-b">↓{{ fmtBytes(n.pull) }} ↑{{ fmtBytes(n.push) }}</div>
        <div v-else-if="n.kind === 'ecs'" class="tp-b">
          <span :class="tunnelUp ? 'tp-up' : 'tp-down'">●</span> 隧道 ↓{{ fmtBytes(n.pull) }} ↑{{ fmtBytes(n.push) }}
        </div>
        <div v-else class="tp-sub">{{ n.sub }}</div>
        <div v-if="n.kind === 'worker'" class="tp-sub">{{ n.sub }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tp { position: relative; display: flex; align-items: stretch; gap: 30px; overflow-x: auto; padding: 4px 0 6px; }
.tp-edges { position: absolute; top: 0; left: 0; pointer-events: none; z-index: 0; overflow: visible; }
.tp-edges path { fill: none; stroke: var(--ink-300); stroke-width: 1.4; }
.tp-edges path.sel { stroke: var(--brand-500); stroke-width: 2.5; }
.tp-col { position: relative; z-index: 1; display: flex; flex-direction: column; justify-content: center; gap: 9px; flex: none; }
.tp-node {
  min-width: 130px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface);
  padding: 7px 10px; cursor: pointer; transition: background .12s, border-color .12s;
}
.tp-node.is-sel { border-color: var(--brand-500); background: var(--brand-50); }
.tp-node.is-ecs { background: var(--brand-50); border-color: var(--brand-200); }
.tp-node.is-nas { border-left: 3px solid var(--ink-500); }
.tp-h { display: flex; align-items: center; gap: 5px; font-size: 12.5px; font-weight: 600; color: var(--ink-800); }
.tp-b { font-size: 11px; color: var(--ink-600); margin-top: 3px; font-variant-numeric: tabular-nums; }
.tp-sub { font-size: 10.5px; color: var(--ink-400); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 150px; }
.tp-up { color: var(--ok); }
.tp-down { color: var(--bad); }
</style>
