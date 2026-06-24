<script setup lang="ts">
// 流水线分层拓扑 DAG:按 needs 做最长路径分层 → 横向列(左→右),同层步骤竖排=可并行;
// 步骤左侧池色点(io/cpu/ai/gpu);扇入步(>1 依赖)标「⟵合 X·Y」。纯 CSS、无图布局库;过宽横滚。
import { computed } from 'vue'

interface Step { key: string; label: string | null; pool: string | null; needs: string[] }
// statusByKey 给定时(每个 job 视图):点按步骤状态着色;否则(流水线定义视图)按池着色。
const props = defineProps<{ steps: Step[]; statusByKey?: Record<string, string> }>()

const byKey = computed<Record<string, Step>>(() => {
  const m: Record<string, Step> = {}
  for (const s of props.steps) m[s.key] = s
  return m
})

// 最长路径分层:layer(s)=0(无依赖)或 1+max(layer(需要的步))。带环保护。
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

// 扇入注记:>1 依赖才标(单依赖=普通流转,列间箭头已示意)。
function mergeNote(s: Step): string {
  const ns = (s.needs || []).filter(n => byKey.value[n])
  return ns.length > 1 ? '合 ' + ns.map(n => byKey.value[n].label || n).join('·') : ''
}
// 点色:job 视图按状态(done/running/...),定义视图按池(io/cpu/ai/gpu)。
function dotCls(s: Step): string {
  if (props.statusByKey) return 'st-' + (props.statusByKey[s.key] || 'waiting')
  return 'pl-' + (s.pool || 'io')
}
</script>

<template>
  <div class="dag">
    <template v-for="(col, ci) in layers" :key="ci">
      <div class="dag-col">
        <div v-for="s in col" :key="s.key" class="dag-node" :title="`${s.label || s.key} · ${s.pool || ''} 池`">
          <span class="dag-dot" :class="dotCls(s)"></span>
          <span class="dag-text">
            <span class="dag-label">{{ s.label || s.key }}</span>
            <span v-if="mergeNote(s)" class="dag-merge">⟵{{ mergeNote(s) }}</span>
          </span>
        </div>
      </div>
      <div v-if="ci < layers.length - 1" class="dag-arrow">›</div>
    </template>
  </div>
</template>

<style scoped>
.dag { display: flex; align-items: stretch; gap: 3px; overflow-x: auto; padding: 2px 0 6px; }
.dag-col { display: flex; flex-direction: column; justify-content: center; gap: 7px; flex: none; }
.dag-node {
  display: flex; align-items: center; gap: 6px; padding: 5px 9px;
  border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface);
  white-space: nowrap;
}
.dag-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.dag-text { display: flex; flex-direction: column; line-height: 1.25; }
.dag-label { font-size: 12px; color: var(--ink-800); }
.dag-merge { font-size: 10px; color: var(--ink-400); }
/* 池色:复用既有 token,无新增颜色 */
.pl-io { background: var(--ink-300); }
.pl-cpu { background: var(--ink-500); }
.pl-ai { background: var(--info); }
.pl-gpu { background: var(--warn); }
/* job 视图按步骤状态着色(复用语义色,无新增) */
.st-done { background: var(--ok); }
.st-running { background: var(--run); }
.st-ready { background: var(--warn); }
.st-failed { background: var(--bad); }
.st-skipped { background: var(--ink-300); }
.st-waiting { background: var(--ink-200); }
.dag-arrow { display: flex; align-items: center; color: var(--ink-300); font-size: 13px; flex: none; }
</style>
