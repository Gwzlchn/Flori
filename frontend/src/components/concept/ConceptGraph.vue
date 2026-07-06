<script setup lang="ts">
// 概念图谱(工作台「图谱」tab):力导向网络。数据来自 GET /api/domains/{d}/concept-graph,
// 返回形状见 api/services/kb.py 的 concept_graph,以后端为准。
// 节点是概念:大小随出现数增大,形状/色按 status 与 is_topic 区分。边是共现:粗细随权重(共享 job 数)增大。
// 点节点 → 右侧栏(定义/出现处/主题徽标 + 打开概念详情);悬停 → 高亮邻居/相连边;可搜索/筛选/隐藏孤立。
// vis-network 自带力学物理与 click/hover 事件,按需动态 import(首屏不拉图谱库)。
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useDomainStore } from '../../stores/domains'
import type { ConceptGraph, ConceptGraphNode } from '../../types'
import { Share2, Search, Lightbulb, FileText, ExternalLink, X, Maximize2, Minimize2 } from 'lucide-vue-next'
// vis-network 的类型仅用于标注,运行时实例懒加载;用宽松类型避免把库塞进首屏 chunk。
type VisNetwork = any

const props = defineProps<{ domain: string }>()
const router = useRouter()
const domainStore = useDomainStore()

const loading = ref(true)
const error = ref('')
const data = ref<ConceptGraph | null>(null)
const selected = ref<ConceptGraphNode | null>(null)

const containerEl = ref<HTMLElement | null>(null)
let network: VisNetwork | null = null
let DataSet: any = null            // vis-data DataSet 构造器(懒加载)
let nodesDS: any = null            // 节点 DataSet(过滤/高亮就地更新)

// 过滤控件。默认只画 accepted + 高频(出现≥2)节点(工单 09 P2 降噪),低频/候选靠开关。
const query = ref('')
const hideIsolated = ref(false)
const statusFilter = ref<'all' | 'accepted' | 'suggested'>('accepted')
const showLowFreq = ref(false)
const fullscreen = ref(false)   // 全屏看图(力导向图节点多,放大才看得清)

const ACCEPTED = '#2383e2'        // 已采纳=品牌蓝
const SUGGESTED = '#cb7b1f'       // 候选=琥珀
const TOPIC_RING = '#9065b0'      // 主题高亮描边
// 边配色按 kind:共现灰,类型化真边着色(prerequisite 另带箭头)。
const EDGE_COLORS: Record<string, string> = {
  cooccur: '#d6d5d0',
  related: '#8fb4dd',
  prerequisite: '#9065b0',
  is_a: '#4fa36e',
  part_of: '#c78f3d',
}
const EDGE_LABELS: Record<string, string> = {
  cooccur: '共现', related: '相关', prerequisite: '先修', is_a: '是一种', part_of: '组成',
}

const isEmpty = computed(
  () => !loading.value && !error.value && !(data.value?.nodes?.length),
)
const stats = computed(() => data.value?.stats
  ?? { node_count: 0, edge_count: 0, typed_edge_count: 0, isolated_count: 0 })

// 邻接表:供搜索定位 + 悬停高亮 + 侧栏「相连概念」。
const adjacency = computed(() => {
  const adj = new Map<string, Set<string>>()
  for (const e of data.value?.edges ?? []) {
    if (!adj.has(e.source)) adj.set(e.source, new Set())
    if (!adj.has(e.target)) adj.set(e.target, new Set())
    adj.get(e.source)!.add(e.target)
    adj.get(e.target)!.add(e.source)
  }
  return adj
})

