<script setup lang="ts">
// Prompt 白盒: 把四条流水线的全部步骤画成 DAG(看清流程=白盒),
// 在 AI 步上编辑该步 prompt 覆盖。圆点角标表示该步已有覆盖。数据来自 GET /api/pipelines(含 is_ai/has_override)。
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import PipelineDag from '../components/PipelineDag.vue'
import PromptEditor from '../components/settings/PromptEditor.vue'
import { FileCode2, ChevronLeft } from 'lucide-vue-next'

interface PStep {
  key: string
  label: string | null
  pool: string | null
  needs: string[]
  is_ai?: boolean
  has_override?: boolean
}
interface Pipeline {
  name: string
  steps: PStep[]
}

const api = useApi()
const pipelines = ref<Pipeline[]>([])
const loading = ref(true)
const error = ref('')
const editing = ref<{ pipeline: string; step: string; label: string } | null>(null)

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
  editing.value = { pipeline: p.name, step: key, label: s.label || key }
}

function onSaved() {
  editing.value = null
  load() // 刷新已有覆盖的圆点角标
}
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom:6px"><FileCode2 :size="18" />Prompt(白盒)</div>
    <div class="row" style="cursor:pointer;margin-bottom:14px" @click="$router.push('/settings')">
      <ChevronLeft :size="15" /><span style="font-size:13px;color:var(--ink-500)">返回设置</span>
    </div>
    <p style="font-size:13px;color:var(--ink-600);margin-bottom:18px">
      四条流水线的完整步骤(白盒)。点蓝色 AI 步编辑其 prompt 覆盖(全局或按领域);<b>●</b> = 已有覆盖。
      覆盖存数据库,下个任务派发时注入该步。
    </p>

    <div v-if="loading" style="color:var(--ink-500);font-size:13px">加载中…</div>
    <div v-else-if="error" style="color:var(--danger-600,#dc2626);font-size:13px">
      {{ error }} <button class="btn sm" @click="load">重试</button>
    </div>

    <template v-else>
      <div v-for="p in pipelines" :key="p.name" class="card pad" style="margin-bottom:18px">
        <div class="seclabel" style="margin-bottom:12px">{{ p.name }}</div>
        <PipelineDag :steps="p.steps" @select="openStep(p, $event)" />
        <!-- AI 步可编辑入口(可靠的编辑面;DAG 上方为流程白盒)-->
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px">
          <button v-for="s in aiSteps(p)" :key="s.key" class="badge b-info"
            style="cursor:pointer;border:none" @click="openStep(p, s.key)">
            <span v-if="s.has_override" style="margin-right:4px">●</span>{{ s.label || s.key }}
          </button>
        </div>
      </div>
    </template>

    <PromptEditor v-if="editing" :pipeline="editing.pipeline" :step="editing.step" :label="editing.label"
      @close="editing = null" @saved="onSaved" @changed="load" />
  </section>
</template>
