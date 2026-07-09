<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { fmtClock, fmtDateTime } from '../utils/datetime'
import type { StudyCard, StudyCardListResponse } from '../types'
import {
  BookOpenCheck, CheckCircle2, Eye, GraduationCap, Pause, Plus, RotateCcw,
  Sparkles, Trash2,
} from 'lucide-vue-next'

const api = useApi()
const router = useRouter()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const loading = ref(true)
const reviewing = ref(false)
const saving = ref(false)
const error = ref('')
const due = ref<StudyCard[]>([])
const cards = ref<StudyCard[]>([])
const totalCards = ref(0)
const selectedDomain = ref('')
const revealed = ref(false)

const form = reactive({
  domain: 'general',
  front: '',
  back: '',
  explanation: '',
  concept_term: '',
  job_id: '',
  evidence_snippet: '',
})

const current = computed(() => due.value[0] || null)
const activeCards = computed(() => cards.value.filter((c) => c.status === 'active').length)
const suspendedCards = computed(() => cards.value.filter((c) => c.status === 'suspended').length)
const domainOptions = computed(() => {
  const set = new Set<string>()
  for (const c of cards.value) if (c.domain) set.add(c.domain)
  if (form.domain) set.add(form.domain)
  return [...set].sort()
})

function evidenceText(card: StudyCard): string {
  const ev = Array.isArray(card.evidence) ? card.evidence[0] : card.evidence
  if (!ev || typeof ev !== 'object') return ''
  return ev.snippet || ev.section || ev.chunk_id || ev.note_type || ''
}

function nextDue(card: StudyCard): string {
  if (!card.review) return '未排入队列'
  return fmtDateTime(card.review.due_at)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const domain = selectedDomain.value ? `domain=${encodeURIComponent(selectedDomain.value)}&` : ''
    const [dueResp, cardResp] = await Promise.all([
      api.get<StudyCardListResponse>(`/api/study/due?${domain}limit=50`),
      api.get<StudyCardListResponse>(`/api/study/cards?${domain}limit=100`),
    ])
    due.value = dueResp.items
    cards.value = cardResp.items
    totalCards.value = cardResp.total
    revealed.value = false
  } catch (e: any) {
    error.value = e?.message || '加载学习队列失败'
  } finally {
    loading.value = false
  }
}

async function createCard() {
  if (!form.front.trim() || !form.back.trim() || saving.value) return
  saving.value = true
  try {
    const evidence = form.evidence_snippet.trim()
      ? [{ snippet: form.evidence_snippet.trim(), source: 'manual' }]
      : []
    const card = await api.post<StudyCard>('/api/study/cards', {
      domain: form.domain.trim() || 'general',
      front: form.front.trim(),
      back: form.back.trim(),
      explanation: form.explanation.trim(),
      concept_term: form.concept_term.trim() || null,
      job_id: form.job_id.trim() || null,
      evidence,
      status: 'active',
      source: 'manual',
    })
    showToast('卡片已加入复习队列', 'success')
    form.front = ''
    form.back = ''
    form.explanation = ''
    form.concept_term = ''
    form.job_id = ''
    form.evidence_snippet = ''
    if (!selectedDomain.value && card.domain) form.domain = card.domain
    await load()
  } catch (e: any) {
    showToast(e?.message || '创建卡片失败', 'error')
  } finally {
    saving.value = false
  }
}

async function gradeCurrent(grade: 'again' | 'hard' | 'good' | 'easy') {
  const card = current.value
  if (!card || reviewing.value) return
  reviewing.value = true
  try {
    await api.post('/api/study/reviews', { card_id: card.card_id, grade })
    showToast('已记录复习', 'success')
    await load()
  } catch (e: any) {
    showToast(e?.message || '记录失败', 'error')
  } finally {
    reviewing.value = false
  }
}

async function setStatus(card: StudyCard, status: 'active' | 'suspended') {
  try {
    await api.post(`/api/study/cards/${encodeURIComponent(card.card_id)}/status`, { status })
    showToast(status === 'active' ? '已恢复' : '已暂停', 'success')
    await load()
  } catch (e: any) {
    showToast(e?.message || '操作失败', 'error')
  }
}

async function deleteCard(card: StudyCard) {
  if (!confirm('确定删除这张卡片？')) return
  try {
    await api.del(`/api/study/cards/${encodeURIComponent(card.card_id)}`)
    showToast('已删除', 'success')
    await load()
  } catch (e: any) {
    showToast(e?.message || '删除失败', 'error')
  }
}

function openSource(card: StudyCard) {
  if (card.job_id) router.push(`/content/${encodeURIComponent(card.job_id)}`)
}

