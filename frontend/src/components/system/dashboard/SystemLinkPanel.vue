<script setup lang="ts">
import { Network } from 'lucide-vue-next'
import LinkTopologyTree from '../LinkTopologyTree.vue'
import { fmtBytes } from '../../../utils/format'
import type { LinkTraffic, Worker } from '../../../types'

interface LinkDetail {
  title: string; cum: string; rate: string; linkDesc: string; wid: string; dl: string; ul: string; span: string
  down: number[]; up: number[]; peak: number; tunnels: Array<{ name: string; fwd: string; rx: number; tx: number }>
}
defineProps<{ workers: Worker[]; link: LinkTraffic | null; selected: string | null; detail: LinkDetail | null; points: (values: number[], max: number) => string }>()
defineEmits<{ select: [node: string]; openWorker: [workerId: string] }>()
</script>

<template>
  <div class="seclabel" style="margin-bottom:12px"><Network :size="14" />通联 · 链路流量</div>
  <div class="card pad" style="margin-bottom:24px">
    <LinkTopologyTree :workers="workers" :link="link" :selected="selected" @select="$emit('select', $event)" />
    <div v-if="detail" class="tp-detail">
      <div class="tp-detail-h">{{ detail.title }}<span class="tp-detail-cum">{{ detail.cum }}<template v-if="detail.rate"> · 速率 {{ detail.rate }}</template></span><span v-if="detail.linkDesc" class="tp-detail-tag">{{ detail.linkDesc }}</span><span v-if="detail.wid" class="tp-detail-link" @click="$emit('openWorker', detail.wid)">worker 详情 →</span></div>
      <div v-if="detail.down.length > 1" class="tp-chart"><svg viewBox="0 0 100 28" preserveAspectRatio="none"><polyline :points="points(detail.down, detail.peak)" class="ch-d" /><polyline :points="points(detail.up, detail.peak)" class="ch-u" /></svg><span class="tp-chart-leg"><i class="ch-dd" />{{ detail.dl }} <i class="ch-ud" />{{ detail.ul }}<span class="dim" style="margin-left:6px">近 {{ detail.span }}</span></span></div>
      <div v-else class="tp-detail-empty">趋势数据累积中…(上报器每 20s 采样;需边缘在线)</div>
      <div v-if="detail.tunnels.length" class="tp-tunnels"><span v-for="tunnel in detail.tunnels" :key="tunnel.name" class="tp-tn" :title="tunnel.fwd"><b>{{ tunnel.name }}</b> ↓{{ fmtBytes(tunnel.rx) }} ↑{{ fmtBytes(tunnel.tx) }}</span></div>
    </div>
    <div v-else class="tp-detail tp-detail-empty">选择节点查看链路流量</div>
  </div>
</template>

<style scoped>
.tp-detail { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--line-soft); }
.tp-detail-h { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; font-size: 13px; font-weight: 600; color: var(--ink-800); }
.tp-detail-cum { font-weight: 400; font-size: 12px; color: var(--ink-500); font-variant-numeric: tabular-nums; }
.tp-detail-tag { font-weight: 400; font-size: 11px; color: var(--ink-400); }
.tp-detail-link { font-weight: 400; font-size: 11.5px; color: var(--brand-600); cursor: pointer; margin-left: auto; }
.tp-detail-link:hover { text-decoration: underline; }
.tp-detail-empty { font-size: 11.5px; color: var(--ink-400); margin-top: 8px; }
.tp-chart { display: flex; align-items: center; gap: 10px; margin-top: 9px; }
.tp-chart svg { width: 220px; height: 30px; flex: none; }
.tp-chart polyline { fill: none; stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.ch-d { stroke: var(--brand-500); }.ch-u { stroke: var(--warn); }
.tp-chart-leg { font-size: 10.5px; color: var(--ink-400); display: inline-flex; align-items: center; gap: 5px; }
.tp-chart-leg i { width: 9px; height: 2.5px; border-radius: 1px; display: inline-block; }.ch-dd { background: var(--brand-500); }.ch-ud { background: var(--warn); }
.tp-tunnels { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; font-size: 11.5px; color: var(--ink-500); }.tp-tn { font-variant-numeric: tabular-nums; }.tp-tn b { color: var(--ink-700); font-weight: 600; }
</style>
