<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import MarkdownViewer from '../components/notes/MarkdownViewer.vue'
import { fmtDateTime } from '../utils/datetime'
import { contentTypeIcon, contentTypePill, contentTypeLabel } from '../utils/contentType'
import {
  Radar, ChevronRight, TrendingUp, Sparkles, Flame, LayoutList, FileText,
} from 'lucide-vue-next'

// 本周知识雷达:GET /radar(无 LLM,秒开)渲染各板块;「生成本周摘要」按钮 → POST /digest(LLM)。
// 形状(后端 api/services/radar.py):
//   { rising_concepts:[{term,recent,prior,delta}], new_concepts:[{term,definition,first_seen}],
//     recent_jobs:[{job_id,title,published_at,content_type}], top_recent_concepts:[{term,recent}],
//     window:{days,since,until} }
const route = useRoute()
const router = useRouter()
const api = useApi()

const domain = computed(() => String(route.params.domain))

interface Rising { term: string; recent: number; prior: number; delta: number }
interface NewConcept { term: string; definition: string; first_seen: string }
interface RecentJob { job_id: string; title: string | null; published_at: string; content_type: string }
interface TopConcept { term: string; recent: number }
interface RadarData {
  rising_concepts: Rising[]
  new_concepts: NewConcept[]
  recent_jobs: RecentJob[]
  top_recent_concepts: TopConcept[]
  window: { days: number; since: string; until: string }
}

const data = ref<RadarData | null>(null)
const loading = ref(false)
const error = ref('')

const digest = ref('')
const digesting = ref(false)
const digestError = ref('')

