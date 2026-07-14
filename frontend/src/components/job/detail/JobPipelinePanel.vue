<script setup lang="ts">
import { Coins, FileText, GitBranch, Play, RotateCcw } from 'lucide-vue-next'
import PipelineDag from '../../PipelineDag.vue'
import StepWorkbench from '../StepWorkbench.vue'
import type { StepInfo } from '../../../types'

interface DagStep { key: string; label: string | null; pool: string | null; needs: string[] }
interface PromptRow { step: string; label: string; used: string; current: string | null; stale: boolean }
interface TotalAi { cost: number; equiv: boolean; calls: number }

defineProps<{
  jobId: string
  steps: StepInfo[]
  dagSteps: DagStep[]
  statusByKey: Record<string, string>
  selectedStep: string
  selectedStepLabel: string
  usageByStep: Record<string, { provider: string; cost: number; equiv: boolean }>
  totalAi: TotalAi
  jobStatus: string
  rebuilding: boolean
  promptRows: PromptRow[]
}>()

defineEmits<{
  selectStep: [step: string]
  retry: []
  rerun: []
  rebuild: []
  rerunPrompt: [step: string]
}>()

const fmtCost = (value: number) => `$${(value ?? 0).toFixed(4)}`
</script>

<template>
  <div v-if="dagSteps.length" class="card pad" style="margin-bottom:14px;padding:13px 15px">
    <div style="font-size:13px;font-weight:600;color:var(--ink-800);display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <GitBranch :size="14" />流程依赖图（DAG）
      <span class="legend">
        <span><i style="background:var(--ok)" />完成</span><span><i style="background:var(--run)" />运行中</span>
        <span><i style="background:var(--bad)" />失败</span><span><i style="background:var(--ink-300)" />跳过/待运行</span>
        <span style="color:var(--ink-300)">|</span><span><i class="pool ai" />AI</span><span><i class="pool cpu" />CPU</span><span><i class="pool gpu" />GPU</span>
      </span>
      <span v-if="totalAi.calls" class="total-ai"><Coins :size="13" />AI 总开销 {{ fmtCost(totalAi.cost) }}<span v-if="totalAi.equiv">（等价）</span></span>
    </div>
    <PipelineDag :steps="dagSteps" :status-by-key="statusByKey" :selected="selectedStep" :usage-by-step="usageByStep" style="margin-top:10px" @select="$emit('selectStep', $event)" />
  </div>
  <div v-if="jobStatus === 'done' || jobStatus === 'failed'" style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
    <button v-if="jobStatus === 'failed'" class="btn pri" @click="$emit('retry')"><RotateCcw :size="14" />重试</button>
    <button v-if="selectedStep" class="btn" @click="$emit('rerun')"><Play :size="14" />从「{{ selectedStepLabel }}」重跑</button>
    <button class="btn" :disabled="rebuilding" title="基于当前 pipeline/prompt 重建为新版本(只重跑变化步骤及下游,旧版本保留对比)" @click="$emit('rebuild')">
      <GitBranch :size="14" />{{ rebuilding ? '重建中…' : '重建新版本' }}
    </button>
  </div>
  <div v-if="promptRows.length" class="card pad" style="margin-bottom:12px">
    <div class="card-h"><FileText :size="15" />本任务 Prompt 版本</div>
    <div v-for="row in promptRows" :key="row.step" class="pver-row">
      <span class="pver-step">{{ row.label }}</span>
      <span class="pver-tag" :class="row.stale ? 'pv-stale' : 'pv-ok'">本任务 prompt v{{ row.used }}<template v-if="row.stale"> · 当前 {{ row.current == null ? '默认(无覆盖)' : 'v' + row.current }}</template></span>
      <span style="flex:1" /><button v-if="row.stale" class="btn sm" @click="$emit('rerunPrompt', row.step)"><Play :size="13" />重跑该步</button>
    </div>
    <div class="dim" style="font-size:12px;margin-top:6px">「本任务」= 该步派发时用的 prompt 版本快照;不一致表示之后改过 prompt,可「重跑该步」按当前版本重出(连同其下游)。</div>
  </div>
  <StepWorkbench :job-id="jobId" :steps="steps" :selected-step="selectedStep" />
</template>

<style scoped>
.legend { font-weight: 400; font-size: 11px; color: var(--ink-500); display: inline-flex; gap: 9px; margin-left: 4px; }
.legend span { display: inline-flex; align-items: center; gap: 4px; }
.legend i { width: 7px; height: 7px; border-radius: 50%; }
.legend i.pool { width: 3px; height: 11px; border-radius: 1px; }
.legend i.ai { background: var(--info); }
.legend i.cpu { background: var(--ink-400); }
.legend i.gpu { background: var(--warn); }
.total-ai { margin-left: auto; font-weight: 600; color: var(--ink-700); display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.total-ai svg { color: var(--ink-400); }
.total-ai span { font-weight: 400; color: var(--ink-400); font-size: 11px; }
.pver-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.pver-row + .pver-row { border-top: 1px solid var(--line-soft); }
.pver-step { font-size: 13px; font-weight: 600; color: var(--ink-700); }
.pver-tag { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.pv-ok { color: var(--ink-500, #6b7280); background: var(--mut-bg, #f1f5f9); }
.pv-stale { color: var(--warn-700, #b45309); background: var(--warn-bg, #fffbeb); }
</style>
