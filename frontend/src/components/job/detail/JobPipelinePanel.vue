<script setup lang="ts">
import { computed } from 'vue'
import { Coins, FileText, GitBranch, RotateCcw } from 'lucide-vue-next'
import PipelineDag from '../../PipelineDag.vue'
import StepWorkbench from '../StepWorkbench.vue'
import type { JobPartInfo, StepInfo } from '../../../types'

interface DagStep { key: string; label: string | null; pool: string | null; needs: string[]; scope?: 'job' | 'part' }
interface PromptRow { step: string; label: string; used: string; current: string | null; stale: boolean }
interface TotalAi { cost: number; equiv: boolean; calls: number }

const props = defineProps<{
  jobId: string
  steps: StepInfo[]
  parts?: JobPartInfo[]
  dagSteps: DagStep[]
  statusByKey: Record<string, string>
  selectedStep: string
  selectedPartId?: string | null
  usageByStep: Record<string, { provider: string; cost: number; equiv: boolean }>
  totalAi: TotalAi
  jobStatus: string
  rebuilding: boolean
  updateAvailable: boolean
  promptRows: PromptRow[]
}>()

defineEmits<{
  selectStep: [step: string]
  selectPartStep: [partId: string, step: string]
  retry: []
  rerun: []
  rebuild: []
  rerunPart: [partId: string, fromStep: string]
}>()

const fmtCost = (value: number) => `$${(value ?? 0).toFixed(2)}`
const firstFailedStep = (part: JobPartInfo) => (
  part.steps.find(step => step.status === 'failed')?.name || part.steps[0]?.name || ''
)
const selectedPart = computed(() => (
  props.parts?.find(part => part.part_id === props.selectedPartId) || null
))
const workbenchSteps = computed(() => selectedPart.value?.steps || props.steps)
</script>

<template>
  <div v-if="parts?.length" class="card pad parts-card" data-test="job-parts">
    <div class="card-h"><GitBranch :size="15" />各 Part 处理 <span class="part-count">{{ parts.length }} 个</span></div>
    <div class="parts-grid">
      <section v-for="part in parts" :key="part.part_id" class="part-card" :data-test="`job-part-${part.part_index}`">
        <div class="part-head">
          <span class="part-number">P{{ String(part.part_index).padStart(2, '0') }}</span>
          <strong>{{ part.title || `第 ${part.part_index} 部分` }}</strong>
          <span class="part-status" :class="`is-${part.status}`">{{ part.status }}</span>
          <span class="part-progress">{{ part.progress_pct }}%</span>
          <button v-if="part.status === 'failed' && firstFailedStep(part)" class="part-retry" title="只重试这个Part" @click="$emit('rerunPart', part.part_id, firstFailedStep(part))"><RotateCcw :size="12" /></button>
        </div>
        <a v-if="part.url" class="part-url" :href="part.url" target="_blank" rel="noopener">{{ part.url }}</a>
        <div class="part-steps">
          <button v-for="step in part.steps" :key="step.name" :class="[`is-${step.status}`, { selected: selectedPartId === part.part_id && selectedStep === step.name }]" :title="step.error || step.label || step.name" @click="$emit('selectPartStep', part.part_id, step.name)">
            <i />{{ step.label || step.name }}
          </button>
        </div>
      </section>
    </div>
    <div class="merge-note">全部 Part 完成后，系统只运行一次全场汇总、笔记、概念与评审。</div>
  </div>
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
  <div v-if="jobStatus === 'failed' || updateAvailable" class="job-actions" data-test="job-actions">
    <button v-if="jobStatus === 'failed'" class="btn pri" @click="$emit('retry')"><RotateCcw :size="14" />从失败处继续</button>
    <button v-if="updateAvailable" class="btn" data-test="pipeline-update" :disabled="rebuilding" @click="$emit('rebuild')"><GitBranch :size="14" />{{ rebuilding ? '更新中…' : '更新到最新流程' }}</button>
    <span v-if="updateAvailable" class="dim update-desc">流程或 Prompt 已更新,将创建新版本并保留当前版本</span>
  </div>
  <div v-if="promptRows.length" class="card pad" style="margin-bottom:12px">
    <div class="card-h"><FileText :size="15" />本任务 Prompt 版本</div>
    <div v-for="row in promptRows" :key="row.step" class="pver-row">
      <span class="pver-step">{{ row.label }}</span>
      <span class="pver-tag" :class="row.stale ? 'pv-stale' : 'pv-ok'">本任务 prompt v{{ row.used }}<template v-if="row.stale"> · 当前 {{ row.current == null ? '默认(无覆盖)' : 'v' + row.current }}</template></span>
      <span style="flex:1" />
    </div>
    <div class="dim" style="font-size:12px;margin-top:6px">「本任务」是该步派发时使用的 Prompt 快照;有更新时使用上方版本升级入口,当前版本不会被覆盖。</div>
  </div>
  <StepWorkbench :job-id="jobId" :steps="workbenchSteps" :selected-step="selectedStep" :selected-part-id="selectedPartId" :can-rerun="jobStatus === 'done' || jobStatus === 'failed'" @rerun="$emit('rerun')" />