onMounted(load)
</script>

<template>
  <section class="page study-page">
    <div class="study-head">
      <div>
        <div class="h1"><GraduationCap :size="19" />学习</div>
        <div class="lead">
          待复习 {{ due.length }} · 卡片 {{ totalCards }} · 活跃 {{ activeCards }}
          <template v-if="suspendedCards"> · 暂停 {{ suspendedCards }}</template>
        </div>
      </div>
      <div class="head-actions">
        <select v-model="selectedDomain" class="input domain-select" @change="load">
          <option value="">全部知识库</option>
          <option v-for="d in domainOptions" :key="d" :value="d">{{ d }}</option>
        </select>
        <button class="btn sm" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="error" class="state-panel">
      <p>{{ error }}</p>
      <button class="btn sm" @click="load">重试</button>
    </div>

    <div v-else class="study-grid">
      <section class="study-panel review-panel">
        <div class="panel-title">
          <BookOpenCheck :size="16" />
          <span>今日复习</span>
          <span v-if="current?.review" class="dim">到期 {{ fmtClock(current.review.due_at) }}</span>
        </div>

        <div v-if="loading && !current" class="state-panel compact">加载中...</div>
        <div v-else-if="!current" class="empty-review">
          <CheckCircle2 :size="42" />
          <p>今天没有到期卡片</p>
        </div>
        <article v-else class="review-card">
          <div class="card-meta">
            <span class="pill">{{ current.domain }}</span>
            <span v-if="current.concept_term" class="pill ghost">{{ current.concept_term }}</span>
            <button v-if="current.job_id" class="mini-link" @click="openSource(current)">来源</button>
          </div>
          <h2>{{ current.front }}</h2>
          <button v-if="!revealed" class="btn reveal" @click="revealed = true">
            <Eye :size="16" />显示答案
          </button>
          <div v-else class="answer-block">
            <div class="answer">{{ current.back }}</div>
            <p v-if="current.explanation" class="explain">{{ current.explanation }}</p>
            <p v-if="evidenceText(current)" class="evidence-line">{{ evidenceText(current) }}</p>
            <div class="grade-row">
              <button class="grade again" :disabled="reviewing" @click="gradeCurrent('again')"><RotateCcw :size="15" />重来</button>
              <button class="grade hard" :disabled="reviewing" @click="gradeCurrent('hard')">困难</button>
              <button class="grade good" :disabled="reviewing" @click="gradeCurrent('good')">掌握</button>
              <button class="grade easy" :disabled="reviewing" @click="gradeCurrent('easy')"><Sparkles :size="15" />简单</button>
            </div>
          </div>
        </article>
      </section>

      <section class="study-panel create-panel">
        <div class="panel-title"><Plus :size="16" /><span>新增卡片</span></div>
        <form class="card-form" @submit.prevent="createCard">
          <label>知识库<input v-model="form.domain" class="input" /></label>
          <label>正面<textarea v-model="form.front" class="input" rows="3" /></label>
          <label>背面<textarea v-model="form.back" class="input" rows="3" /></label>
          <label>解释<textarea v-model="form.explanation" class="input" rows="2" /></label>
          <div class="form-row">
            <label>概念<input v-model="form.concept_term" class="input" /></label>
            <label>job_id<input v-model="form.job_id" class="input" /></label>
          </div>
          <label>来源片段<input v-model="form.evidence_snippet" class="input" /></label>
          <button class="btn submit-card" :disabled="saving || !form.front.trim() || !form.back.trim()">
            <Plus :size="15" />加入复习
          </button>
        </form>
      </section>
    </div>

    <section class="library-section">
      <div class="panel-title"><span>卡片库</span><span class="dim">{{ cards.length }} / {{ totalCards }}</span></div>
      <div v-if="!cards.length && !loading" class="state-panel compact">还没有卡片</div>
      <div v-else class="card-list">
        <article v-for="card in cards" :key="card.card_id" class="card-row">
          <div class="row-main">
            <div class="row-title">{{ card.front }}</div>
            <div class="row-sub">{{ card.back }}</div>
            <div class="row-meta">
              <span>{{ card.domain }}</span>
              <span v-if="card.concept_term">{{ card.concept_term }}</span>
              <span>{{ card.status === 'active' ? '下次 ' + nextDue(card) : card.status }}</span>
            </div>
          </div>
          <div class="row-actions">
            <button v-if="card.job_id" class="icon-btn" title="打开来源" @click="openSource(card)"><BookOpenCheck :size="15" /></button>
            <button v-if="card.status === 'active'" class="icon-btn" title="暂停" @click="setStatus(card, 'suspended')"><Pause :size="15" /></button>
            <button v-else class="icon-btn" title="恢复" @click="setStatus(card, 'active')"><RotateCcw :size="15" /></button>
            <button class="icon-btn danger" title="删除" @click="deleteCard(card)"><Trash2 :size="15" /></button>
          </div>
        </article>
      </div>
    </section>
  </section>
