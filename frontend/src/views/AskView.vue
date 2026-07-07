<script setup lang="ts">
// 问知识库(跨源综合问答,异步):提问 → POST /api/ask 返 202 {task_id, sources} → 立刻显示来源,
// 再轮询 GET /api/ai-tasks/{task_id}/result 取答案(claude 在 ai-worker 跑)。答案受控 markdown
// (含 [来源N] 引用 + 共识/分歧),MarkdownViewer 安全渲染;答案出来后可展开「AI 审计」(白盒)。
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useDomainStore } from '../stores/domains'
import MarkdownViewer from '../components/notes/MarkdownViewer.vue'
import AiTaskAuditPanel from '../components/job/AiTaskAuditPanel.vue'
import { contentTypeIcon } from '../utils/contentType'
import type { AskResponse, AiTaskResult } from '../types'
import { Sparkles, MessageCircleQuestion, Send, ScrollText } from 'lucide-vue-next'

const api = useApi()
const router = useRouter()
const domainStore = useDomainStore()

const question = ref('')
const domain = ref('')          // '' = 全库
const loading = ref(false)       // POST /api/ask 在途(检索中)
const error = ref('')            // POST 硬失败
const submitted = ref<AskResponse | null>(null)  // 202 回的 {task_id, sources, retrieved_count}
const answering = ref(false)     // 轮询 AI 答案中
const answerMd = ref<string | null>(null)        // 取到的答案 markdown
const answerErr = ref('')        // 轮询失败/超时
const showAudit = ref(false)
let pollToken = ''               // 当前轮询的 task_id;新提问换 token 取消旧轮询

const POLL_MS = 1500
const POLL_TIMEOUT_MS = 90000
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

onMounted(() => { if (!domainStore.domains.length) domainStore.fetchAll() })

async function ask() {
  const q = question.value.trim()
  if (!q || loading.value) return
  loading.value = true
  error.value = ''
  submitted.value = null
  answerMd.value = null
  answerErr.value = ''
  answering.value = false
  showAudit.value = false
  pollToken = ''
  try {
    const resp = await api.post<AskResponse>('/api/ask', {
      question: q,
      domain: domain.value.trim() || null,
    })
    submitted.value = resp
    if (resp.task_id) {
      // 命中:答案异步,开始轮询(来源已随 202 回,模板立刻显示)。
      answering.value = true
      pollToken = resp.task_id
      pollResult(resp.task_id)
    } else {
      // 无命中短路 / 投递失败:answer_markdown 已是最终消息。
      answerMd.value = resp.answer_markdown ?? ''
    }
  } catch (e: any) {
    error.value = e?.message || '提问失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

async function pollResult(taskId: string) {
  const start = Date.now()
  while (pollToken === taskId) {
    let r: AiTaskResult
    try {
      r = await api.get<AiTaskResult>(`/api/ai-tasks/${encodeURIComponent(taskId)}/result`)
    } catch {
      r = { status: 'pending', task_id: taskId }  // 网络抖动:当 pending 续轮询到超时
    }
    if (pollToken !== taskId) return               // 被新提问取消
    if (r.status === 'done') {
      answerMd.value = r.answer_markdown ?? r.content ?? ''
      answering.value = false
      return
    }
    if (r.status === 'error') {
      answerErr.value = r.error || 'AI 调用失败。'
      answering.value = false
      return
    }
    if (Date.now() - start > POLL_TIMEOUT_MS) {
      answerErr.value = 'AI 暂不可用（超时），请稍后重试。'
      answering.value = false
      return
    }
    await sleep(POLL_MS)
  }
}

// Cmd/Ctrl + Enter 提交(textarea 内回车换行)。
function onKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault()
    ask()
  }
}

function openSource(jobId: string) {
  router.push(`/content/${encodeURIComponent(jobId)}`)
}

function evidenceLabel(s: AskResponse['sources'][number]) {
  const ev = s.evidence
  if (!ev) return ''
  if (ev.section) return ev.section
  if (ev.timestamp_sec !== null && ev.timestamp_sec !== undefined) return `${Math.round(ev.timestamp_sec)}s`
  if (ev.page !== null && ev.page !== undefined) return `p.${ev.page}`
  if (ev.chunk_index !== null && ev.chunk_index !== undefined) return `片段 ${ev.chunk_index + 1}`
  return ''
}
</script>