// 当前过滤后应显示的节点 id 集合(搜索/隐藏孤立/状态筛选/低频过滤)。
const visibleNodeIds = computed(() => {
  const q = query.value.trim().toLowerCase()
  const adj = adjacency.value
  const ids = new Set<string>()
  for (const n of data.value?.nodes ?? []) {
    if (statusFilter.value !== 'all' && n.status !== statusFilter.value) continue
    if (!showLowFreq.value && n.occurrence_count < 2) continue
    if (hideIsolated.value && (adj.get(n.id)?.size ?? 0) === 0) continue
    if (q && !n.term.toLowerCase().includes(q) && !(n.zh_name || '').toLowerCase().includes(q)) continue
    ids.add(n.id)
  }
  return ids
})

// 过滤态计数,反馈给用户(空筛选结果时给提示)。
const visibleCount = computed(() => visibleNodeIds.value.size)

// 侧栏:选中节点的相连概念(按 term 升序)。
const neighbors = computed(() => {
  if (!selected.value) return []
  return [...(adjacency.value.get(selected.value.id) ?? [])].sort()
})

function nodeColor(n: ConceptGraphNode): string {
  return n.status === 'suggested' ? SUGGESTED : ACCEPTED
}

// 节点大小随出现数增大(sqrt 压缩,避免巨头吞屏);孤立/零出现给个基础尺寸。
function nodeSize(occ: number): number {
  return 10 + Math.sqrt(occ) * 6
}

function toVisNodes() {
  const adj = adjacency.value
  return (data.value?.nodes ?? []).map((n) => ({
    id: n.id,
    label: n.term,
    value: Math.max(n.occurrence_count, 1),
    size: nodeSize(n.occurrence_count),
    // 主题用方形 + 紫色描边突出;普通圆点。
    shape: n.is_topic ? 'square' : 'dot',
    color: {
      background: nodeColor(n),
      border: n.is_topic ? TOPIC_RING : nodeColor(n),
      highlight: { background: nodeColor(n), border: TOPIC_RING },
    },
    borderWidth: n.is_topic ? 3 : 1,
    title: `${n.term}（出现 ${n.occurrence_count} · 相连 ${adj.get(n.id)?.size ?? 0}）`,
    hidden: !visibleNodeIds.value.has(n.id),
    font: { size: 13, color: '#37352f' },
  }))
}

function toVisEdges() {
  const vis = visibleNodeIds.value
  return (data.value?.edges ?? []).map((e, i) => {
    const kind = e.kind ?? 'cooccur'
    return {
      id: i,
      from: e.source,
      to: e.target,
      value: e.weight,
      width: 1 + e.weight,
      title: `${EDGE_LABELS[kind] ?? kind} · 共现 ${e.weight} 次`,
      color: { color: EDGE_COLORS[kind] ?? EDGE_COLORS.cooccur, highlight: TOPIC_RING, opacity: 0.7 },
      // prerequisite 有语义方向(source 先修于 target),画箭头;其余无向。
      arrows: kind === 'prerequisite' ? { to: { enabled: true, scaleFactor: 0.6 } } : undefined,
      hidden: !(vis.has(e.source) && vis.has(e.target)),
    }
  })
}

async function load() {
  loading.value = true
  error.value = ''
  selected.value = null
  try {
    data.value = await domainStore.conceptGraph(props.domain)
  } catch (e: any) {
    error.value = e?.message || '加载失败'
    data.value = null
  } finally {
    loading.value = false
    await nextTick()
    await render()
  }
}

