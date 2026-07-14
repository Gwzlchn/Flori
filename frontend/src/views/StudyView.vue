<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { fmtClock, fmtDateTime } from '../utils/datetime'
import type {
  StudyCard, StudyCardListResponse, StudyMasteryItem, StudyMasteryResponse,
  StudyStats, StudySuggestion, StudySuggestionBatch, StudySuggestionListResponse,
  StudySuggestionOperationsResponse,
} from '../types'
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
const qualityLoading = ref(false)
const qualityError = ref('')
const suggestions = ref<StudySuggestion[]>([])
const mastery = ref<StudyMasteryItem[]>([])
const currentBatch = ref<StudySuggestionBatch | null>(null)
const generating = ref(false)
const operating = ref(false)
const selectedSuggestionIds = ref<string[]>([])
const rejectReason = ref('')
const maxCards = ref(10)
const suggestionDrafts = reactive<Record<string, {
  card_type: 'basic' | 'cloze' | 'qa'
  front: string
  back: string
  explanation: string
  concept_term: string
}>>({})
let batchPollTimer: ReturnType<typeof setTimeout> | null = null
let batchPollEpoch = 0
let componentAlive = false
let pendingBatchCreate: { signature: string; request_id: string } | null = null
let pendingBatchRetry: { signature: string; request_id: string } | null = null
let pendingSuggestionOperation: { signature: string; request_id: string } | null = null
const stats = ref<StudyStats>({
  total: 0,
  statuses: { suggested: 0, active: 0, suspended: 0, rejected: 0 },
  due: 0,
  reviewed_cards: 0,
  reviews_total: 0,
  grades: { again: 0, hard: 0, good: 0, easy: 0 },
  retained_reviews: 0,
  retention_rate: 0,
})
const pendingReview = ref<{
  card_id: string
  revision: number
  grade: 'again' | 'hard' | 'good' | 'easy'
  request_id: string
} | null>(null)

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
const activeCards = computed(() => stats.value.statuses.active)
const suspendedCards = computed(() => stats.value.statuses.suspended)
const domainOptions = computed(() => {
  const set = new Set<string>()
  for (const c of cards.value) if (c.domain) set.add(c.domain)
  if (form.domain) set.add(form.domain)
  return [...set].sort()
})
const selectedSuggestions = computed(() => suggestions.value.filter(
  (item) => selectedSuggestionIds.value.includes(item.suggestion_id),
))
const selectedBatchId = computed(() => selectedSuggestions.value[0]?.batch_id || '')
const generationDomain = computed(() => selectedDomain.value || form.domain.trim() || 'general')

function requestId(prefix: string): string {
  const suffix = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `${prefix}:${suffix}`
}

function requestFor(
  pending: { signature: string; request_id: string } | null,
  signature: string,
  prefix: string,
): { signature: string; request_id: string } {
  return pending?.signature === signature
    ? pending
    : { signature, request_id: requestId(prefix) }
}

function isAmbiguousFailure(value: any): boolean {
  const status = value?.status
  return typeof status !== 'number'
    || status <= 0
    || status === 408
    || status === 429
    || status >= 500
}

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
    const statsPath = selectedDomain.value
      ? `/api/study/stats?domain=${encodeURIComponent(selectedDomain.value)}`
      : '/api/study/stats'
    const [dueResp, cardResp, statsResp] = await Promise.all([
      api.get<StudyCardListResponse>(`/api/study/due?${domain}limit=50`),
      api.get<StudyCardListResponse>(`/api/study/cards?${domain}limit=100`),
      api.get<StudyStats>(statsPath),
    ])
    due.value = dueResp.items
    cards.value = cardResp.items
    totalCards.value = cardResp.total
    stats.value = statsResp
    revealed.value = false
  } catch (e: any) {
    error.value = e?.message || '加载学习队列失败'
  } finally {
    loading.value = false
  }
}

function syncSuggestionDrafts(items: StudySuggestion[]) {
  for (const item of items) {
    suggestionDrafts[item.suggestion_id] = {
      card_type: item.card_type,
      front: item.front,
      back: item.back,
      explanation: item.explanation,
      concept_term: item.concept_term || '',
    }
  }
}