<template>
  <section class="page">
    <div class="h1" style="margin-bottom:6px"><Sparkles :size="18" />问知识库</div>
    <div class="lead" style="margin-bottom:16px">
      用一句话提问，系统会跨语料检索相关笔记，综合出带引用的答案并标注共识 / 分歧。
    </div>

    <!-- 提问框 -->
    <div class="card pad" style="display:flex;flex-direction:column;gap:10px">
      <textarea
        v-model="question"
        class="input"
        rows="3"
        style="resize:vertical;font-size:14px;line-height:1.6"
        placeholder="例如：反向传播和梯度下降有什么区别？各来源是否有分歧？（⌘/Ctrl + Enter 提交）"
        @keydown="onKeydown"
      />
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <select v-model="domain" class="input" style="max-width:200px">
          <option value="">全部知识库</option>
          <option v-for="d in domainStore.domains" :key="d.domain" :value="d.domain">
            {{ d.display_name || d.domain }}
          </option>
        </select>
        <button
          class="btn-submit"
          :disabled="!question.trim() || loading"
          style="margin-left:auto"
          @click="ask"
        >
          <Send :size="16" /><span>{{ loading ? '检索中…' : '提问' }}</span>
        </button>
      </div>
    </div>

    <!-- 检索中(POST 在途) -->
    <div v-if="loading" class="card pad" style="margin-top:18px;color:var(--ink-500);font-size:13px">
      正在检索相关笔记…
    </div>

    <!-- POST 硬失败 -->
    <div v-else-if="error" class="card pad"
      style="margin-top:18px;display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:32px 18px">
      <div style="font-size:13.5px;color:var(--ink-700)">{{ error }}</div>
      <button class="btn" @click="ask">重试</button>
    </div>

    <!-- 结果态 -->
    <template v-else-if="submitted">
      <!-- 无命中(task_id=null 且 0 篇) -->
      <div v-if="!submitted.task_id && submitted.retrieved_count === 0" class="card pad"
        style="margin-top:18px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:40px 18px">
        <MessageCircleQuestion :size="40" :stroke-width="1" style="color:var(--ink-300)" />
        <div style="font-size:14px;color:var(--ink-700);font-weight:600">没有找到相关笔记</div>
        <div class="lead" style="max-width:380px">{{ submitted.answer_markdown }}</div>
      </div>

      <template v-else>
        <!-- 答案正文 -->
        <div class="card pad" style="margin-top:18px">
          <div class="muted" style="font-size:12.5px;margin-bottom:10px;display:flex;align-items:center;gap:6px">
            <Sparkles :size="14" />综合自 {{ submitted.retrieved_count }} 条来源
          </div>
          <!-- 答案就绪 -->
          <template v-if="answerMd !== null">
            <MarkdownViewer :content="answerMd" :job-id="''" />
            <!-- AI 审计入口:task_id 有才给,无命中/投递失败无 task -->
            <div v-if="submitted.task_id" style="margin-top:12px">
              <button class="btn-audit" @click="showAudit = !showAudit">
                <ScrollText :size="13" />{{ showAudit ? '收起 AI 审计' : '查看 AI 审计' }}
              </button>
              <div v-if="showAudit" style="margin-top:10px">
                <AiTaskAuditPanel :task-id="submitted.task_id" />
              </div>
            </div>
          </template>
          <!-- 轮询失败/超时 -->
          <div v-else-if="answerErr" style="font-size:13px;color:var(--ink-600)">⚠️ {{ answerErr }}</div>
          <!-- 综合中(轮询答案) -->
          <div v-else-if="answering" style="font-size:13px;color:var(--ink-500)">
            综合中…（AI 正在跨笔记作答，可能需要十几秒）
          </div>
        </div>

        <!-- 来源 chips(随 202 即得,先于答案显示) -->
        <div v-if="submitted.sources.length" style="margin-top:16px">
          <div class="muted" style="font-size:12.5px;margin-bottom:9px">引用来源</div>
          <div class="source-chips">
            <button
              v-for="(s, i) in submitted.sources"
              :key="`${s.job_id}-${s.evidence?.chunk_id || i}`"
              class="source-chip"
              :title="s.title"
              @click="openSource(s.job_id)"
            >
              <span class="chip-head">
                <span class="chip-idx">来源{{ i + 1 }}</span>
                <component :is="contentTypeIcon(s.content_type)" :size="14" />
                <span class="chip-title">{{ s.title }}</span>
                <span v-if="s.domain && s.domain !== 'general'" class="chip-dom">{{ s.domain }}</span>
              </span>
              <span v-if="evidenceLabel(s)" class="chip-evidence">{{ evidenceLabel(s) }}</span>
            </button>
          </div>
        </div>
      </template>
    </template>

    <!-- 初始态 -->
    <div v-else class="note-tip" style="margin-top:18px">
      提个问题开始吧。答案会内联标注 [来源N]，下方列出可点击的来源笔记。
    </div>
  </section>
</template>

<style scoped>
.source-chips { display:flex; flex-wrap:wrap; gap:8px; }
.source-chip {
  display:inline-flex; flex-direction:column; align-items:flex-start; gap:3px;
  max-width:340px; min-width:0; padding:6px 11px;
  background:var(--surface); border:1px solid var(--line);
  border-radius:999px; cursor:pointer; font-size:12.5px; color:var(--ink-700);
  transition:border-color .12s, background .12s;
}
.source-chip:hover { border-color:var(--brand-300); background:var(--brand-50); }
.chip-head { display:flex; align-items:center; gap:7px; min-width:0; width:100%; }
.chip-idx { font-weight:600; color:var(--brand-700); font-size:11.5px; flex:none; }
.chip-title { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chip-dom { color:var(--ink-400); font-size:11px; flex:none; }
.chip-evidence {
  max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  color:var(--ink-500); font-size:11.5px; padding-left:46px;
}
.btn-audit {
  display:inline-flex; align-items:center; gap:6px;
  padding:5px 10px; font-size:12px; color:var(--ink-600);
  background:var(--surface); border:1px solid var(--line); border-radius:7px; cursor:pointer;
}
.btn-audit:hover { border-color:var(--brand-300); color:var(--brand-700); }
</style>
