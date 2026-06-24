<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useJobStore } from '../stores/jobs'
import { useGlobalStore } from '../stores/global'
import { useJobWs } from '../composables/useJobWs'
import MarkdownViewer from '../components/notes/MarkdownViewer.vue'
import StepWorkbench from '../components/job/StepWorkbench.vue'
import PipelineDag from '../components/PipelineDag.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import { fmtDateTime, fmtDuration } from '../utils/datetime'
import { contentTypeIcon, contentTypePill, contentTypeLabel } from '../utils/contentType'
import { jobSourceLabel } from '../constants/sources'
import type { JobDetail, GlossaryTerm, JobConcept } from '../types'
import {
  Play, FileText, ExternalLink, BookOpen, Lightbulb,
  GitBranch, Info, RefreshCw, ChevronDown, Star, List, RotateCcw, Trash2,
  AlertTriangle, ChevronRight, Bookmark, ShieldCheck,
} from 'lucide-vue-next'

// 内容详情(原型 #detail)：头部 + 4 tab(笔记/概念/流水线/元信息)。
// 完成态(done)默认落「笔记」，未完成默认「流水线」。
// 吸收旧 JobDetailView(头/ws/步骤/重试重跑删除) + NotesView(版本/评审/采纳/换 provider 重跑)。
const route = useRoute()
const router = useRouter()
const api = useApi()
const jobStore = useJobStore()
const global = useGlobalStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const jobId = computed(() => String(route.params.id))
const { steps, jobStatus, connected, setInitialSteps } = useJobWs(jobId)

// 每个 job 的 DAG:流水线定义(含 needs)按 content_type 匹配 /api/pipelines,叠加各步实时状态着色。
const pipelinesDef = ref<{ name: string; steps: { key: string; label: string | null; pool: string | null; needs: string[] }[] }[]>([])
const jobDagSteps = computed(() => pipelinesDef.value.find(p => p.name === job.value?.content_type)?.steps || [])
const stepStatusByKey = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  for (const s of steps.value) m[s.name] = s.status
  return m
})
// DAG 与工作台共享的选中步(点 DAG 节点即选)。默认:运行中→失败→最后完成→末步;用户点选后不被刷新覆盖。
const selectedStep = ref('')
watch(steps, (s) => {
  if (selectedStep.value && s.some(x => x.name === selectedStep.value)) return
  if (!s.length) return
  const pick = s.find(x => x.status === 'running') || s.find(x => x.status === 'failed')
    || [...s].reverse().find(x => x.status === 'done') || s[s.length - 1]
  selectedStep.value = pick.name
}, { immediate: true })

const job = ref<JobDetail | null>(null)
const loading = ref(true)
const loadError = ref('')

// ── tab ──
type Tab = 'notes' | 'concepts' | 'proc' | 'info' | 'evidence'
const tab = ref<Tab>('proc')
const TABS: { key: Tab; label: string; icon: any }[] = [
  { key: 'notes', label: '笔记', icon: BookOpen },
  { key: 'concepts', label: '概念', icon: Lightbulb },
  { key: 'proc', label: '流水线', icon: GitBranch },
  { key: 'info', label: '元信息', icon: Info },
]

// 头部派生:内容类型图标/配色、来源标签统一走共享单一来源(utils/contentType、constants/sources)。
const typeIcon = computed(() => contentTypeIcon(job.value?.content_type))
const typeClass = computed(() => contentTypePill(job.value?.content_type))
const sourceLabel = computed(() => jobSourceLabel(job.value?.source))
// BV 号(B 站)
const bv = computed(() => jobId.value.match(/_(BV[0-9A-Za-z]+)/)?.[1] ?? null)