</template>

<style scoped>
.parts-card { margin-bottom: 14px; }
.part-count { color: var(--ink-400); font-size: 11px; font-weight: 500; }
.parts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 9px; }
.part-card { min-width: 0; padding: 10px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); background: var(--raised); }
.part-head { display: flex; align-items: center; gap: 7px; min-width: 0; }
.part-head strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-800); font-size: 12px; }
.part-number { flex: none; color: var(--brand-700); font: 700 11px/1 ui-monospace, monospace; }
.part-status { margin-left: auto; flex: none; padding: 2px 6px; border-radius: 999px; background: var(--mut-bg); color: var(--ink-500); font-size: 10px; }
.part-status.is-done { background: var(--ok-bg, #ecfdf5); color: var(--ok, #059669); }
.part-status.is-running { background: var(--info-bg, #eff6ff); color: var(--run, #2563eb); }
.part-status.is-failed { background: var(--bad-bg, #fef2f2); color: var(--bad, #dc2626); }
.part-progress { flex: none; color: var(--ink-400); font-size: 10px; }
.part-retry { flex: none; padding: 3px; border-radius: 5px; color: var(--bad); }
.part-retry:hover { background: var(--bad-bg, #fef2f2); }
.part-url { display: block; margin-top: 6px; overflow: hidden; color: var(--ink-500); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.part-steps { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.part-steps button { display: inline-flex; align-items: center; gap: 4px; padding: 2px 4px; border-radius: 4px; color: var(--ink-500); font-size: 10px; }
.part-steps button:hover, .part-steps button.selected { background: var(--brand-50); color: var(--brand-700); }
.part-steps i { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-200); }
.part-steps .is-done i { background: var(--ok); }
.part-steps .is-running i { background: var(--run); }
.part-steps .is-ready i { background: var(--warn); }
.part-steps .is-failed i { background: var(--bad); }
.merge-note { margin-top: 9px; color: var(--ink-500); font-size: 11px; }
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
.job-actions { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; white-space: nowrap; }
.job-actions .btn { flex: none; }
.update-desc { min-width: 0; overflow: hidden; text-overflow: ellipsis; font-size: 12px; }
.pver-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.pver-row + .pver-row { border-top: 1px solid var(--line-soft); }
.pver-step { font-size: 13px; font-weight: 600; color: var(--ink-700); }
.pver-tag { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.pv-ok { color: var(--ink-500, #6b7280); background: var(--mut-bg, #f1f5f9); }
.pv-stale { color: var(--warn-700, #b45309); background: var(--warn-bg, #fffbeb); }
</style>
