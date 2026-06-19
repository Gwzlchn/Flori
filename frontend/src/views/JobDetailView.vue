<script setup lang="ts">
import { ref, computed, onMounted, watch, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useJobStore } from '../stores/jobs'
import { useJobWs } from '../composables/useJobWs'
import MarkdownViewer from '../components/notes/MarkdownViewer.vue'
import StepWorkbench from '../components/job/StepWorkbench.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import { fmtDateTime } from '../utils/datetime'
import { CONTENT_TYPE_LABELS } from '../types'
import type { JobDetail, GlossaryTerm, JobConcept } from '../types'
import {
  Play, FileText, Newspaper, Headphones, ExternalLink, BookOpen, Lightbulb,
  GitBranch, Info, RefreshCw, ChevronDown, Star, List, RotateCcw, Trash2,
  AlertTriangle, ChevronRight, Bookmark,
} from 'lucide-vue-next'

// 内容详情(原型 #detail)：头部 + 4 tab(笔记/概念/流水线/元信息)。
// 完成态(done)默认落「笔记」，未完成默认「流水线」。
// 吸收旧 JobDetailView(头/ws/步骤/重试重跑删除) + NotesView(版本/评审/采纳/换 provider 重跑)。
const route = useRoute()
const router = useRouter()
const api = useApi()
const jobStore = useJobStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const jobId = computed(() => String(route.params.id))
const { steps, jobStatus, connected, setInitialSteps } = useJobWs(jobId)

const job = ref<JobDetail | null>(null)
const loading = ref(true)
const loadError = ref('')

// ── tab ──
type Tab = 'notes' | 'concepts' | 'proc' | 'info'
const tab = ref<Tab>('proc')
const TABS: { key: Tab; label: string; icon: any }[] = [
  { key: 'notes', label: '笔记', icon: BookOpen },
  { key: 'concepts', label: '概念', icon: Lightbulb },
  { key: 'proc', label: '流水线', icon: GitBranch },
  { key: 'info', label: '元信息', icon: Info },
]

// 头部派生
const typeIcon = computed(() => {
  const m: Record<string, any> = { video: Play, paper: FileText, article: Newspaper, audio: Headphones }
  return m[job.value?.content_type || ''] || FileText
})
const typeClass = computed(() => {
  const m: Record<string, string> = { video: 't-video', paper: 't-paper', article: 't-article', audio: 't-audio' }
  return m[job.value?.content_type || ''] || 't-video'
})
const SOURCE_LABELS: Record<string, string> = {
  bilibili: 'Bilibili', youtube: 'YouTube', arxiv: 'arXiv',
  http_article: '公众号', podcast: '播客', upload: '本地', other: '其它',
}
const sourceLabel = computed(() => {
  const s = job.value?.source
  return s ? (SOURCE_LABELS[s] || s) : '—'
})
// BV 号(B 站)
const bv = computed(() => jobId.value.match(/_(BV[0-9A-Za-z]+)/)?.[1] ?? null)

// 生成时间窗口(由步骤起止推导)。
function fmtDur(sec: number | null): string {
  if (sec == null) return '—'
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60)
  return h ? `${h}h${m}m` : m ? `${m}m${s}s` : `${s}s`
}
const anyRunning = computed(() => steps.value.some(s => s.status === 'running'))
const genStart = computed(() => {
  const t = steps.value.map(s => s.started_at).filter(Boolean).map(x => +new Date(x as string))
  return t.length ? Math.min(...t) : null
})
const genEnd = computed(() => {
  if (anyRunning.value) return null
  const t = steps.value.map(s => s.finished_at).filter(Boolean).map(x => +new Date(x as string))
  return t.length ? Math.max(...t) : null
})
const genDurSec = computed(() => (genStart.value && genEnd.value ? (genEnd.value - genStart.value) / 1000 : null))

// 集合(元信息)：collection_name 由后端 collection_id join 出,无归属/已删为 null;以名为主、id 备查。
const collectionId = computed(() => job.value?.collection_id ?? null)
const collectionName = computed(() => job.value?.collection_name ?? null)