async function render() {
  if (network) { network.destroy(); network = null }
  nodesDS = null
  if (!containerEl.value || !data.value || !data.value.nodes?.length) return
  // 容器还没真实尺寸(tab 隐藏/v-show display:none)→ 暂不建图,等 ResizeObserver 在容器拿到尺寸后再 render。
  // 否则 vis-network 会建在 0×0 容器里、autoResize 之后也救不回(画布恒 0×0)。
  if (!containerEl.value.clientWidth || !containerEl.value.clientHeight) return
  // 懒加载图谱库(首屏不含);jsdom/无 canvas 测试环境优雅跳过——数据/侧栏/筛选逻辑仍可测。
  try {
    const vis: any = await import('vis-network/standalone')
    const Network = vis.Network
    DataSet = vis.DataSet
    if (!Network || !DataSet) return
    nodesDS = new DataSet(toVisNodes())
    const edgesDS = new DataSet(toVisEdges())
    network = new Network(
      containerEl.value,
      { nodes: nodesDS, edges: edgesDS },
      {
        autoResize: true,
        physics: {
          // 力导向:斥力 + 弹簧,稳定后停(省 CPU)。
          solver: 'forceAtlas2Based',
          forceAtlas2Based: { gravitationalConstant: -50, springLength: 110, springConstant: 0.08 },
          stabilization: { iterations: 150 },
        },
        interaction: { hover: true, tooltipDelay: 120, navigationButtons: false },
        nodes: { scaling: { min: 10, max: 46 } },
      },
    )
    network.on('click', (params: any) => {
      const id = params.nodes?.[0]
      if (id != null) selectNode(String(id))
      else selected.value = null
    })
    network.on('hoverNode', (params: any) => highlightNeighbors(String(params.node)))
    network.on('blurNode', () => clearHighlight())
    // 关键:深链 ?tab=graph 直开时容器要数百 ms 才稳定到真实尺寸,autoResize 初测取到 0→画布 0×0。
    // 轮询重试 setSize+redraw+fit,直到画布拿到非零尺寸(或 ~2s 放弃),覆盖深链初次加载的时序。
    ensureSized()
  } catch {
    network = null  // 库缺失/无 canvas → 不渲染,不抛(测试/SSR 友好)。
  }
}

function ensureSized(attempts = 0): void {
  const el = containerEl.value
  if (!network || !el) return
  const cv = el.querySelector('canvas') as HTMLCanvasElement | null
  if (cv && cv.width > 0 && cv.height > 0) return  // 已成功,停。
  if (el.clientWidth > 0 && el.clientHeight > 0) {
    try { network.setSize(`${el.clientWidth}px`, `${el.clientHeight}px`); network.redraw(); network.fit() } catch { /* noop */ }
  }
  if (attempts < 20 && typeof setTimeout !== 'undefined') {
    setTimeout(() => ensureSized(attempts + 1), 100)  // 重试 ~2s
  }
}

function selectNode(id: string) {
  selected.value = (data.value?.nodes ?? []).find((n) => n.id === id) ?? null
}

// 悬停高亮:相邻节点正常,其余淡化(就地改 DataSet 的不透明度)。
function highlightNeighbors(id: string) {
  if (!network || !nodesDS) return
  const adj = adjacency.value.get(id) ?? new Set()
  const updates = (data.value?.nodes ?? [])
    .filter((n) => visibleNodeIds.value.has(n.id))
    .map((n) => ({
      id: n.id,
      opacity: n.id === id || adj.has(n.id) ? 1 : 0.25,
    }))
  nodesDS.update(updates)
}

function clearHighlight() {
  if (!nodesDS) return
  const updates = (data.value?.nodes ?? [])
    .filter((n) => visibleNodeIds.value.has(n.id))
    .map((n) => ({ id: n.id, opacity: 1 }))
  nodesDS.update(updates)
}

// 过滤变化 → 就地更新 DataSet 的 hidden(不重建 network,保持布局)。
function applyFilter() {
  if (!network || !nodesDS) return
  nodesDS.update(toVisNodes().map((n) => ({ id: n.id, hidden: n.hidden })))
  const edges = network.body?.data?.edges
  if (edges) edges.update(toVisEdges().map((e) => ({ id: e.id, hidden: e.hidden })))
}

function openTermDetail(term: string) {
  // 概念详情页(/kb/{domain}/concepts/{term})列出该概念的全部出现处并各自链到对应内容/笔记。
  router.push(`/kb/${encodeURIComponent(props.domain)}/concepts/${encodeURIComponent(term)}`)
}

function focusTerm(term: string) {
  selectNode(term)
  if (network) {
    try { network.selectNodes([term]); network.focus(term, { scale: 1.1, animation: true }) } catch { /* noop */ }
  }
}