async function loadQuality() {
  qualityLoading.value = true
  qualityError.value = ''
  try {
    const domain = selectedDomain.value
      ? `domain=${encodeURIComponent(selectedDomain.value)}&`
      : ''
    const masteryPath = selectedDomain.value
      ? `/api/study/mastery?domain=${encodeURIComponent(selectedDomain.value)}`
      : '/api/study/mastery'
    const [suggestionResp, masteryResp] = await Promise.all([
      api.get<StudySuggestionListResponse>(`/api/study/suggestions?${domain}status=suggested&limit=100`),
      api.get<StudyMasteryResponse>(masteryPath),
    ])
    suggestions.value = suggestionResp.items || []
    mastery.value = masteryResp.items || []
    syncSuggestionDrafts(suggestions.value)
    const liveIds = new Set(suggestions.value.map((item) => item.suggestion_id))
    selectedSuggestionIds.value = selectedSuggestionIds.value.filter((id) => liveIds.has(id))
  } catch (e: any) {
    qualityError.value = e?.message || '加载自动卡片失败'
  } finally {
    qualityLoading.value = false
  }
}

async function reloadAll() {
  await Promise.all([load(), loadQuality()])
}

function clearBatchPoll() {
  if (batchPollTimer !== null) {
    clearTimeout(batchPollTimer)
    batchPollTimer = null
  }
}

function scheduleBatchPoll(
  epoch = batchPollEpoch,
  batchId = currentBatch.value?.batch_id || '',
  force = false,
) {
  clearBatchPoll()
  const activeId = currentBatch.value?.batch_id || localStorage.getItem('study_suggestion_batch_id')
  const canPoll = force
    || !currentBatch.value
    || ['pending_enqueue', 'queued'].includes(currentBatch.value.status)
  if (
    componentAlive
    && epoch === batchPollEpoch
    && batchId
    && activeId === batchId
    && canPoll
  ) {
    batchPollTimer = setTimeout(() => void pollBatch(epoch, batchId), 2000)
  }
}

async function pollBatch(epoch = batchPollEpoch, requestedBatchId?: string) {
  const batchId = requestedBatchId
    || currentBatch.value?.batch_id
    || localStorage.getItem('study_suggestion_batch_id')
  if (!batchId) return
  const isCurrentRequest = () => {
    if (!componentAlive || epoch !== batchPollEpoch) return false
    const activeId = currentBatch.value?.batch_id
    return activeId ? activeId === batchId : localStorage.getItem('study_suggestion_batch_id') === batchId
  }
  try {
    const batch = await api.get<StudySuggestionBatch>(
      `/api/study/suggestion-batches/${encodeURIComponent(batchId)}`,
    )
    if (!isCurrentRequest()) return
    if (batch.batch_id !== batchId) {
      qualityError.value = '读取生成批次返回了错误批次'
      scheduleBatchPoll(epoch, batchId, true)
      return
    }
    currentBatch.value = batch
    qualityError.value = ''
    if (batch.status === 'ready') {
      await loadQuality()
      if (!isCurrentRequest()) return
    }
    scheduleBatchPoll(epoch, batchId)
  } catch (e: any) {
    if (!isCurrentRequest()) return
    clearBatchPoll()
    if (e?.status === 404) {
      localStorage.removeItem('study_suggestion_batch_id')
      currentBatch.value = null
    } else {
      qualityError.value = e?.message || '读取生成批次失败'
      scheduleBatchPoll(epoch, batchId, true)
    }
  }
}

async function generateSuggestions() {
  if (generating.value) return
  generating.value = true
  clearBatchPoll()
  const epoch = ++batchPollEpoch
  const signature = JSON.stringify({
    domain: generationDomain.value,
    max_cards: maxCards.value,
  })
  pendingBatchCreate = requestFor(
    pendingBatchCreate,
    signature,
    'study-suggestion-batch',
  )
  try {
    currentBatch.value = await api.post<StudySuggestionBatch>('/api/study/suggestion-batches', {
      request_id: pendingBatchCreate.request_id,
      domain: generationDomain.value,
      max_cards: maxCards.value,
    })
    pendingBatchCreate = null
    localStorage.setItem('study_suggestion_batch_id', currentBatch.value.batch_id)
    showToast('已创建自动卡片批次', 'success')
    if (currentBatch.value.status === 'ready') await loadQuality()
    else scheduleBatchPoll(epoch, currentBatch.value.batch_id)
  } catch (e: any) {
    if (!isAmbiguousFailure(e)) pendingBatchCreate = null
    showToast(e?.message || '创建自动卡片批次失败', 'error')
    if (currentBatch.value) scheduleBatchPoll(epoch, currentBatch.value.batch_id)
  } finally {
    generating.value = false
  }
}

