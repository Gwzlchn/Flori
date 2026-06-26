<script setup lang="ts">
// 问知识库(跨源综合问答):提问 → POST /api/ask → 渲染带引用的综合答案 + 来源 chips。
// 答案是受控 markdown(后端 LLM 生成,含 [来源N] 内联引用 + 「共识 / 分歧」段),
// 复用 MarkdownViewer(markdown-it, html:false)安全渲染;来源 chip 跳 /content/{job_id}。
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useDomainStore } from '../stores/domains'
import MarkdownViewer from '../components/notes/MarkdownViewer.vue'
import { contentTypeIcon, contentTypeLabel } from '../utils/contentType'
import type { AskResponse } from '../types'
import { Sparkles, MessageCircleQuestion, Send } from 'lucide-vue-next'

const api = useApi()
const router = useRouter()
const domainStore = useDomainStore()

const question = ref('')
const domain = ref('')          // '' = 全库
const loading = ref(false)
const error = ref('')
const answer = ref<AskResponse | null>(null)

onMounted(() => { if (!domainStore.domains.length) domainStore.fetchAll() })

async function ask() {
  const q = question.value.trim()
  if (!q || loading.value) return
  loading.value = true
  error.value = ''
  answer.value = null
  try {
    answer.value = await api.post<AskResponse>('/api/ask', {
      question: q,
      domain: domain.value.trim() || null,
    })
  } catch (e: any) {
    error.value = e?.message || '提问失败，请稍后重试。'
  } finally {
    loading.value = false
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
          <Send :size="16" /><span>{{ loading ? '综合中…' : '提问' }}</span>
        </button>
      </div>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="card pad" style="margin-top:18px;color:var(--ink-500);font-size:13px">
      正在检索相关笔记并综合答案，这可能需要十几秒…
    </div>

    <!-- 错误态 -->
    <div v-else-if="error" class="card pad"
      style="margin-top:18px;display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:32px 18px">
      <div style="font-size:13.5px;color:var(--ink-700)">{{ error }}</div>
      <button class="btn" @click="ask">重试</button>
    </div>

    <!-- 结果态 -->
    <template v-else-if="answer">
      <!-- 无命中 -->
      <div v-if="answer.retrieved_count === 0" class="card pad"
        style="margin-top:18px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:40px 18px">
        <MessageCircleQuestion :size="40" :stroke-width="1" style="color:var(--ink-300)" />
        <div style="font-size:14px;color:var(--ink-700);font-weight:600">没有找到相关笔记</div>
        <div class="lead" style="max-width:380px">{{ answer.answer_markdown }}</div>
      </div>

      <template v-else>
        <!-- 答案正文 -->
        <div class="card pad" style="margin-top:18px">
          <div class="muted" style="font-size:12.5px;margin-bottom:10px;display:flex;align-items:center;gap:6px">
            <Sparkles :size="14" />综合自 {{ answer.retrieved_count }} 篇笔记
          </div>
          <MarkdownViewer :content="answer.answer_markdown" :job-id="''" />
        </div>

        <!-- 来源 chips -->
        <div style="margin-top:16px">
          <div class="muted" style="font-size:12.5px;margin-bottom:9px">引用来源</div>
          <div class="source-chips">
            <button
              v-for="(s, i) in answer.sources"
              :key="s.job_id"
              class="source-chip"
              :title="s.title"
              @click="openSource(s.job_id)"
            >
              <span class="chip-idx">来源{{ i + 1 }}</span>
              <component :is="contentTypeIcon(s.content_type)" :size="14" />
              <span class="chip-title">{{ s.title }}</span>
              <span v-if="s.domain && s.domain !== 'general'" class="chip-dom">{{ s.domain }}</span>
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
  display:inline-flex; align-items:center; gap:7px;
  max-width:320px; padding:6px 11px;
  background:var(--surface); border:1px solid var(--line);
  border-radius:999px; cursor:pointer; font-size:12.5px; color:var(--ink-700);
  transition:border-color .12s, background .12s;
}
.source-chip:hover { border-color:var(--brand-300); background:var(--brand-50); }
.chip-idx { font-weight:600; color:var(--brand-700); font-size:11.5px; flex:none; }
.chip-title { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chip-dom { color:var(--ink-400); font-size:11px; flex:none; }
</style>