// vis-network 在 tab 隐藏(v-show display:none)时初始化会得到 0×0 画布;容器从隐藏变可见后
// autoResize 不一定触发。用 ResizeObserver:容器拿到真实尺寸时强制 setSize+redraw+fit,保证显隐切换都画。
let ro: ResizeObserver | null = null
function refit() {
  const el = containerEl.value
  if (!network || !el) return
  const w = el.clientWidth, h = el.clientHeight
  if (w > 0 && h > 0) {
    try { network.setSize(`${w}px`, `${h}px`); network.redraw(); network.fit() } catch { /* noop */ }
  }
}
onMounted(async () => {
  await load()
  if (typeof ResizeObserver !== 'undefined' && containerEl.value) {
    // 容器尺寸变化(tab 由隐藏变可见、首次拿到尺寸)时:还没建图就 render(此刻容器已有尺寸);已建则 refit。
    ro = new ResizeObserver(() => { if (!network) { void render() } else { refit() } })
    ro.observe(containerEl.value)
  }
})
watch(() => props.domain, load)
watch([query, hideIsolated, statusFilter, showLowFreq], applyFilter)
// 全屏切换后容器尺寸大变 → 下一帧重新按尺寸定一次(RO 也会兜底)。
watch(fullscreen, async () => { await nextTick(); ensureSized() })
onBeforeUnmount(() => { ro?.disconnect(); if (network) network.destroy() })

// 图谱节点的点击/悬停发生在 canvas(vis-network),无 DOM 节点可触发 —— 暴露这些入口供
// 测试驱动点节点开侧栏的流程,也便于父组件/集成代码以编程方式选中/聚焦概念。
defineExpose({ selectNode, focusTerm, selected })
</script>