async function retryBatch() {
  const batch = currentBatch.value
  if (!batch || batch.status !== 'failed' || generating.value) return
  generating.value = true
  clearBatchPoll()
  const epoch = ++batchPollEpoch
  const signature = JSON.stringify({
    batch_id: batch.batch_id,
    expected_revision: batch.revision,
  })
  pendingBatchRetry = requestFor(
    pendingBatchRetry,
    signature,
    'study-suggestion-retry',
  )
  try {
    currentBatch.value = await api.post<StudySuggestionBatch>(
      `/api/study/suggestion-batches/${encodeURIComponent(batch.batch_id)}/retry`,
      {
        request_id: pendingBatchRetry.request_id,
        expected_revision: batch.revision,
      },
    )
    pendingBatchRetry = null
    showToast('已重新提交生成任务', 'success')
    scheduleBatchPoll(epoch, currentBatch.value.batch_id)
  } catch (e: any) {
    if (!isAmbiguousFailure(e)) pendingBatchRetry = null
    if (e?.status === 409) await pollBatch(epoch, batch.batch_id)
    else scheduleBatchPoll(epoch, batch.batch_id, true)
    showToast(e?.message || '重试失败', 'error')
  } finally {
    generating.value = false
  }
}

function toggleSuggestion(item: StudySuggestion, checked: boolean) {
  if (!checked) {
    selectedSuggestionIds.value = selectedSuggestionIds.value.filter(
      (id) => id !== item.suggestion_id,
    )
    return
  }
  const otherBatch = selectedSuggestions.value.some(
    (selected) => selected.batch_id !== item.batch_id,
  )
  if (otherBatch) {
    selectedSuggestionIds.value = [item.suggestion_id]
    showToast('批量操作一次只能处理同一生成批次', 'error')
    return
  }
  if (!selectedSuggestionIds.value.includes(item.suggestion_id)) {
    selectedSuggestionIds.value = [...selectedSuggestionIds.value, item.suggestion_id]
  }
}

function toggleSuggestionEvent(item: StudySuggestion, event: Event) {
  toggleSuggestion(item, (event.target as HTMLInputElement).checked)
}

async function submitSuggestionOperations(
  batchId: string,
  items: Array<Record<string, unknown>>,
) {
  if (operating.value) return
  operating.value = true
  const signature = JSON.stringify({ batch_id: batchId, items })
  pendingSuggestionOperation = requestFor(
    pendingSuggestionOperation,
    signature,
    'study-suggestion-operation',
  )
  try {
    await api.post<StudySuggestionOperationsResponse>('/api/study/suggestions/operations', {
      request_id: pendingSuggestionOperation.request_id,
      batch_id: batchId,
      items,
    })
    pendingSuggestionOperation = null
    selectedSuggestionIds.value = []
    rejectReason.value = ''
    showToast('候选卡片已更新', 'success')
    await Promise.all([load(), loadQuality()])
  } catch (e: any) {
    if (!isAmbiguousFailure(e)) pendingSuggestionOperation = null
    if (e?.status === 409) {
      showToast('候选已变化,已刷新最新状态', 'error')
      await loadQuality()
    } else {
      showToast(e?.message || '候选操作失败', 'error')
    }
  } finally {
    operating.value = false
  }
}

async function saveSuggestion(item: StudySuggestion) {
  const draft = suggestionDrafts[item.suggestion_id]
  if (!draft || !draft.front.trim() || !draft.back.trim()) return
  await submitSuggestionOperations(item.batch_id, [{
    suggestion_id: item.suggestion_id,
    expected_revision: item.revision,
    action: 'edit',
    patch: {
      card_type: draft.card_type,
      front: draft.front.trim(),
      back: draft.back.trim(),
      explanation: draft.explanation.trim(),
      concept_term: draft.concept_term.trim() || null,
    },
  }])
}

