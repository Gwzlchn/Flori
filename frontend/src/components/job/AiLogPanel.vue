<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { useApi } from '../../composables/useApi'
import { fmtDuration } from '../../utils/datetime'
import type { AiLogCall, AiLogsResponse } from '../../types'
import { Check, X, Copy } from 'lucide-vue-next'

// 只读:展示该 job 某 AI 步当时的完整 AI 审计日志(每次 LLM 调用一条)。改 prompt 去设置页。
const props = defineProps<{ jobId: string; step: string }>()
const api = useApi()

const calls = ref<AiLogCall[]>([])
const loading = ref(false)
const err = ref('')

async function load() {
  if (!props.step) { calls.value = []; return }
  loading.value = true; err.value = ''
  try {
    const r = await api.get<AiLogsResponse>(
      `/api/jobs/${props.jobId}/ai-logs?step=${encodeURIComponent(props.step)}`)
    calls.value = (r.steps || []).find(s => s.step === props.step)?.calls || []
  } catch (e: any) {
    err.value = e?.message || '加载失败'; calls.value = []
  } finally { loading.value = false }
}

onMounted(load)
watch(() => props.step, load)

const fmtCost = (v?: number) => `$${(v ?? 0).toFixed(4)}`
const num = (v?: number) => (v ?? 0).toLocaleString()
function copy(text?: string | null) { if (text != null) navigator.clipboard?.writeText(text) }
function pretty(v: any): string { try { return JSON.stringify(v, null, 2) } catch { return String(v) } }
</script>

<template>
  <div>
    <div v-if="loading" class="text-xs text-gray-400">加载中…</div>
    <div v-else-if="err" class="text-xs text-gray-400">{{ err }}</div>
    <div v-else-if="!calls.length" class="text-xs text-gray-400">
      该步骤暂无 AI 日志(此 job 跑该步时尚未启用审计记录)
    </div>
    <div v-else class="space-y-2">
      <div
        v-for="(c, i) in calls" :key="i" class="border rounded-lg p-2.5"
        :class="c.ok === false ? 'border-red-200 bg-red-50/40' : 'border-gray-200 bg-gray-50/40'"
      >
        <!-- 头:调用序 + 状态 + provider/model/tier + 成本 -->
        <div class="flex items-center gap-2 flex-wrap text-xs mb-1.5">
          <span class="font-semibold text-gray-700">调用 {{ (c.call_index ?? i) + 1 }}/{{ calls.length }}</span>
          <component :is="c.ok === false ? X : Check" :size="13"
                     :class="c.ok === false ? 'text-red-500' : 'text-green-500'" />
          <span class="font-mono text-gray-800">{{ c.routing?.provider || '—' }}</span>
          <span class="text-gray-500">{{ c.routing?.model }}</span>
          <span v-if="c.routing?.tier_used" class="px-1 rounded bg-gray-200 text-gray-600">{{ c.routing.tier_used }}</span>
          <span class="ml-auto text-gray-800 font-medium">
            {{ fmtCost(c.cost?.cost_usd) }}<span v-if="c.cost?.basis === 'cli-equiv'" class="text-gray-400">（等价）</span>
          </span>
        </div>
        <!-- 用量 + 延迟 -->
        <div class="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-500 mb-1.5">
          <span>入 {{ num(c.usage?.input_tokens) }}</span>
          <span>出 {{ num(c.usage?.output_tokens) }}</span>
          <span>读缓存 {{ num(c.usage?.cache_read_input_tokens) }}</span>
          <span>写缓存 {{ num(c.usage?.cache_creation_input_tokens) }}</span>
          <span v-if="c.output?.num_turns">轮数 {{ c.output.num_turns }}</span>
          <span v-if="c.latency?.ttft_ms != null">ttft {{ Math.round(c.latency.ttft_ms) }}ms</span>
          <span v-if="c.latency?.api_ms != null">api {{ Math.round(c.latency.api_ms) }}ms</span>
          <span v-if="c.latency?.duration_total_sec != null">耗时 {{ fmtDuration(c.latency.duration_total_sec, { decimalSeconds: true }) }}</span>
          <span v-if="c.call_meta?.images_count">帧图 {{ c.call_meta.images_count }}</span>
        </div>
        <!-- 尝试链(降级/失败时有看头) -->
        <div v-if="(c.routing?.attempts?.length || 0) > 1 || c.ok === false" class="text-xs text-gray-500 mb-1.5">
          尝试链:
          <span v-for="(a, ai) in c.routing?.attempts || []" :key="ai">
            <span :class="a.ok ? 'text-green-600' : 'text-red-600'">{{ a.tier }}/{{ a.provider }} {{ a.ok ? '✓' : '✗' }}</span>
            <span v-if="ai < (c.routing?.attempts?.length || 0) - 1"> · </span>
          </span>
        </div>
        <!-- 失败错误 -->
        <p v-if="c.ok === false && c.error" class="text-xs text-red-600 bg-red-50 rounded p-1.5 mb-1.5 break-all">✗ {{ c.error }}</p>

        <!-- 折叠字段:System / User(渲染后) / 输出 / 解析 / raw -->
        <details class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none flex items-center gap-1.5">
            System
            <button @click.prevent.stop="copy(c.prompt?.rendered?.system)" class="text-blue-500 hover:text-blue-700"><Copy :size="11" /></button>
            <span class="text-gray-400">{{ c.prompt?.template?.source === 'override' ? '(覆盖)' : '(默认:无)' }}</span>
          </summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ c.prompt?.rendered?.system || '(无)' }}</pre>
        </details>
        <details class="mb-1" open>
          <summary class="text-xs text-gray-600 cursor-pointer select-none flex items-center gap-1.5">
            User(渲染后实际发出)
            <button @click.prevent.stop="copy(c.prompt?.rendered?.user)" class="text-blue-500 hover:text-blue-700"><Copy :size="11" /></button>
          </summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ c.prompt?.rendered?.user }}</pre>
        </details>
        <details class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none flex items-center gap-1.5">
            输出
            <button @click.prevent.stop="copy(c.output?.content)" class="text-blue-500 hover:text-blue-700"><Copy :size="11" /></button>
            <span v-if="c.output?.finish_reason" class="text-gray-400">{{ c.output.finish_reason }}</span>
          </summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ c.output?.content || '(无)' }}</pre>
        </details>
        <details v-if="c.output_processed" class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none">解析 / 抽取(output_processed)</summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ pretty(c.output_processed) }}</pre>
        </details>
        <details v-if="c.raw" class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none">原始 raw</summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ pretty(c.raw) }}</pre>
        </details>

        <!-- 溯源行 -->
        <div class="text-[11px] text-gray-400 mt-1 flex flex-wrap gap-x-3">
          <span v-if="c.session_id">session {{ c.session_id }}</span>
          <span v-if="c.flori?.version">flori {{ c.flori.version }}</span>
          <span v-if="c.flori?.git_commit">commit {{ String(c.flori.git_commit).slice(0, 8) }}</span>
          <span v-if="c.env?.worker_id">worker {{ c.env.worker_id }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