// 窗口标题:06.20–06.26(本地短日期)。
function shortDate(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}.${p(d.getDate())}`
}
const windowLabel = computed(() => {
  const w = data.value?.window
  if (!w) return ''
  return `${shortDate(w.since)}–${shortDate(w.until)}`
})

const isEmpty = computed(() =>
  !!data.value
  && data.value.recent_jobs.length === 0
  && data.value.rising_concepts.length === 0
  && data.value.new_concepts.length === 0,
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get<RadarData>(
      `/api/domains/${encodeURIComponent(domain.value)}/radar?window_days=7`,
    )
  } catch (e: any) {
    error.value = e?.message || '加载知识雷达失败'
  } finally {
    loading.value = false
  }
}

async function generateDigest() {
  digesting.value = true
  digestError.value = ''
  try {
    const r = await api.post<{ markdown: string; window: any }>(
      `/api/domains/${encodeURIComponent(domain.value)}/digest?window_days=7`,
    )
    digest.value = r?.markdown || ''
  } catch (e: any) {
    digestError.value = e?.message || '生成摘要失败'
  } finally {
    digesting.value = false
  }
}

function goDomain() {
  router.push(`/kb/${encodeURIComponent(domain.value)}`)
}
function goConcept(term: string) {
  router.push(`/kb/${encodeURIComponent(domain.value)}/concepts/${encodeURIComponent(term)}`)
}
function openJob(j: RecentJob) {
  router.push(`/content/${j.job_id}`)
}

onMounted(load)
watch(domain, load)
</script>

<template>
  <section class="page">
    <!-- 头部 -->
    <div style="display:flex;align-items:center;gap:13px;margin-bottom:6px">
      <div style="min-width:0">
        <div class="h1"><Radar :size="18" />本周知识雷达<span v-if="windowLabel" class="dim" style="font-weight:400"> ({{ windowLabel }})</span></div>
        <div class="lead">
          <a class="term-link" @click="goDomain">{{ domain }}</a>
          <template v-if="data">
            · 新增 {{ data.recent_jobs.length }} 篇 · 新概念 {{ data.new_concepts.length }}
          </template>
        </div>
      </div>
      <button class="btn sm" style="margin-left:auto" :disabled="loading" @click="load">刷新</button>
    </div>

    <!-- 错误态 -->
    <div v-if="error" class="card pad" style="text-align:center;margin-top:20px">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="load">重试</button>
    </div>

    <!-- 加载态 -->
    <div v-else-if="loading && !data" class="card pad" style="text-align:center;color:var(--ink-500);margin-top:20px">
      加载中…
    </div>

    <!-- 空态 -->
    <div v-else-if="isEmpty" class="card pad" style="text-align:center;padding:40px 18px;margin-top:20px">
      <Radar :size="40" :stroke-width="1" style="color:var(--ink-300);margin-bottom:12px" />
      <p class="muted">本周这个知识库还没有新动静（窗口内无新增内容 / 概念变化）</p>
    </div>

    <!-- 主体 -->
    <template v-else-if="data">
      <!-- ↑飙升 -->
      <div class="seclabel" style="margin:22px 0 10px"><TrendingUp :size="14" />↑ 飙升概念</div>
      <div v-if="data.rising_concepts.length" class="list">
        <div v-for="c in data.rising_concepts" :key="c.term" class="row rising-row" @click="goConcept(c.term)">
          <div class="body">
            <div class="title">{{ c.term }}</div>
            <div class="meta"><span class="dim">本周 {{ c.recent }} 次 · 上周 {{ c.prior }} 次</span></div>
          </div>
          <span class="delta-pill">+{{ c.delta }}</span>
          <ChevronRight :size="16" class="dim" />
        </div>
      </div>
      <p v-else class="muted" style="font-size:13px;margin:0 0 4px">本周没有概念热度上升</p>

      <!-- ✦新出现 -->
      <div class="seclabel" style="margin:22px 0 10px"><Sparkles :size="14" />✦ 新出现概念</div>
      <div v-if="data.new_concepts.length" class="list">
        <div v-for="c in data.new_concepts" :key="c.term" class="row" @click="goConcept(c.term)">
          <div class="body">
            <div class="title">{{ c.term }}</div>
            <div v-if="c.definition" class="meta"><span class="dim">{{ c.definition }}</span></div>
          </div>
          <ChevronRight :size="16" class="dim" />
        </div>
      </div>
      <p v-else class="muted" style="font-size:13px;margin:0 0 4px">本周没有新概念</p>

      <!-- 🔥热点 -->
      <div class="seclabel" style="margin:22px 0 10px"><Flame :size="14" />🔥 热点概念</div>
      <div v-if="data.top_recent_concepts.length" class="chips">
        <button
          v-for="c in data.top_recent_concepts" :key="c.term"
          class="chip" @click="goConcept(c.term)"
        >{{ c.term }} <span class="dim">·{{ c.recent }}</span></button>
      </div>
      <p v-else class="muted" style="font-size:13px;margin:0 0 4px">本周暂无热点</p>

      <!-- 本周摘要 -->
      <div class="seclabel" style="margin:22px 0 10px"><FileText :size="14" />本周摘要</div>
      <div class="card pad">
        <div v-if="!digest && !digesting" style="text-align:center;padding:6px 0">
          <button class="btn" :disabled="digesting" @click="generateDigest">
            <Sparkles :size="14" />生成本周摘要
          </button>
          <p class="muted" style="font-size:12px;margin:10px 0 0">用 AI 总结本周知识源在聊什么、新概念、热点</p>
        </div>
        <div v-else-if="digesting" style="text-align:center;color:var(--ink-500);padding:6px 0">生成中…（调用 AI，稍候）</div>
        <template v-else>
          <MarkdownViewer :content="digest" :job-id="''" :domain="domain" />
          <button class="btn sm" style="margin-top:10px" @click="generateDigest">重新生成</button>
        </template>
        <p v-if="digestError" class="muted" style="color:var(--danger,#dc2626);font-size:12px;margin:10px 0 0">{{ digestError }}</p>
      </div>

      <!-- 本周新增内容 -->
      <div class="seclabel" style="margin:22px 0 10px"><LayoutList :size="14" />本周新增内容 · {{ data.recent_jobs.length }}</div>
      <div v-if="data.recent_jobs.length" class="list">
        <div v-for="j in data.recent_jobs" :key="j.job_id" class="row" @click="openJob(j)">
          <span class="type-pill" :class="contentTypePill(j.content_type)">
            <component :is="contentTypeIcon(j.content_type)" :size="17" />
          </span>
          <div class="body">
            <div class="title">{{ j.title || j.job_id }}</div>
            <div class="meta">
              <span>{{ contentTypeLabel(j.content_type) }}</span>
              <span class="sep">·</span>
              <span class="dim">{{ fmtDateTime(j.published_at) }}</span>
            </div>
          </div>
          <ChevronRight :size="16" class="dim" />
        </div>
      </div>
      <p v-else class="muted" style="font-size:13px;margin:0">本周没有新增内容</p>
    </template>
  </section>
</template>

<style scoped>
.delta-pill {
  font-size: 12px;
  font-weight: 600;
  color: #16a34a;
  background: #f0fdf4;
  border-radius: 999px;
  padding: 2px 9px;
  white-space: nowrap;
}
.rising-row .delta-pill { margin-left: auto; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