async function bulkSuggestions(action: 'accept' | 'reject') {
  if (!selectedSuggestions.value.length || !selectedBatchId.value) return
  const reason = rejectReason.value.trim()
  if (action === 'reject' && !reason) return
  await submitSuggestionOperations(
    selectedBatchId.value,
    selectedSuggestions.value.map((item) => {
      const draft = suggestionDrafts[item.suggestion_id]
      return {
        suggestion_id: item.suggestion_id,
        expected_revision: item.revision,
        action,
        patch: action === 'accept' && draft ? {
          card_type: draft.card_type,
          front: draft.front.trim(),
          back: draft.back.trim(),
          explanation: draft.explanation.trim(),
          concept_term: draft.concept_term.trim() || null,
        } : undefined,
        reason: action === 'reject' ? reason : undefined,
      }
    }),
  )
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
    if (
      !pendingReview.value
      || pendingReview.value.card_id !== card.card_id
      || pendingReview.value.revision !== card.revision
      || pendingReview.value.grade !== grade
    ) {
      const suffix = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`
      pendingReview.value = {
        card_id: card.card_id,
        revision: card.revision,
        grade,
        request_id: `study-review:${suffix}`,
      }
    }
    await api.post('/api/study/reviews', {
      request_id: pendingReview.value.request_id,
      card_id: card.card_id,
      expected_revision: card.revision,
      grade,
    })
    pendingReview.value = null
    showToast('已记录复习', 'success')
    await load()
  } catch (e: any) {
    if (!isAmbiguousFailure(e)) {
      pendingReview.value = null
      if (e?.status === 409) await load()
    }
    showToast(e?.message || '记录失败', 'error')
  } finally {
    reviewing.value = false
  }
}

async function setStatus(card: StudyCard, status: 'active' | 'suspended' | 'rejected') {
  try {
    await api.post(`/api/study/cards/${encodeURIComponent(card.card_id)}/status`, {
      status,
      expected_revision: card.revision,
    })
    showToast(status === 'active' ? '已恢复' : status === 'suspended' ? '已暂停' : '已驳回', 'success')
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

onMounted(async () => {
  componentAlive = true
  await Promise.all([load(), loadQuality()])
  const batchId = localStorage.getItem('study_suggestion_batch_id')
  if (componentAlive && batchId) {
    const epoch = ++batchPollEpoch
    await pollBatch(epoch, batchId)
  }
})
onUnmounted(() => {
  componentAlive = false
  batchPollEpoch += 1
  clearBatchPoll()
})
</script>

<template>
  <section class="page study-page">
    <div class="study-head">
      <div>
        <div class="h1"><GraduationCap :size="19" />学习</div>
        <div class="lead">
          待复习 {{ stats.due }} · 卡片 {{ stats.total }} · 活跃 {{ activeCards }}
          <template v-if="suspendedCards"> · 暂停 {{ suspendedCards }}</template>
          <template v-if="stats.reviews_total"> · 留存 {{ Math.round(stats.retention_rate * 100) }}%</template>
        </div>
      </div>
      <div class="head-actions">
        <select v-model="selectedDomain" class="input domain-select" @change="reloadAll">
          <option value="">全部知识库</option>
          <option v-for="d in domainOptions" :key="d" :value="d">{{ d }}</option>
        </select>
        <button class="btn sm" :disabled="loading || qualityLoading" @click="reloadAll">刷新</button>
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
            <button v-else-if="card.status === 'suspended'" class="icon-btn" title="恢复" @click="setStatus(card, 'active')"><RotateCcw :size="15" /></button>
            <button v-else-if="card.status === 'suggested'" class="icon-btn danger" title="驳回" @click="setStatus(card, 'rejected')"><Trash2 :size="15" /></button>
            <button class="icon-btn danger" title="删除" @click="deleteCard(card)"><Trash2 :size="15" /></button>
          </div>
        </article>
      </div>
    </section>

    <section class="quality-section">
      <div class="suggestion-head">
        <div>
          <div class="panel-title quality-title"><Sparkles :size="16" /><span>证据型自动卡片</span></div>
          <p class="section-lead">候选只有经人工接受后才会进入复习队列。</p>
        </div>
        <div class="generate-controls">
          <label>
            数量
            <input v-model.number="maxCards" class="input suggestion-max" type="number" min="1" max="50" />
          </label>
          <button class="btn sm generate-suggestions" :disabled="generating" @click="generateSuggestions">
            <Sparkles :size="14" />生成 {{ generationDomain }} 卡片
          </button>
        </div>
      </div>

      <div v-if="currentBatch" class="batch-status" :class="`is-${currentBatch.status}`">
        <div>
          <strong>生成批次 {{ currentBatch.status }}</strong>
          <span>第 {{ currentBatch.attempt }} 次 · {{ currentBatch.suggestion_count }} 张候选</span>
          <span v-if="currentBatch.error_message" class="batch-error">{{ currentBatch.error_message }}</span>
        </div>
        <button
          v-if="currentBatch.status === 'failed'"
          class="btn sm"
          :disabled="generating"
          @click="retryBatch"
        >重试</button>
      </div>

      <div v-if="qualityError" class="state-panel compact">
        <p>{{ qualityError }}</p>
        <button class="btn sm" @click="loadQuality">重试</button>
      </div>
      <div v-else-if="qualityLoading && !suggestions.length" class="state-panel compact">加载候选中...</div>
      <div v-else-if="!suggestions.length" class="state-panel compact">暂无待审核候选</div>
      <template v-else>
        <div class="bulk-bar">
          <span>已选 {{ selectedSuggestionIds.length }} / {{ suggestions.length }}</span>
          <input v-model="rejectReason" class="input reject-reason" placeholder="批量拒绝原因" />
          <button
            class="btn sm"
            :disabled="operating || !selectedSuggestionIds.length"
            @click="bulkSuggestions('accept')"
          >接受所选</button>
          <button
            class="btn sm danger-btn"
            :disabled="operating || !selectedSuggestionIds.length || !rejectReason.trim()"
            @click="bulkSuggestions('reject')"
          >拒绝所选</button>
        </div>
        <div class="suggestion-list">
          <article v-for="item in suggestions" :key="item.suggestion_id" class="suggestion-card">
            <label class="suggestion-select">
              <input
                type="checkbox"
                :checked="selectedSuggestionIds.includes(item.suggestion_id)"
                @change="toggleSuggestionEvent(item, $event)"
              />
              <span>#{{ item.ordinal + 1 }}</span>
              <span class="pill">{{ item.domain }}</span>
              <span class="revision">revision {{ item.revision }}</span>
            </label>
            <div v-if="suggestionDrafts[item.suggestion_id]" class="suggestion-editor">
              <div class="suggestion-fields compact-fields">
                <label>
                  类型
                  <select v-model="suggestionDrafts[item.suggestion_id].card_type" class="input">
                    <option value="basic">basic</option>
                    <option value="cloze">cloze</option>
                    <option value="qa">qa</option>
                  </select>
                </label>
                <label>
                  概念
                  <input v-model="suggestionDrafts[item.suggestion_id].concept_term" class="input" />
                </label>
              </div>
              <label>正面<textarea v-model="suggestionDrafts[item.suggestion_id].front" class="input" rows="2" /></label>
              <label>背面<textarea v-model="suggestionDrafts[item.suggestion_id].back" class="input" rows="2" /></label>
              <label>解释<textarea v-model="suggestionDrafts[item.suggestion_id].explanation" class="input" rows="2" /></label>
            </div>
            <details class="evidence-preview">
              <summary>证据 {{ item.evidence.length }} 条</summary>
              <blockquote
                v-for="evidence in item.evidence"
                :key="evidence.evidence_id"
                :class="{ invalid: evidence.status !== 'valid' }"
              >
                <div>{{ evidence.quote }}</div>
                <small>{{ evidence.title }} · {{ evidence.section || evidence.note_type }} · {{ evidence.status }}</small>
              </blockquote>
            </details>
            <div class="suggestion-actions">
              <button
                class="btn sm save-suggestion"
                :disabled="operating || !suggestionDrafts[item.suggestion_id]?.front.trim() || !suggestionDrafts[item.suggestion_id]?.back.trim()"
                @click="saveSuggestion(item)"
              >保存编辑</button>
            </div>
          </article>
        </div>
      </template>
    </section>

    <section class="mastery-section">
      <div class="panel-title"><GraduationCap :size="16" /><span>概念掌握度</span><span class="dim">{{ mastery.length }}</span></div>
      <p v-if="!mastery.length" class="section-lead">完成真实复习后，这里会按概念汇总最近评分。</p>
      <div v-else class="mastery-list">
        <article v-for="item in mastery" :key="`${item.domain}:${item.concept_term}`" class="mastery-row">
          <div>
            <strong>{{ item.concept_term }}</strong>
            <span>{{ item.domain }} · {{ item.reviewed_cards }} 张卡 · {{ item.reviews_total }} 次复习</span>
          </div>
          <div class="mastery-score" :class="`is-${item.level}`">
            <strong>{{ item.score }}</strong><span>{{ item.level }}</span>
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
.quality-section, .mastery-section {
  margin-top: 14px; padding: 16px; border: 1px solid var(--line-soft);
  border-radius: var(--r-sm); background: var(--surface);
}
.suggestion-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.quality-title { margin-bottom: 2px; }
.section-lead { margin: 0; color: var(--ink-500); font-size: 13px; line-height: 1.5; }
.generate-controls { display: flex; align-items: flex-end; gap: 8px; }
.generate-controls label, .suggestion-editor label {
  display: grid; gap: 5px; color: var(--ink-500); font-size: 12px; font-weight: 700;
}
.suggestion-max { width: 74px; }
.batch-status {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: 14px; padding: 10px 12px; border-radius: var(--r-sm);
  background: var(--brand-50); color: var(--ink-700);
}
.batch-status > div { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.batch-status span { font-size: 12px; color: var(--ink-500); }
.batch-status.is-failed { background: #fff7ed; }
.batch-error { color: #c2410c !important; }
.bulk-bar {
  display: flex; align-items: center; gap: 8px; margin-top: 14px; padding: 9px 10px;
  border-radius: var(--r-sm); background: var(--bg-soft, #f6f7f9); color: var(--ink-600); font-size: 13px;
}
.reject-reason { flex: 1; min-width: 180px; }
.danger-btn { color: #b91c1c; }
.suggestion-list { display: grid; gap: 10px; margin-top: 10px; }
.suggestion-card { padding: 12px; border: 1px solid var(--line-soft); border-radius: var(--r-sm); }
.suggestion-select { display: flex; align-items: center; gap: 7px; color: var(--ink-700); font-size: 12px; font-weight: 700; }
.suggestion-select input { accent-color: var(--brand-600); }
.revision { margin-left: auto; color: var(--ink-400); font-weight: 500; }
.suggestion-editor { display: grid; gap: 9px; margin-top: 10px; }
.suggestion-fields { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 9px; }
.suggestion-editor textarea { resize: vertical; }
.evidence-preview { margin-top: 10px; color: var(--ink-600); font-size: 12px; }
.evidence-preview summary { cursor: pointer; font-weight: 700; }
.evidence-preview blockquote {
  margin: 8px 0 0; padding: 8px 10px; border-left: 2px solid var(--brand-200);
  background: var(--brand-50); color: var(--ink-700); line-height: 1.5;
}
.evidence-preview blockquote.invalid { border-color: #f59e0b; background: #fff7ed; }
.evidence-preview small { display: block; margin-top: 4px; color: var(--ink-400); }
.suggestion-actions { display: flex; justify-content: flex-end; margin-top: 10px; }
.mastery-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.mastery-row {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; border: 1px solid var(--line-soft); border-radius: var(--r-sm);
}
.mastery-row > div:first-child { display: grid; gap: 4px; min-width: 0; }
.mastery-row > div:first-child span { color: var(--ink-400); font-size: 12px; }
.mastery-score { display: grid; justify-items: end; color: #b45309; }
.mastery-score strong { font-size: 20px; }
.mastery-score span { font-size: 11px; }
.mastery-score.is-mastered { color: #047857; }
.mastery-score.is-learning { color: var(--brand-700); }

@media (max-width: 900px) {
  .study-head { flex-direction: column; }
  .head-actions { width: 100%; }
  .domain-select { flex: 1; width: auto; }
  .study-grid { grid-template-columns: 1fr; }
  .suggestion-head { flex-direction: column; }
  .generate-controls { width: 100%; }
  .generate-suggestions { flex: 1; justify-content: center; }
  .mastery-list { grid-template-columns: 1fr; }
}

@media (max-width: 560px) {
  .review-card h2 { font-size: 20px; }
  .grade-row, .form-row { grid-template-columns: 1fr 1fr; }
  .card-row { grid-template-columns: 1fr; }
  .row-actions { justify-content: flex-start; }
  .bulk-bar { align-items: stretch; flex-direction: column; }
  .reject-reason { min-width: 0; width: 100%; }
  .suggestion-fields { grid-template-columns: 1fr; }
  .generate-controls { align-items: stretch; flex-direction: column; }
  .suggestion-max { width: 100%; }
}
</style>