</template>

<style scoped>
.study-page { max-width: 1180px; }
.study-head { display: flex; align-items: flex-start; gap: 16px; justify-content: space-between; margin-bottom: 16px; }
.head-actions { display: flex; align-items: center; gap: 8px; }
.domain-select { width: 180px; min-height: 34px; }
.study-grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(320px, .85fr); gap: 14px; align-items: start; }
.study-panel, .library-section, .state-panel {
  border: 1px solid var(--line-soft);
  border-radius: var(--r-sm);
  background: var(--surface);
}
.study-panel { padding: 16px; }
.panel-title { display: flex; align-items: center; gap: 8px; min-height: 24px; margin-bottom: 12px; color: var(--ink-800); font-weight: 700; }
.panel-title .dim { margin-left: auto; font-weight: 500; }
.state-panel { padding: 28px; text-align: center; color: var(--ink-500); }
.state-panel.compact { padding: 22px; }
.review-card { min-height: 330px; display: flex; flex-direction: column; justify-content: center; gap: 16px; }
.card-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
.pill { display: inline-flex; align-items: center; min-height: 24px; padding: 3px 8px; border-radius: 6px; background: var(--brand-50); color: var(--brand-700); font-size: 12px; font-weight: 700; }
.pill.ghost { background: var(--line-soft); color: var(--ink-600); }
.mini-link { border: 0; background: transparent; color: var(--brand-700); font-weight: 700; cursor: pointer; }
.review-card h2 { margin: 0; font-size: 24px; line-height: 1.38; color: var(--ink-900); letter-spacing: 0; }
.reveal { align-self: flex-start; }
.answer-block { border-top: 1px solid var(--line-soft); padding-top: 14px; }
.answer { font-size: 17px; line-height: 1.6; color: var(--ink-900); white-space: pre-wrap; }
.explain, .evidence-line { margin: 10px 0 0; line-height: 1.55; color: var(--ink-500); }
.evidence-line { font-size: 12px; border-left: 2px solid var(--brand-200); padding-left: 8px; }
.grade-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 16px; }
.grade { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); background: var(--surface); color: var(--ink-700); font-weight: 700; cursor: pointer; }
.grade:hover { border-color: var(--brand-200); color: var(--brand-700); }
.grade.again { color: #b45309; }
.grade.good { color: #047857; }
.grade.easy { color: var(--brand-700); }
.empty-review { min-height: 330px; display: grid; place-items: center; align-content: center; gap: 10px; color: var(--ink-400); }
.empty-review p { margin: 0; font-weight: 700; color: var(--ink-500); }
.card-form { display: grid; gap: 10px; }
.card-form label { display: grid; gap: 5px; margin: 0; font-size: 12px; font-weight: 700; color: var(--ink-500); }
.card-form .input { font-size: 13px; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.submit-card { justify-content: center; margin-top: 2px; }
.library-section { margin-top: 14px; padding: 14px 16px; }
.card-list { display: grid; gap: 8px; }
.card-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; border-top: 1px solid var(--line-soft); padding: 10px 0 2px; }
.card-row:first-child { border-top: 0; padding-top: 0; }
.row-main { min-width: 0; }
.row-title { color: var(--ink-900); font-weight: 700; line-height: 1.35; }
.row-sub { margin-top: 3px; color: var(--ink-600); line-height: 1.45; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; color: var(--ink-400); font-size: 12px; }
.row-actions { display: flex; gap: 4px; }
.icon-btn { display: grid; place-items: center; width: 30px; height: 30px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); background: var(--surface); color: var(--ink-500); cursor: pointer; }
.icon-btn:hover { color: var(--ink-900); background: var(--line-soft); }
.icon-btn.danger:hover { color: #dc2626; }

@media (max-width: 900px) {
  .study-head { flex-direction: column; }
  .head-actions { width: 100%; }
  .domain-select { flex: 1; width: auto; }
  .study-grid { grid-template-columns: 1fr; }
}

@media (max-width: 560px) {
  .review-card h2 { font-size: 20px; }
  .grade-row, .form-row { grid-template-columns: 1fr 1fr; }
  .card-row { grid-template-columns: 1fr; }
  .row-actions { justify-content: flex-start; }
}
</style>