<template>
  <div :class="{ 'cg-fs': fullscreen }">
    <div class="cg-bar">
      <div class="seclabel"><Share2 :size="14" />概念关联网络</div>
      <span class="cg-hint">滚轮缩放 · 拖拽平移 · 点节点看详情</span>
      <div class="cg-stats">
        <span class="badge b-brand">{{ stats.node_count }} 概念</span>
        <span class="badge b-mut">{{ stats.edge_count }} 关联</span>
        <span v-if="stats.typed_edge_count" class="badge b-ok">{{ stats.typed_edge_count }} 真边</span>
        <span v-if="stats.isolated_count" class="badge b-warn">{{ stats.isolated_count }} 孤立</span>
      </div>
    </div>

    <!-- 控件:搜索 / 隐藏孤立 / 状态筛选 -->
    <div class="cg-controls">
      <label class="cg-search">
        <Search :size="14" />
        <input v-model="query" class="cg-input" placeholder="搜索概念…" @keyup.enter="focusTerm(query.trim())" />
      </label>
      <button class="chip" :class="{ on: hideIsolated }" @click="hideIsolated = !hideIsolated">只看有关联</button>
      <button class="chip" :class="{ on: showLowFreq }" @click="showLowFreq = !showLowFreq">含低频</button>
      <button class="chip" :class="{ on: statusFilter === 'all' }" @click="statusFilter = 'all'">全部</button>
      <button class="chip" :class="{ on: statusFilter === 'accepted' }" @click="statusFilter = 'accepted'">已采纳</button>
      <button class="chip" :class="{ on: statusFilter === 'suggested' }" @click="statusFilter = 'suggested'">候选</button>
      <button class="chip cg-fsbtn" :class="{ on: fullscreen }" style="margin-left:auto" @click="fullscreen = !fullscreen">
        <Maximize2 v-if="!fullscreen" :size="13" /><Minimize2 v-else :size="13" />{{ fullscreen ? '退出全屏' : '全屏' }}
      </button>
    </div>

    <div class="cg-layout">
      <div class="card pad cg-canvas-card">
        <div v-if="loading" class="cg-state">加载中…</div>
        <div v-else-if="error" class="cg-state">
          <div style="color:var(--ink-700)">{{ error }}</div>
          <button class="btn sm" style="margin-top:10px" @click="load">重试</button>
        </div>
        <div v-else-if="isEmpty" class="cg-state cg-dim">该知识库暂无概念图谱数据</div>
        <div v-else-if="!visibleCount" class="cg-state cg-dim">没有匹配当前筛选的概念</div>
        <div v-show="!loading && !error && !isEmpty" ref="containerEl" class="cg-canvas"></div>
      </div>

      <!-- 侧栏:选中概念详情 -->
      <div v-if="selected" class="card pad cg-panel" data-test="panel">
        <div class="card-h">
          <span>{{ selected.term }}</span>
          <span v-if="selected.is_topic" class="badge b-brand" style="margin-left:6px"><Lightbulb :size="11" />主题</span>
          <span class="badge" :class="selected.status === 'accepted' ? 'b-ok' : 'b-warn'" style="margin-left:4px">
            {{ selected.status === 'accepted' ? '已采纳' : '候选' }}
          </span>
          <button class="iconbtn" style="margin-left:auto" @click="selected = null"><X :size="15" /></button>
        </div>

        <p v-if="selected.definition" class="cg-def">{{ selected.definition }}</p>
        <p v-else class="cg-def cg-dim">（暂无定义）</p>

        <div class="seclabel" style="margin-top:14px"><FileText :size="13" />出现处 · {{ selected.occurrence_count }}</div>
        <div v-if="neighbors.length" class="cg-related">
          <div class="seclabel" style="margin-top:12px"><Share2 :size="13" />相连概念 · {{ neighbors.length }}</div>
          <div class="cg-chips">
            <button v-for="t in neighbors" :key="t" class="chip" @click="focusTerm(t)">{{ t }}</button>
          </div>
        </div>
        <div v-else class="cg-dim" style="font-size:12px;margin-top:8px">该概念暂无关联(孤立节点)</div>

        <button class="btn pri sm" style="margin-top:16px" @click="openTermDetail(selected.term)">
          <ExternalLink :size="13" />打开概念详情
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cg-bar { display: flex; align-items: center; gap: 12px; }
.cg-hint { font-size: 12px; color: var(--ink-400); }
.cg-stats { margin-left: auto; display: flex; gap: 6px; }
.cg-controls { display: flex; align-items: center; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.cg-search { display: flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: var(--r-sm); padding: 4px 10px; color: var(--ink-500); background: var(--surface); }
.cg-input { border: none; outline: none; font-size: 13px; background: transparent; color: var(--ink-800); width: 150px; }
.cg-fsbtn { display: inline-flex; align-items: center; gap: 4px; }
.cg-layout { display: flex; gap: 14px; margin-top: 12px; align-items: flex-start; }
.cg-canvas-card { flex: 1; min-width: 0; }
/* 力导向图节点多,默认就给足高度(占大半视口);全屏时铺满。 */
.cg-canvas { height: calc(100vh - 300px); min-height: 480px; width: 100%; }

/* 全屏:整块固定铺满视口,画布吃满剩余高度。 */
.cg-fs { position: fixed; inset: 0; z-index: 1000; background: var(--surface, #fff); padding: 16px 20px; overflow: auto; }
.cg-fs .cg-canvas { height: calc(100vh - 150px); min-height: 0; }
.cg-fs .cg-panel { top: 16px; }
.cg-state { color: var(--ink-500); font-size: 13px; padding: 60px 0; text-align: center; }
.cg-dim { color: var(--ink-400); }
.cg-panel { width: 320px; flex: none; position: sticky; top: 12px; }
.cg-def { font-size: 13px; color: var(--ink-700); line-height: 1.6; margin-top: 10px; }
.cg-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
@media (max-width: 880px) {
  .cg-layout { flex-direction: column; }
  .cg-panel { width: 100%; position: static; }
}
</style>
