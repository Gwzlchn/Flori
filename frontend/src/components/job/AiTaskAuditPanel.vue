<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '../../composables/useApi'
import type { AiTaskLogResponse, AiTaskLogCall } from '../../types'
import { Check, X, Copy } from 'lucide-vue-next'

// 只读:展示某独立 AI task(/ask、/digest)的【完整 AI 审计】(每次 claude 调用一条),
// 镜像 ai_task_logs(GET /api/ai-tasks/{task_id}/log),与 DAG 步的 AiLogPanel 同风格。
const props = defineProps<{ taskId: string }>()
const api = useApi()

const calls = ref<AiTaskLogCall[]>([])
const loading = ref(false)
const err = ref('')

async function load() {
  loading.value = true
  err.value = ''
  try {
    const r = await api.get<AiTaskLogResponse>(`/api/ai-tasks/${encodeURIComponent(props.taskId)}/log`)
    calls.value = r.calls || []
  } catch (e: any) {
    err.value = e?.message || '加载失败'
    calls.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)

const num = (v?: number) => (v ?? 0).toLocaleString()
const fmtCost = (v?: number) => `$${(v ?? 0).toFixed(4)}`
function copy(t?: string | null) { if (t != null) navigator.clipboard?.writeText(t) }
function pretty(v: any): string { try { return JSON.stringify(v, null, 2) } catch { return String(v) } }
function userText(c: AiTaskLogCall): string {
  const msgs = c.record?.prompt?.messages || []
  return msgs.map((m: any) => m?.content || '').join('\n')
}
</script>

<template>
  <div>
    <div v-if="loading" class="text-xs text-gray-400">加载中…</div>
    <div v-else-if="err" class="text-xs text-gray-400">{{ err }}</div>
    <div v-else-if="!calls.length" class="text-xs text-gray-400">暂无审计记录</div>
    <div v-else class="space-y-2">
      <div
        v-for="(c, i) in calls" :key="i" class="border rounded-lg p-2.5"
        :class="c.ok === false ? 'border-red-200 bg-red-50/40' : 'border-gray-200 bg-gray-50/40'"
      >
        <!-- 头:状态 + provider/model/tier + 成本 -->
        <div class="flex items-center gap-2 flex-wrap text-xs mb-1.5">
          <component :is="c.ok === false ? X : Check" :size="13"
                     :class="c.ok === false ? 'text-red-500' : 'text-green-500'" />
          <span class="font-mono text-gray-800">{{ c.provider || '—' }}</span>
          <span class="text-gray-500">{{ c.model }}</span>
          <span v-if="c.record?.routing?.tier_used" class="px-1 rounded bg-gray-200 text-gray-600">{{ c.record.routing.tier_used }}</span>
          <span class="ml-auto text-gray-800 font-medium">{{ fmtCost(c.record?.usage?.cost_usd) }}</span>
        </div>
        <!-- 用量 -->
        <div class="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-500 mb-1.5">
          <span>入 {{ num(c.record?.usage?.input_tokens) }}</span>
          <span>出 {{ num(c.record?.usage?.output_tokens) }}</span>
          <span>读缓存 {{ num(c.record?.usage?.cache_read_input_tokens) }}</span>
          <span v-if="c.record?.usage?.num_turns">轮数 {{ c.record.usage.num_turns }}</span>
          <span v-if="c.record?.usage?.duration_sec != null">耗时 {{ Number(c.record.usage.duration_sec).toFixed(1) }}s</span>
        </div>
        <!-- 尝试链(降级/失败时有看头) -->
        <div v-if="(c.record?.routing?.attempts?.length || 0) > 1 || c.ok === false" class="text-xs text-gray-500 mb-1.5">
          尝试链:
          <span v-for="(a, ai) in c.record?.routing?.attempts || []" :key="ai">
            <span :class="a.ok ? 'text-green-600' : 'text-red-600'">{{ a.tier }}/{{ a.provider }} {{ a.ok ? '✓' : '✗' }}</span>
            <span v-if="ai < (c.record?.routing?.attempts?.length || 0) - 1"> · </span>
          </span>
        </div>
        <p v-if="c.ok === false && c.error" class="text-xs text-red-600 bg-red-50 rounded p-1.5 mb-1.5 break-all">✗ {{ c.error }}</p>

        <!-- 折叠:System / User(实际发出)/ 输出 / raw -->
        <details class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none flex items-center gap-1.5">
            System
            <button @click.prevent.stop="copy(c.record?.prompt?.system)" class="text-blue-500 hover:text-blue-700"><Copy :size="11" /></button>
          </summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ c.record?.prompt?.system || '(无)' }}</pre>
        </details>
        <details class="mb-1" open>
          <summary class="text-xs text-gray-600 cursor-pointer select-none flex items-center gap-1.5">
            User(实际发出)
            <button @click.prevent.stop="copy(userText(c))" class="text-blue-500 hover:text-blue-700"><Copy :size="11" /></button>
          </summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ userText(c) }}</pre>
        </details>
        <details class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none flex items-center gap-1.5">
            输出
            <button @click.prevent.stop="copy(c.record?.output)" class="text-blue-500 hover:text-blue-700"><Copy :size="11" /></button>
          </summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ c.record?.output || '(无)' }}</pre>
        </details>
        <details v-if="c.record?.raw" class="mb-1">
          <summary class="text-xs text-gray-600 cursor-pointer select-none">原始 raw</summary>
          <pre class="text-xs mt-1 bg-white border border-gray-100 rounded p-2 whitespace-pre-wrap break-all max-h-72 overflow-auto">{{ pretty(c.record.raw) }}</pre>
        </details>

        <div class="text-[11px] text-gray-400 mt-1 flex flex-wrap gap-x-3">
          <span v-if="c.record?.usage?.session_id">session {{ c.record.usage.session_id }}</span>
          <span v-if="c.exec_id">exec {{ c.exec_id }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