// 码率:kbps,≥1000 转 Mbps。
function fmtBitrate(kbps?: number): string {
  if (kbps == null) return '—'
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbps` : `${kbps} kbps`
}

// 原始文件大小:字节优先(精确,可转 KB);无字节时回退 MB。
function fmtSize(media: { file_size_bytes?: number; file_size_mb?: number }): string {
  let b = media.file_size_bytes
  if (b == null && media.file_size_mb != null) b = media.file_size_mb * 1048576
  if (b == null) return '—'
  if (b < 1024) return `${b} B`
  const u = ['KB', 'MB', 'GB', 'TB']
  let v = b / 1024, i = 0
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`
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
    // 面包屑用真实内容:知识库 / 领域 / 标题(替代通用「所有来源 / 内容详情」)。
    global.setCrumbs([
      { t: '知识库', to: '/' },
      ...(d.domain ? [{ t: d.domain, to: `/kb/${encodeURIComponent(d.domain)}` }] : []),
      { t: d.title || jobId.value },
    ])
    setInitialSteps(d.steps)
    loadEvidence()  // 权威来源(取证产物);有则显示「权威来源」tab
    // 本 job DAG 的依赖(needs)定义(/api/pipelines 返回 {pipelines:[...]});失败留空不影响详情。
    api.get<{ pipelines?: any[] }>('/api/pipelines').then(r => { pipelinesDef.value = Array.isArray(r) ? r : (r?.pipelines ?? []) }).catch(() => {})
    // 完成态默认落笔记，否则落流水线。
    tab.value = d.status === 'done' ? 'notes' : 'proc'
  } catch (e: any) {
    loadError.value = e?.status === 404 ? '内容不存在或已删除' : (e?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(fetchDetail)
watch(jobId, () => { stopPolling(); fetchDetail() })   // 切 job 先停旧轮询
onBeforeUnmount(() => { global.setCrumbs(null); stopPolling() })   // 离开详情页清面包屑覆盖 + 停轮询

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

// ════════════════════ 权威来源(evidence) tab ════════════════════
// 取证产物 evidence.json：案例类笔记 AI fetch 的判决/处罚/报道来源。有则显示 tab，404 即无。
const evidence = ref<any | null>(null)
const hasEvidence = computed(() => !!evidence.value?.evidence?.length)
async function loadEvidence() {
  try { evidence.value = await api.get<any>(`/api/jobs/${jobId.value}/evidence`) }
  catch { evidence.value = null }
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
// 轮询定时器提升到组件作用域,卸载/切 job/再次重跑时统一清理,避免泄漏与对已销毁状态的写入。
let pollTimer: ReturnType<typeof setInterval> | null = null
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
function pollForVersion(provider: string) {
  stopPolling()
  let n = 0
  pollTimer = setInterval(async () => {
    n++
    await loadVersions()
    const got = versions.value.find(v => v.provider === provider)
    if (got || n > 48) {
      stopPolling()
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
  const t = contentTypeLabel(o.content_type)
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
const showArtifacts = ref(false)   // 产物路径默认折叠
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
              <span class="badge b-mut">{{ contentTypeLabel(job.content_type) }}</span>
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
              · 耗时 {{ genEnd ? fmtDuration(genDurSec) : '—' }}
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
        <button v-if="hasEvidence" :class="{ on: tab === 'evidence' }" @click="tab = 'evidence'">
          <ShieldCheck :size="15" />权威来源
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
          <!-- max-w-none:解除 @tailwindcss/typography 给 .prose 的 65ch 上限,
               否则笔记正文被卡到 ~586px、在 762px 列里右侧留大片空白(笔记大小不对)。 -->
          <div class="card pad prose max-w-none">
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

      <!-- ════ 权威来源 ════ -->
      <div v-show="tab === 'evidence'">
        <div class="card pad">
          <div class="card-h"><ShieldCheck :size="15" />权威来源<template v-if="evidence?.evidence?.length"> · {{ evidence.evidence.length }}</template></div>
          <p class="lead" style="margin:-6px 0 12px">AI 取证为这条案例笔记抓取的判决/处罚/报道来源。笔记里的精确数据可点链接到原文核验。</p>

          <div v-if="evidence?.case_match" style="font-size:12.5px;color:var(--ink-600);margin-bottom:12px;padding:8px 10px;background:var(--bg-soft,#f6f7f9);border-radius:8px">
            <span style="font-weight:600" :style="{ color: evidence.case_match.confidence === 'high' ? '#15803d' : '#b45309' }">匹配 {{ evidence.case_match.confidence }}</span>
            ·
            {{ evidence.case_match.subject }}
            <div v-if="evidence.case_match.note" style="margin-top:4px;color:var(--ink-500)">⚠ {{ evidence.case_match.note }}</div>
          </div>

          <div v-for="s in evidence?.evidence || []" :key="s.id" class="card pad" style="margin-bottom:10px">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
              <span class="chip on" style="cursor:default">{{ s.id }}</span>
              <span class="badge">{{ s.type }}</span>
              <span style="font-size:11px;font-weight:600" :style="{ color: s.match_confidence === 'high' ? '#15803d' : '#b45309' }">{{ s.match_confidence }}</span>
              <strong style="font-size:13.5px">{{ s.title }}</strong>
            </div>
            <div style="font-size:12px;color:var(--ink-500);margin-bottom:6px">
              {{ s.publisher }}<template v-if="s.ref"> · {{ s.ref }}</template>
              <a :href="s.url" target="_blank" rel="noopener" style="margin-left:6px;display:inline-flex;align-items:center;gap:2px">
                <ExternalLink :size="12" />原文链接
              </a>
            </div>
            <ul style="font-size:12.5px;color:var(--ink-600);margin:0;padding-left:18px">
              <li v-for="(f, i) in s.key_facts || []" :key="i" style="margin-bottom:3px"><strong>{{ f.figure }}</strong> —— {{ f.quote }}</li>
            </ul>
          </div>

          <div v-if="evidence?.notes" style="font-size:11.5px;color:var(--ink-500);margin-top:4px">{{ evidence.notes }}</div>
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
        <div v-if="jobDagSteps.length" class="card pad" style="margin-bottom:14px;padding:13px 15px">
          <div style="font-size:13px;font-weight:600;color:var(--ink-800);display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <GitBranch :size="14" />流程依赖图（DAG）
            <span style="font-weight:400;font-size:11px;color:var(--ink-500);display:inline-flex;gap:9px;margin-left:4px">
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--ok)"></i>完成</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--run)"></i>运行中</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--bad)"></i>失败</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--ink-300)"></i>跳过/待运行</span>
            </span>
          </div>
          <PipelineDag :steps="jobDagSteps" :status-by-key="stepStatusByKey" :selected="selectedStep" @select="selectedStep = $event" style="margin-top:10px" />
        </div>
        <StepWorkbench :job-id="jobId" :steps="steps" :selected-step="selectedStep" />

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
        <!-- ① 内容本身(源)信息 -->
        <div class="card pad">
          <div class="card-h"><Info :size="15" />内容信息</div>
          <table class="kv">
            <tr><td>标题</td><td>{{ job.title || '—' }}</td></tr>
            <tr><td>类型</td><td>{{ contentTypeLabel(job.content_type) }}</td></tr>
            <tr><td>来源</td><td>{{ sourceLabel }}</td></tr>
            <tr><td>发布时间</td><td>{{ fmtDateTime(job.published_at) }}</td></tr>
            <!-- 视频→时长+分辨率、文章→字数、通用→原始大小/字幕(metadata.json/parsed.json) -->
            <tr v-if="job.media?.duration_sec"><td>时长</td><td>{{ fmtDuration(job.media.duration_sec) }}</td></tr>
            <tr v-if="job.media?.resolution"><td>分辨率</td><td class="mono">{{ job.media.resolution }}</td></tr>
            <tr v-if="job.media?.video_codec"><td>视频编码</td><td class="mono">{{ job.media.video_codec }}</td></tr>
            <tr v-if="job.media?.audio_codec"><td>音频编码</td><td class="mono">{{ job.media.audio_codec }}</td></tr>
            <tr v-if="job.media?.fps"><td>帧率</td><td>{{ job.media.fps }} fps</td></tr>
            <tr v-if="job.media?.bitrate_kbps ?? job.media?.video_bitrate_kbps"><td>码率</td><td>{{ fmtBitrate(job.media.bitrate_kbps ?? job.media.video_bitrate_kbps) }}</td></tr>
            <tr v-if="job.media?.word_count"><td>字数</td><td>{{ job.media.word_count.toLocaleString() }} 字</td></tr>
            <tr v-if="job.media && (job.media.file_size_bytes != null || job.media.file_size_mb != null)">
              <td>原始文件大小</td><td>{{ fmtSize(job.media) }}</td>
            </tr>
            <tr v-if="job.media && (job.media.has_subtitle !== undefined || job.media.has_danmaku !== undefined)">
              <td>字幕/弹幕</td>
              <td>
                <span class="badge" :class="job.media.has_subtitle ? 'b-ok' : 'b-mut'">{{ job.media.has_subtitle ? '有字幕' : '无字幕' }}</span>
                <span v-if="job.media.has_danmaku" class="badge b-info" style="margin-left:5px">有弹幕</span>
              </td>
            </tr>
            <tr v-if="bv"><td>BV 号</td><td class="mono">{{ bv }}</td></tr>
            <tr v-if="job.url"><td>原始链接</td><td>
              <a class="ghost" :href="job.url" target="_blank" rel="noopener" style="color:var(--info)">{{ job.url }}<ExternalLink :size="13" /></a>
            </td></tr>
          </table>
        </div>

        <!-- ② 处理(任务)信息 -->
        <div class="card pad" style="margin-top:16px">
          <div class="card-h"><GitBranch :size="15" />处理信息</div>
          <table class="kv">
            <tr><td>Job ID</td><td class="mono">{{ job.job_id }}</td></tr>
            <tr><td>状态</td><td><StatusBadge :status="jobStatus" /></td></tr>
            <tr><td>知识库</td><td>{{ job.domain || '—' }}</td></tr>
            <tr><td>集合</td><td>
              <template v-if="collectionName">
                {{ collectionName }}
                <span v-if="collectionId" class="mono dim" style="font-size:11.5px;margin-left:6px">{{ collectionId }}</span>
              </template>
              <span v-else class="dim">未归集合</span>
            </td></tr>
            <tr><td>创建于</td><td>{{ fmtDateTime(job.created_at) }}</td></tr>
            <tr v-if="job.updated_at"><td>更新于</td><td>{{ fmtDateTime(job.updated_at) }}</td></tr>
            <tr><td>生成耗时</td><td>{{ genEnd ? fmtDuration(genDurSec) : (anyRunning ? '进行中' : '—') }}</td></tr>
          </table>

          <!-- 产物路径(绝对路径,可折叠) -->
          <div v-if="job.artifacts?.length" class="artifacts">
            <button class="art-toggle" @click="showArtifacts = !showArtifacts">
              <ChevronDown :size="14" class="art-caret" :class="{ open: showArtifacts }" />
              产物路径 · {{ job.artifacts.length }}
            </button>
            <ul v-show="showArtifacts" class="art-list">
              <li v-for="p in job.artifacts" :key="p" class="mono">{{ p }}</li>
            </ul>
          </div>

          <div style="margin-top:16px;display:flex;gap:8px">
            <button v-if="jobStatus === 'failed'" class="btn" @click="retryJob"><RotateCcw :size="14" />重新提交</button>
            <button class="btn danger" @click="showDelete = true"><Trash2 :size="14" />删除内容</button>
          </div>
        </div>
      </div>
    </template>

    <!-- 换 provider 重跑确认(rerunWith 设 pendingProvider → 此弹窗确认才真正发起 rerun-smart) -->
    <div v-if="pendingProvider" class="overlay show confirm" @click.self="pendingProvider = null">
      <div class="modal">
        <div class="hd">
          <span class="lead-ic"><RefreshCw :size="16" /></span>
          <b>换 provider 重跑</b>
        </div>
        <div class="bd" style="font-size:13.5px;color:var(--ink-700)">
          用 <b>{{ pendingProvider.name }}</b>（{{ pendingProvider.label }}）重新生成智能笔记？将新增一个版本，原版本保留。
        </div>
        <div class="ft">
          <button class="btn" @click="pendingProvider = null">取消</button>
          <button class="btn pri" :disabled="rerunning" @click="confirmRerun"><RefreshCw :size="14" />开始重跑</button>
        </div>
      </div>
    </div>

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

<style scoped>
.artifacts { margin-top: 16px; border-top: 1px solid var(--line-soft); padding-top: 12px; }
.art-toggle {
  display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600;
  color: var(--ink-700); background: none; cursor: pointer; padding: 0;
}
.art-caret { transition: transform .15s; transform: rotate(-90deg); }  /* 默认折叠 */
.art-caret.open { transform: rotate(0deg); }                            /* 展开:箭头朝下 */
.art-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.art-list li {
  font-size: 12px; color: var(--ink-600); padding: 3px 8px; border-radius: 5px;
  background: var(--raised); border: 1px solid var(--line-soft); word-break: break-all;
}
</style>
