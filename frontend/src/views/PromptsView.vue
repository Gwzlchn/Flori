<script setup lang="ts">
// AI 工作流:展示三类内容流水线,Document 可编辑 common 或具体体裁覆盖。
import { ref, onMounted, reactive } from 'vue'
import { useApi } from '../composables/useApi'
import PipelineDag from '../components/PipelineDag.vue'
import PromptEditor from '../components/settings/PromptEditor.vue'
import { FileCode2, Lock } from 'lucide-vue-next'
import { documentKindLabel } from '../utils/contentType'

interface PStep {
  key: string
  label: string | null
  pool: string | null
  needs: string[]
  is_ai?: boolean
  has_override?: boolean
  prompt_locked?: boolean
}
interface Pipeline {
  name: string
  label?: string
  document_kinds?: string[]
  steps: PStep[]
}

const api = useApi()
const pipelines = ref<Pipeline[]>([])
const loading = ref(true)
const error = ref('')
const editing = ref<{
  pipeline: string
  step: string
  label: string
  documentKind: string | null
} | null>(null)
const selectedKinds = reactive<Record<string, string>>({})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await api.get<{ pipelines?: Pipeline[] }>('/api/pipelines')
    pipelines.value = Array.isArray(r) ? (r as Pipeline[]) : (r?.pipelines ?? [])
  } catch (e: any) {
    error.value = e?.message || '读取流水线失败'
  } finally {
    loading.value = false
  }
}
onMounted(load)

function aiSteps(p: Pipeline): PStep[] {
  return p.steps.filter((s) => s.is_ai || s.pool === 'ai')
}

function openStep(p: Pipeline, key: string) {
  const s = p.steps.find((x) => x.key === key)
  if (!s || !(s.is_ai || s.pool === 'ai')) return // 非 AI 步不可编辑
  editing.value = {
    pipeline: p.name,
    step: key,
    label: s.label || key,
    documentKind: p.name === 'document' ? (selectedKinds[p.name] || null) : null,
  }
}

function onSaved() {
  editing.value = null
  load() // 刷新已有覆盖的圆点角标
}
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom:6px"><FileCode2 :size="18" />AI 工作流</div>
    <p style="font-size:13px;color:var(--ink-600);margin-bottom:18px">
      Video、Document、Audio 流水线的完整步骤。点蓝色 AI 步编辑全局/领域覆盖;
      Document 可先选全部体裁或某一体裁。<b>●</b> = 已有覆盖;锁标 = 协议 prompt,只读。
      覆盖存数据库,下个任务派发时注入该步。
    </p>

    <div v-if="loading" style="color:var(--ink-500);font-size:13px">加载中…</div>
    <div v-else-if="error" style="color:var(--danger-600,#dc2626);font-size:13px">
      {{ error }} <button class="btn sm" @click="load">重试</button>
    </div>

    <template v-else>
      <div v-for="p in pipelines" :key="p.name" class="card pad" style="margin-bottom:18px">
        <div class="seclabel" style="margin-bottom:12px">{{ p.label || p.name }}</div>
        <div v-if="p.name === 'document'" class="field" style="max-width:260px;margin-bottom:12px">
          <label>Document Prompt 体裁</label>
          <select v-model="selectedKinds[p.name]" class="input" data-test="document-kind-select">
            <option value="">全部体裁(common)</option>
            <option v-for="kind in p.document_kinds || []" :key="kind" :value="kind">
              {{ documentKindLabel(kind) || kind }}
            </option>
          </select>
        </div>
        <PipelineDag :steps="p.steps" @select="openStep(p, $event)" />
        <!-- AI 步可编辑入口(可靠的编辑面;DAG 上方为流程白盒)-->
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px">
          <button v-for="s in aiSteps(p)" :key="s.key" class="badge b-info"
            style="cursor:pointer;border:none" @click="openStep(p, s.key)">
            <Lock v-if="s.prompt_locked" :size="11" style="margin-right:4px" data-test="step-lock" />
            <span v-else-if="s.has_override" style="margin-right:4px">●</span>{{ s.label || s.key }}
          </button>
        </div>
      </div>
    </template>

    <PromptEditor v-if="editing" :pipeline="editing.pipeline" :step="editing.step" :label="editing.label"
      :document-kind="editing.documentKind"
      @close="editing = null" @saved="onSaved" @changed="load" />
  </section>
</template>