async function fetchDetail() {
  loading.value = true
  loadError.value = ''
  try {
    const d = await jobStore.fetchDetail(jobId.value)
    job.value = d
    jobStatus.value = d.status
    setInitialSteps(d.steps)
    // 完成态默认落笔记，否则落流水线。
    tab.value = d.status === 'done' ? 'notes' : 'proc'
  } catch (e: any) {
    loadError.value = e?.status === 404 ? '内容不存在或已删除' : (e?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(fetchDetail)
watch(jobId, fetchDetail)

// ════════════════════ 笔记 tab ════════════════════
const domain = computed(() => job.value?.domain || '')
const isMechanical = ref(false)
const noteContent = ref('')
const noteLoading = ref(false)
const noteError = ref('')
const headings = ref<{ id: string; text: string; level: number }[]>([])
const terms = ref<string[]>([])    // 已采纳术语(供正文术语链接 + 采纳去重)

type Version = { provider: string; model: string; version: string; file: string; review_file: string | null; overall: number | null }
const versions = ref<Version[]>([])
const activeFile = ref<string | null>(null)

type Provider = { name: string; type: string; available: boolean; label: string }
const providers = ref<Provider[]>([])
const showRerun = ref(false)
const rerunning = ref(false)
const pendingProvider = ref<Provider | null>(null)

// 评审
const review = ref<Record<string, any> | null>(null)
const DIM_LABELS: Record<string, string> = {
  completeness: '完整性', accuracy: '准确性', structure: '结构', terminology: '概念',
  visual_integration: '配图', readability: '可读性', formula_integrity: '公式',
  figure_references: '图表引用',
}
const reviewDims = computed(() => {
  const r = review.value || {}
  return Object.entries(r)
    .filter(([k, v]) => typeof v === 'number' && k !== 'overall')
    .map(([k, v]) => ({ label: DIM_LABELS[k] || k, score: v as number }))
})
const keyTerms = computed(() => {
  const raw = review.value?.key_terms
  if (!Array.isArray(raw)) return [] as { term: string; definition: string }[]
  return raw
    .map((t: any) => typeof t === 'string'
      ? { term: t, definition: '' }
      : { term: String(t?.term ?? ''), definition: String(t?.definition ?? '') })
    .filter((t) => t.term.trim())
})

async function loadTerms() {
  if (!domain.value) { terms.value = []; return }
  try {
    const ts = await api.get<GlossaryTerm[]>(`/api/glossary?domain=${encodeURIComponent(domain.value)}&status=accepted`)
    terms.value = ts.map(t => t.term)
  } catch { terms.value = [] }
}

async function loadVersions() {
  if (isMechanical.value) { versions.value = []; return }
  try {
    const r = await api.get<{ versions: Version[] }>(`/api/jobs/${jobId.value}/note-versions`)
    versions.value = r.versions || []
  } catch { versions.value = [] }
}

async function loadProviders() {
  try {
    const r = await api.get<{ providers: Provider[] }>(`/api/providers`)
    providers.value = r.providers || []
  } catch { providers.value = [] }
}

async function loadNote() {
  noteLoading.value = true
  noteError.value = ''
  try {
    const base = isMechanical.value
      ? `/api/jobs/${jobId.value}/notes/mechanical`
      : `/api/jobs/${jobId.value}/notes/smart`
    const url = (!isMechanical.value && activeFile.value)
      ? `${base}?file=${encodeURIComponent(activeFile.value)}`
      : base
    noteContent.value = await api.getText(url)
  } catch (e: any) {
    noteError.value = e?.status === 404 ? '笔记尚未生成' : (e?.message || '加载失败')
    noteContent.value = ''
  } finally {
    noteLoading.value = false
  }
}

async function loadReview() {
  review.value = null
  if (isMechanical.value) return
  const v = versions.value.find(x => x.file === activeFile.value) || versions.value[0]
  const url = v?.review_file
    ? `/api/jobs/${jobId.value}/review?file=${encodeURIComponent(v.review_file)}`
    : `/api/jobs/${jobId.value}/review`
  try { review.value = await api.get<Record<string, any>>(url) } catch { review.value = null }
}

let notesInit = false
async function ensureNotes() {
  if (notesInit) return
  notesInit = true
  await loadTerms()
  await Promise.all([loadVersions(), loadProviders()])
  await Promise.all([loadNote(), loadReview()])
}

async function switchVariant(mech: boolean) {
  if (isMechanical.value === mech) return
  isMechanical.value = mech
  activeFile.value = null
  await loadVersions()
  await Promise.all([loadNote(), loadReview()])
}

async function selectVersion(file: string | null) {
  activeFile.value = file
  await Promise.all([loadNote(), loadReview()])
}
function verLabel(v: Version): string {
  const m = v.version.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/)
  return m ? `${m[2]}/${m[3]} ${m[4]}:${m[5]}` : v.version
}

function rerunWith(p: Provider) {
  if (!p.available || rerunning.value) return
  showRerun.value = false
  pendingProvider.value = p
}
async function confirmRerun() {
  const p = pendingProvider.value
  pendingProvider.value = null
  if (!p) return
  rerunning.value = true
  try {
    await api.post(`/api/jobs/${jobId.value}/rerun-smart`, { provider: p.name })
    showToast(`已用 ${p.name} 开始重跑，完成后会出现新版本`, 'success')
    pollForVersion(p.name)
  } catch (e: any) {
    showToast(e?.message || '重跑失败', 'error')
    rerunning.value = false
  }
}
function pollForVersion(provider: string) {
  let n = 0
  const timer = setInterval(async () => {
    n++
    await loadVersions()
    const got = versions.value.find(v => v.provider === provider)
    if (got || n > 48) {
      clearInterval(timer)
      rerunning.value = false
      if (got) { showToast(`${provider} 版本已生成`, 'success'); await selectVersion(got.file) }
    }
  }, 15000)
}

async function acceptKeyTerm(term: string, definition: string) {
  if (!domain.value || terms.value.includes(term)) return
  try {
    try {
      await api.post(`/api/glossary/${encodeURIComponent(domain.value)}/${encodeURIComponent(term)}/accept`)
    } catch (e: any) {
      if (e?.status === 404) {
        await api.post(`/api/glossary?domain=${encodeURIComponent(domain.value)}`, { term, definition: definition || null })
      } else { throw e }
    }
    terms.value.push(term)
    showToast(`已采纳「${term}」`, 'success')
  } catch (e: any) {
    showToast(e?.message || '采纳失败', 'error')
  }
}

// ════════════════════ 概念 tab ════════════════════
// 直查 GET /api/jobs/{id}/concepts:每项是 GlossaryTerm,另含 job_occurrences(本内容里的命中位置)。
const conceptsLoading = ref(false)
const conceptsError = ref('')
const jobConcepts = ref<JobConcept[]>([])
let conceptsInit = false

async function ensureConcepts() {
  if (conceptsInit) return
  conceptsInit = true
  await loadConcepts()
}
async function loadConcepts() {
  conceptsLoading.value = true
  conceptsError.value = ''
  try {
    const list = await jobStore.fetchConcepts(jobId.value)
    // 已采纳优先、全库佐证多优先。
    jobConcepts.value = [...list].sort(
      (a, b) =>
        (Number(b.status === 'accepted') - Number(a.status === 'accepted')) ||
        ((b.occurrences?.length ?? 0) - (a.occurrences?.length ?? 0)),
    )
  } catch (e: any) {
    conceptsError.value = e?.status === 404 ? '内容不存在或已删除' : (e?.message || '加载失败')
    jobConcepts.value = []
  } finally {
    conceptsLoading.value = false
  }
}
// 本内容里命中的位置(逐个出现处),用 location/content_type 描述。
function occLabel(o: { content_type: string; location: string | null }): string {
  const t = CONTENT_TYPE_LABELS[o.content_type] || o.content_type
  return o.location ? `${t} · ${o.location}` : t
}
function conceptOccText(c: JobConcept): string {
  const occs = c.job_occurrences ?? []
  if (!occs.length) return ''
  return occs.map(occLabel).join(' / ')
}
function goConcept(c: JobConcept) {
  router.push(`/kb/${encodeURIComponent(c.domain)}/concepts/${encodeURIComponent(c.term)}`)
}

// ════════════════════ 流水线 tab ════════════════════
const rerunStep = ref('')
async function retryJob() {
  try {
    await jobStore.retryJob(jobId.value)
    showToast('已提交重试', 'success')
    jobStatus.value = 'processing'
  } catch (e: any) { showToast(e?.message || '重试失败', 'error') }
}
async function rerunFromStep() {
  if (!rerunStep.value) return
  try {
    await jobStore.rerunJob(jobId.value, rerunStep.value)
    showToast(`从 ${rerunStep.value} 开始重跑`, 'success')
    jobStatus.value = 'processing'
    rerunStep.value = ''
  } catch (e: any) { showToast(e?.message || '重跑失败', 'error') }
}

// ════════════════════ 删除 ════════════════════
const showDelete = ref(false)
async function confirmDelete() {
  try {
    await jobStore.deleteJob(jobId.value)
    showToast('已删除', 'success')
    router.push('/content')
  } catch (e: any) {
    showToast(e?.message || '删除失败', 'error')
  }
  showDelete.value = false
}

// 切到对应 tab 时再懒加载其数据。
watch(tab, (t) => {
  if (t === 'notes') ensureNotes()
  else if (t === 'concepts') ensureConcepts()
})
// 详情就绪后若初始 tab 即笔记/概念，触发懒加载。
watch(job, (j) => {
  if (!j) return
  if (tab.value === 'notes') ensureNotes()
  else if (tab.value === 'concepts') ensureConcepts()
})
</script>

<template>
  <div class="page wide">
    <!-- 加载态 -->
    <div v-if="loading" class="card pad">
      <div class="state"><span class="spinner" />加载中…</div>
    </div>

    <!-- 错误态 -->
    <div v-else-if="loadError" class="card pad">
      <div class="state">
        <Info class="big" />
        <div class="t">{{ loadError }}</div>
        <div style="display:flex;gap:8px">
          <button class="btn" @click="fetchDetail"><RotateCcw :size="14" />重试</button>
          <button class="btn" @click="router.push('/content')">返回所有来源</button>
        </div>
      </div>
    </div>

    <template v-else-if="job">
      <!-- ── 头部 ── -->
      <div class="card pad" style="margin-bottom:16px">
        <div style="display:flex;align-items:flex-start;gap:13px">
          <span class="type-pill" :class="typeClass" style="width:42px;height:42px">
            <component :is="typeIcon" />
          </span>
          <div style="flex:1;min-width:0">
            <div class="h1 sm" style="overflow:hidden;text-overflow:ellipsis">{{ job.title || job.job_id }}</div>
            <div class="meta" style="margin-top:5px">
              <StatusBadge :status="jobStatus" />
              <span class="badge b-mut">{{ CONTENT_TYPE_LABELS[job.content_type] || job.content_type }}</span>
              <span>{{ sourceLabel }}</span>
              <template v-if="job.domain">
                <span class="sep">·</span><span>{{ job.domain }}</span>
              </template>
              <template v-if="bv">
                <span class="sep">·</span><span class="mono dim">{{ bv }}</span>
              </template>
              <template v-if="job.url">
                <span class="sep">·</span>
                <a class="ghost" :href="job.url" target="_blank" rel="noopener" style="color:var(--info)">原始链接<ExternalLink :size="13" /></a>
              </template>
            </div>
            <div class="dim" style="font-size:12px;margin-top:4px">
              上传于 {{ fmtDateTime(job.published_at) }} · 生成
              {{ genStart ? fmtDateTime(genStart) : '—' }} →
              {{ anyRunning ? '进行中' : (genEnd ? fmtDateTime(genEnd) : '—') }}
              · 耗时 {{ genEnd ? fmtDur(genDurSec) : '—' }}
            </div>
            <div v-if="jobStatus === 'processing'" class="dim" style="font-size:11.5px;margin-top:4px;display:flex;align-items:center;gap:6px">
              <span class="dot" :class="connected ? 'd-ok pulse' : 'd-bad'" />
              {{ connected ? '实时更新中' : '连接断开，重连中…' }}
            </div>
          </div>
        </div>
      </div>

      <!-- ── tabs ── -->
      <div class="tabs">
        <button v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="tab = t.key">
          <component :is="t.icon" :size="15" />{{ t.label }}
        </button>
      </div>

      <!-- ════ 笔记 ════ -->
      <div v-show="tab === 'notes'">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap">
          <div class="seg">
            <button :class="{ on: !isMechanical }" @click="switchVariant(false)">智能版</button>
            <button :class="{ on: isMechanical }" @click="switchVariant(true)">机械版</button>
          </div>

          <template v-if="!isMechanical">
            <span class="dim" style="font-size:12px;margin-left:6px">版本</span>
            <span v-if="versions.length === 0" class="chip on" style="cursor:default">默认</span>
            <span
              v-for="v in versions" :key="v.file"
              class="chip" :class="{ on: (activeFile ?? versions[0]?.file) === v.file }"
              style="max-width:240px" @click="selectVersion(v.file)"
            >
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ v.provider }}/{{ v.model }} · {{ verLabel(v) }}</span>
              <template v-if="v.overall != null"><Star :size="11" style="color:var(--amber)" />{{ v.overall }}</template>
            </span>

            <!-- 换 provider 重跑 -->
            <div style="position:relative;margin-left:auto">
              <button class="btn sm" :disabled="rerunning" @click="showRerun = !showRerun">
                <RefreshCw :size="13" :class="rerunning ? 'pulse' : ''" />
                {{ rerunning ? '生成中…' : '换 provider 重跑' }}
                <ChevronDown :size="13" />
              </button>
              <div
                v-if="showRerun"
                class="card"
                style="position:absolute;right:0;top:calc(100% + 6px);width:200px;z-index:30;padding:5px;box-shadow:var(--sh-lg)"
              >
                <button
                  v-for="p in providers" :key="p.name"
                  class="iconbtn" :disabled="!p.available"
                  style="width:100%;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 9px;border-radius:var(--r-sm);font-size:12px;text-align:left"
                  :style="!p.available ? 'opacity:.5;cursor:not-allowed' : ''"
                  @click="rerunWith(p)"
                >
                  <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ p.name }} <span class="dim">({{ p.label }})</span></span>
                  <span v-if="!p.available" class="dim" style="font-size:11px;flex:none">无 key</span>
                </button>
                <div v-if="providers.length === 0" class="dim" style="font-size:12px;padding:8px 9px">无可用 provider</div>
              </div>
            </div>
          </template>
        </div>

        <!-- 质量评审面板 -->
        <div v-if="!isMechanical && review" class="review" style="margin-bottom:16px">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <b style="font-size:13.5px;color:var(--ink-900)">质量评审</b>
            <span v-if="review.overall != null" class="badge b-warn"><Star :size="12" />{{ review.overall }} / 5</span>
            <span class="dim" style="font-size:12px">
              {{ review.provider }}<template v-if="review.model"> / {{ review.model }}</template>
              <template v-if="review.generated_at"> · {{ review.generated_at }}</template>
            </span>
          </div>
          <div v-if="reviewDims.length" class="dims">
            <div v-for="d in reviewDims" :key="d.label" class="dim-g">
              <div class="row-l">{{ d.label }}<b>{{ d.score }}</b></div>
              <div class="track"><span :style="{ width: Math.max(0, Math.min(100, d.score * 20)) + '%' }" /></div>
            </div>
          </div>
          <div v-if="review.missing_concepts?.length" style="font-size:12.5px;color:var(--ink-600);margin-bottom:6px">
            <span class="dim">缺失概念：</span>{{ review.missing_concepts.join(' / ') }}
          </div>
          <div v-if="review.top3_improvements?.length" style="font-size:12.5px;color:var(--ink-600);margin-bottom:6px">
            <span class="dim">改进建议：</span>
            <ol style="margin:4px 0 0 18px">
              <li v-for="(t, i) in review.top3_improvements" :key="i">{{ t }}</li>
            </ol>
          </div>
          <div v-if="keyTerms.length" style="font-size:12.5px;color:var(--ink-600)">
            <span class="dim">已讲清的概念（可采纳）：</span>
            <span v-for="kt in keyTerms" :key="kt.term" style="display:inline-flex;align-items:center;margin:0 6px 4px 0">
              <b style="color:var(--ink-900)">{{ kt.term }}</b>
              <button
                class="btn sm" style="padding:2px 8px;margin-left:6px"
                :disabled="terms.includes(kt.term)"
                :style="terms.includes(kt.term) ? 'color:var(--ok);border-color:var(--ok-bd)' : ''"
                @click="acceptKeyTerm(kt.term, kt.definition)"
              >{{ terms.includes(kt.term) ? '✓ 已采纳' : '采纳' }}</button>
            </span>
          </div>
        </div>

        <!-- 正文 + 章节 -->
        <div v-if="noteLoading" class="card pad"><div class="state"><span class="spinner" />加载笔记…</div></div>
        <div v-else-if="noteError" class="card pad">
          <div class="state"><FileText class="big" /><div class="t">{{ noteError }}</div></div>
        </div>
        <div v-else class="notes-wrap">
          <div class="card pad prose">
            <MarkdownViewer
              :content="noteContent" :job-id="jobId" :terms="terms" :domain="domain"
              @headings="headings = $event"
            />
          </div>
          <nav v-if="headings.length" class="toc">
            <div class="seclabel"><List :size="14" />章节</div>
            <a
              v-for="h in headings" :key="h.id"
              :href="`#${h.id}`" :class="{ sub: h.level >= 3 }"
            >{{ h.text }}</a>
          </nav>
        </div>
      </div>

      <!-- ════ 概念 ════ -->
      <div v-show="tab === 'concepts'">
        <div class="card pad">
          <div class="card-h"><Lightbulb :size="15" />本内容涉及的概念<template v-if="jobConcepts.length"> · {{ jobConcepts.length }}</template></div>
          <p class="lead" style="margin:-6px 0 12px">这条内容里命中的概念。点进去可反查它在整个知识库里——还有哪些内容也讲过它。</p>

          <div v-if="conceptsLoading" class="state"><span class="spinner" />加载概念…</div>
          <div v-else-if="conceptsError" class="state"><Lightbulb class="big" /><div class="t">{{ conceptsError }}</div>
            <button class="btn" @click="loadConcepts"><RotateCcw :size="14" />重试</button></div>
          <div v-else-if="jobConcepts.length === 0" class="state"><Lightbulb class="big" /><div class="t">这条内容暂未关联任何概念</div></div>
          <div v-else>
            <div v-for="c in jobConcepts" :key="c.term" class="concept" @click="goConcept(c)">
              <Bookmark v-if="c.is_topic" class="pin" />
              <span v-else style="width:14px;flex:none" />
              <div style="flex:1;min-width:0">
                <div class="t">
                  {{ c.term }}
                  <span v-if="c.is_topic" class="badge b-brand" style="margin-left:4px">主题概念</span>
                </div>
                <div v-if="c.definition" class="d" style="white-space:normal">{{ c.definition }}</div>
                <div class="d">
                  <template v-if="conceptOccText(c)">本内容 {{ conceptOccText(c) }} · </template>全库 {{ c.occurrences?.length ?? 0 }} 条内容讲过
                </div>
              </div>
              <StatusBadge :status="c.status" />
              <ChevronRight :size="15" class="dim" style="flex:none" />
            </div>
          </div>
        </div>
      </div>

      <!-- ════ 流水线 ════ -->
      <div v-show="tab === 'proc'">
        <StepWorkbench :job-id="jobId" :steps="steps" />

        <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center">
          <button v-if="jobStatus === 'failed'" class="btn pri" @click="retryJob"><RotateCcw :size="14" />重试</button>
          <template v-if="jobStatus === 'done' || jobStatus === 'failed'">
            <select v-model="rerunStep" class="input" style="max-width:220px">
              <option value="">从步骤重跑…</option>
              <option v-for="s in steps" :key="s.name" :value="s.name">{{ s.label || s.name }}</option>
            </select>
            <button class="btn" :disabled="!rerunStep" @click="rerunFromStep"><Play :size="14" />重跑</button>
          </template>
        </div>
      </div>

      <!-- ════ 元信息 ════ -->
      <div v-show="tab === 'info'">
        <div class="card pad" style="max-width:560px">
          <div class="card-h"><Info :size="15" />元信息</div>
          <table class="kv">
            <tr><td>标题</td><td>{{ job.title || '—' }}</td></tr>
            <tr><td>类型</td><td>{{ CONTENT_TYPE_LABELS[job.content_type] || job.content_type }}</td></tr>
            <tr><td>来源</td><td>{{ sourceLabel }}</td></tr>
            <tr><td>知识库</td><td>{{ job.domain || '—' }}</td></tr>
            <tr><td>集合</td><td>
              <template v-if="collectionName">
                {{ collectionName }}
                <span v-if="collectionId" class="mono dim" style="font-size:11.5px;margin-left:6px">{{ collectionId }}</span>
              </template>
              <span v-else class="dim">未归集合</span>
            </td></tr>
            <tr v-if="bv"><td>BV 号</td><td class="mono">{{ bv }}</td></tr>
            <tr v-if="job.url"><td>原始链接</td><td>
              <a class="ghost" :href="job.url" target="_blank" rel="noopener" style="color:var(--info)">{{ job.url }}<ExternalLink :size="13" /></a>
            </td></tr>
            <tr><td>状态</td><td><StatusBadge :status="jobStatus" /></td></tr>
            <tr><td>上传于</td><td>{{ fmtDateTime(job.published_at) }}</td></tr>
            <tr><td>创建于</td><td>{{ fmtDateTime(job.created_at) }}</td></tr>
            <tr v-if="job.updated_at"><td>更新于</td><td>{{ fmtDateTime(job.updated_at) }}</td></tr>
            <tr><td>生成耗时</td><td>{{ genEnd ? fmtDur(genDurSec) : (anyRunning ? '进行中' : '—') }}</td></tr>
          </table>
          <div style="margin-top:16px;display:flex;gap:8px">
            <button v-if="jobStatus === 'failed'" class="btn" @click="retryJob"><RotateCcw :size="14" />重新提交</button>
            <button class="btn danger" @click="showDelete = true"><Trash2 :size="14" />删除内容</button>
          </div>
        </div>
      </div>
    </template>

    <!-- 删除确认 -->
    <div v-if="showDelete" class="overlay show confirm" @click.self="showDelete = false">
      <div class="modal">
        <div class="hd">
          <span class="danger-ic"><AlertTriangle :size="18" /></span>
          <b>删除内容</b>
        </div>
        <div class="bd" style="font-size:13.5px;color:var(--ink-700)">确定删除此内容及所有产物？此操作不可恢复。</div>
        <div class="ft">
          <button class="btn" @click="showDelete = false">取消</button>
          <button class="btn danger" @click="confirmDelete"><Trash2 :size="14" />删除</button>
        </div>
      </div>
    </div>
  </div>
</template>
