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
  AlertTriangle, ChevronRight, Bookmark, ShieldCheck, Coins, Languages,
  Image as ImageIcon,
} from 'lucide-vue-next'

// 内容详情(原型 #detail):头部 + 4 tab,即笔记/概念/流水线/元信息。
// 完成态(done)默认落「笔记」,未完成默认「流水线」。
// 另含步骤操作(重试/重跑/删除);笔记侧支持版本切换、评审、采纳、换 provider 重跑。
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
// DAG 与工作台共享的选中步(点 DAG 节点即选)。默认按优先级取运行中、失败、最后完成、末步;用户点选后不被刷新覆盖。
const selectedStep = ref('')
watch(steps, (s) => {
  if (selectedStep.value && s.some(x => x.name === selectedStep.value)) return
  if (!s.length) return
  const pick = s.find(x => x.status === 'running') || s.find(x => x.status === 'failed')
    || [...s].reverse().find(x => x.status === 'done') || s[s.length - 1]
  selectedStep.value = pick.name
}, { immediate: true })

// AI 用量(逐次)→ 按步聚合 provider/开销喂 DAG 节点 + 全 job 总开销。
const jobUsageRows = ref<{ step: string | null; provider: string; cost_usd: number }[]>([])
const usageByStep = computed<Record<string, { provider: string; cost: number; equiv: boolean }>>(() => {
  const m: Record<string, { provider: string; cost: number; equiv: boolean }> = {}
  for (const u of jobUsageRows.value) {
    if (!u.step) continue
    const e = m[u.step] || (m[u.step] = { provider: u.provider, cost: 0, equiv: false })
    e.cost += u.cost_usd || 0
    if (u.provider === 'claude-cli') e.equiv = true
  }
  return m
})
const totalAi = computed(() => {
  let cost = 0, equiv = false
  for (const u of jobUsageRows.value) { cost += u.cost_usd || 0; if (u.provider === 'claude-cli') equiv = true }
  return { cost, equiv, calls: jobUsageRows.value.length }
})
const fmtCost = (v: number) => `$${(v ?? 0).toFixed(4)}`

const job = ref<JobDetail | null>(null)
// 同源 lineage 的所有快照:时间倒序;>1 则头部出历史版本跳转下拉。
interface LineageVersion { job_id: string; created_at: string; is_current: boolean; title: string | null; status: string }
const lineageVersions = ref<LineageVersion[]>([])
function jumpVersion(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  if (id && id !== jobId.value) router.push(`/content/${encodeURIComponent(id)}`)
}
const loading = ref(true)
const loadError = ref('')

// tab
type Tab = 'notes' | 'concepts' | 'proc' | 'info' | 'evidence' | 'translated' | 'figures'
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
// 来源展示:优先具体来源(论文→会议+年份 venue;文章→网站名 sitename),无则回退类型标签。
const sourceDisplay = computed(() => job.value?.media?.venue || job.value?.media?.sitename || sourceLabel.value)
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

// 集合(元信息):collection_name 由后端 collection_id join 出,无归属/已删为 null;以名为主、id 备查。
const collectionId = computed(() => job.value?.collection_id ?? null)
const collectionName = computed(() => job.value?.collection_name ?? null)

async function fetchDetail() {
  loading.value = true
  loadError.value = ''
  jobUsageRows.value = []  // 切 job 先清空:避免新 job 的 usage 请求失败/404 时残留上一个 job 的开销(跨 job 串台)
  const fid = jobId.value
  try {
    const d = await jobStore.fetchDetail(jobId.value)
    job.value = d
    jobStatus.value = d.status
    // 面包屑用真实内容:知识库 / 领域 / 标题。
    global.setCrumbs([
      { t: '知识库', to: '/' },
      ...(d.domain ? [{ t: d.domain, to: `/kb/${encodeURIComponent(d.domain)}` }] : []),
      { t: d.title || jobId.value },
    ])
    setInitialSteps(d.steps)
    // 同源 lineage 快照(重投/来源更新/pipeline 重建产生的多个 job);>1 则头部出历史版本跳转。
    api.get<{ versions: LineageVersion[] }>(`/api/jobs/${fid}/versions`).then(r => { if (jobId.value === fid) lineageVersions.value = r?.versions || [] }).catch(() => {})
    loadEvidence()  // 权威来源(取证产物);有则显示「权威来源」tab
    loadOriginal()  // 原文 MD(article 链 output/original.md);在笔记 tab 作「原文」变体展示
    loadTranslated()  // 译文 MD(非中文文章 output/translated.md);有则显示「译文」tab
    loadFigures()     // 论文图表(intermediate/figures.json);有渲染图则显示「图表」tab
    // 本 job DAG 的 needs 依赖定义,来自 /api/pipelines 的 {pipelines:[...]};失败留空不影响详情。
    api.get<{ pipelines?: any[] }>('/api/pipelines').then(r => { pipelinesDef.value = Array.isArray(r) ? r : (r?.pipelines ?? []) }).catch(() => {})
    // 逐次 AI 用量 → DAG 节点 provider/开销 + 总开销。带 job 切换守卫,迟到的回填不串到新 job。
    api.get<{ usage?: any[] }>(`/api/jobs/${fid}/usage`).then(r => { if (jobId.value === fid) jobUsageRows.value = r?.usage || [] }).catch(() => {})
    // 本任务各 AI 步用的 prompt 版本 vs 当前激活版本(白盒版本管理);不一致给「重跑该步」。
    loadPromptVersions().catch(() => {})
    // 完成态默认落笔记,否则落流水线。
    tab.value = d.status === 'done' ? 'notes' : 'proc'
  } catch (e: any) {
    loadError.value = e?.status === 404 ? '内容不存在或已删除' : (e?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

// 切 job 必须重置的每-job 视图态。notesInit 不复位会让 ensureNotes 对新 job 直接 no-op,
// 上一个 job 的 noteContent 挂在新 job 标题下(跨 job 串台,实测踩过:Prompt 页显示 Hallucination 原文);
// 其余清空防切页瞬间闪现旧 job 内容。
function resetJobView() {
  notesInit = false; conceptsInit = false
  noteContent.value = ''; noteError.value = ''
  versions.value = []; review.value = null
  originalMd.value = ''; translatedMd.value = ''
  evidence.value = null; figures.value = []
  jobConcepts.value = []; conceptsError.value = ''
  activeFile.value = null; isMechanical.value = false
}

onMounted(fetchDetail)
watch(jobId, () => { stopPolling(); resetJobView(); fetchDetail() })   // 切 job 先停旧轮询 + 清旧视图态
onBeforeUnmount(() => { global.setCrumbs(null); stopPolling() })   // 离开详情页清面包屑覆盖 + 停轮询

// 笔记 tab
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
const isArticle = computed(() => job.value?.content_type === 'article')
// 文章 + 论文都有解析版原文(article=02_parse readability MD;paper=03_sections 章节 MD),
// 机械变体即「原文」;video/audio 的机械变体仍是转写机械版。
const hasReadableOriginal = computed(() =>
  isArticle.value || job.value?.content_type === 'paper')
// 有无智能笔记:有版本即有(文章关笔记时为空 → 隐藏智能版、机械版即原文)
const hasSmartNote = computed(() => versions.value.length > 0)

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

// ★以下 loader 均带 job 切换守卫:捕获发起时的 fid,响应回填前校验仍是当前 job——
// 否则从 A 页切到 B 页时,A 的迟到响应会覆盖 B 的内容(与 fetchDetail 里 usage/lineage 同范式)。
async function loadTerms() {
  const fid = jobId.value
  if (!domain.value) { terms.value = []; return }
  try {
    const ts = await api.get<GlossaryTerm[]>(`/api/glossary?domain=${encodeURIComponent(domain.value)}&status=accepted`)
    if (jobId.value !== fid) return
    terms.value = ts.map(t => t.term)
  } catch { if (jobId.value === fid) terms.value = [] }
}

async function loadVersions() {
  const fid = jobId.value
  if (isMechanical.value) { versions.value = []; return }
  try {
    const r = await api.get<{ versions: Version[] }>(`/api/jobs/${fid}/note-versions`)
    if (jobId.value !== fid) return
    versions.value = r.versions || []
  } catch { if (jobId.value === fid) versions.value = [] }
}

async function loadProviders() {
  try {
    const r = await api.get<{ providers: Provider[] }>(`/api/providers`)
    providers.value = r.providers || []
  } catch { providers.value = [] }
}

async function loadNote() {
  const fid = jobId.value
  noteLoading.value = true
  noteError.value = ''
  try {
    let text: string
    if (isMechanical.value && hasReadableOriginal.value) {
      // 文章「原文」= output/original.md(已由 loadOriginal 载入;兜底再拉一次)
      text = originalMd.value || await api.getText(
        `/api/jobs/${fid}/artifact?path=${encodeURIComponent('output/original.md')}`)
    } else {
      const base = isMechanical.value
        ? `/api/jobs/${fid}/notes/mechanical`
        : `/api/jobs/${fid}/notes/smart`
      const url = (!isMechanical.value && activeFile.value)
        ? `${base}?file=${encodeURIComponent(activeFile.value)}`
        : base
      text = await api.getText(url)
    }
    if (jobId.value !== fid) return
    noteContent.value = text
  } catch (e: any) {
    if (jobId.value !== fid) return
    noteError.value = e?.status === 404
      ? (isMechanical.value && hasReadableOriginal.value ? '原文未生成' : '笔记尚未生成')
      : (e?.message || '加载失败')
    noteContent.value = ''
  } finally {
    noteLoading.value = false
  }
}

async function loadReview() {
  const fid = jobId.value
  review.value = null
  if (isMechanical.value) return
  const v = versions.value.find(x => x.file === activeFile.value) || versions.value[0]
  const url = v?.review_file
    ? `/api/jobs/${fid}/review?file=${encodeURIComponent(v.review_file)}`
    : `/api/jobs/${fid}/review`
  try {
    const r = await api.get<Record<string, any>>(url)
    if (jobId.value === fid) review.value = r
  } catch { if (jobId.value === fid) review.value = null }
}

// 权威来源(evidence) tab
// 取证产物 evidence.json:案例类笔记 AI fetch 的判决/处罚/报道来源。有则显示 tab,404 即无。
const evidence = ref<any | null>(null)
const hasEvidence = computed(() => !!evidence.value?.evidence?.length)
async function loadEvidence() {
  const fid = jobId.value
  try {
    const r = await api.get<any>(`/api/jobs/${fid}/evidence`)
    if (jobId.value === fid) evidence.value = r
  } catch { if (jobId.value === fid) evidence.value = null }
}

// 原文(article output/original.md)
// 可读原文 Markdown(图片本地化);在笔记 tab 作「原文」变体展示,404 即无。
const originalMd = ref('')
async function loadOriginal() {
  const fid = jobId.value
  try {
    const text = await api.getText(
      `/api/jobs/${fid}/artifact?path=${encodeURIComponent('output/original.md')}`)
    if (jobId.value === fid) originalMd.value = text
  } catch { if (jobId.value === fid) originalMd.value = '' }
}

// 译文(article output/translated.md) tab
// 非中文文章的中文全文译文;有则显示「译文」tab,404 即无。
const translatedMd = ref('')
const hasTranslation = computed(() => !!translatedMd.value)
async function loadTranslated() {
  const fid = jobId.value
  try {
    const text = await api.getText(
      `/api/jobs/${fid}/artifact?path=${encodeURIComponent('output/translated.md')}`)
    if (jobId.value === fid) translatedMd.value = text
  } catch { if (jobId.value === fid) translatedMd.value = '' }
}

// 图表(论文 intermediate/figures.json) tab
// 04_figures 渲染的图(含矢量图);独立展示,不依赖智能笔记/AI worker。只列有渲染图(filename)的条目。
interface FigureItem { id: string; page: number; caption: string; filename: string | null; ocr_text?: string }
const figures = ref<FigureItem[]>([])
const figuresWithImage = computed(() => figures.value.filter(f => f.filename))
const hasFigures = computed(() => figuresWithImage.value.length > 0)
async function loadFigures() {
  const fid = jobId.value
  try {
    const raw = await api.getText(
      `/api/jobs/${fid}/artifact?path=${encodeURIComponent('intermediate/figures.json')}`)
    if (jobId.value === fid) figures.value = JSON.parse(raw)
  } catch { if (jobId.value === fid) figures.value = [] }
}
function figureUrl(filename: string): string {
  return `/api/jobs/${jobId.value}/assets/${filename}`
}

let notesInit = false
async function ensureNotes() {
  if (notesInit) return
  notesInit = true
  await loadTerms()
  await Promise.all([loadVersions(), loadProviders()])
  // 文章无智能笔记(关笔记)时默认显示原文,即机械版
  if (!versions.value.length && hasReadableOriginal.value) isMechanical.value = true
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
// 轮询定时器放组件作用域:卸载/切 job/再次重跑时统一清理,避免泄漏与对已销毁状态的写入。
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

// 概念 tab
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
  const fid = jobId.value
  conceptsLoading.value = true
  conceptsError.value = ''
  try {
    const list = await jobStore.fetchConcepts(fid)
    if (jobId.value !== fid) return
    // 已采纳优先、全库佐证多优先。
    jobConcepts.value = [...list].sort(
      (a, b) =>
        (Number(b.status === 'accepted') - Number(a.status === 'accepted')) ||
        ((b.occurrences?.length ?? 0) - (a.occurrences?.length ?? 0)),
    )
  } catch (e: any) {
    if (jobId.value !== fid) return
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

// 流水线 tab
// 选中步(DAG 点选)的中文名,供"从「X」重跑"按钮。
const selectedStepLabel = computed(() => {
  const d = jobDagSteps.value.find(x => x.key === selectedStep.value)
  if (d?.label) return d.label
  const s = steps.value.find(x => x.name === selectedStep.value)
  return s?.label || selectedStep.value
})
async function retryJob() {
  try {
    await jobStore.retryJob(jobId.value)
    showToast('已提交重试', 'success')
    jobStatus.value = 'processing'
  } catch (e: any) { showToast(e?.message || '重试失败', 'error') }
}
async function rerunFromStep() {
  if (!selectedStep.value) return
  try {
    await jobStore.rerunJob(jobId.value, selectedStep.value)
    showToast(`从 ${selectedStepLabel.value} 开始重跑`, 'success')
    jobStatus.value = 'processing'
  } catch (e: any) { showToast(e?.message || '重跑失败', 'error') }
}

// 本任务 prompt 版本(白盒版本管理)
// job.json.prompt_overrides[step].version 是本任务派发时用的版本快照,后端透出 job.prompt_versions。
// 与当前激活版本对比:GET /api/prompts/{pipeline}/{step},按本 job domain 解析,domain 覆盖优先于 global。
// 不一致(stale)则高亮并给「重跑该步」:复用 POST /api/jobs/{id}/rerun 传 from_step,清该步及下游 .done 重跑。
type AiPromptRow = { step: string; label: string; used: number; current: number | null; stale: boolean }
const aiPromptRows = ref<AiPromptRow[]>([])

async function loadPromptVersions() {
  aiPromptRows.value = []
  const pv = job.value?.prompt_versions || {}
  const pipeline = job.value?.content_type
  const dom = (job.value?.domain || '').trim()
  const fid = jobId.value
  if (!pipeline || !Object.keys(pv).length) return
  const rows: AiPromptRow[] = []
  for (const [step, used] of Object.entries(pv)) {
    // 当前激活版本:先按本 job domain 查(domain 覆盖优先),无则回退 global。两者都无表示无覆盖,走默认。
    let current: number | null = null
    try {
      if (dom) {
        const dq = await api.get<{ active_version: number | null }>(
          `/api/prompts/${pipeline}/${step}?scope=domain&domain=${encodeURIComponent(dom)}`)
        if (dq.active_version != null) current = dq.active_version
      }
      if (current == null) {
        const gq = await api.get<{ active_version: number | null }>(
          `/api/prompts/${pipeline}/${step}?scope=global`)
        current = gq.active_version ?? null
      }
    } catch { /* 读不到当前版本时按未知处理,不阻断 */ }
    const label = jobDagSteps.value.find(x => x.key === step)?.label
      || steps.value.find(x => x.name === step)?.label || step
    rows.push({ step, label, used: used as number, current, stale: current !== (used as number) })
  }
  if (jobId.value === fid) aiPromptRows.value = rows
}

// 「重跑该步」:复用 job 级 rerun(from_step=该步)。scheduler 清该步及下游 .done 后重跑,应用新激活 prompt。
async function rerunStep(step: string) {
  try {
    await jobStore.rerunJob(jobId.value, step)
    showToast('已发起重跑该步(及其下游)', 'success')
    jobStatus.value = 'processing'
  } catch (e: any) { showToast(e?.message || '重跑失败', 'error') }
}
// 重建为新版本快照:fork 当前 job,只重跑定义已变的步骤及下游,旧版本保留对比。
const rebuilding = ref(false)
async function rebuildJob() {
  if (rebuilding.value) return
  if (!confirm('重建为新版本?将基于当前 pipeline/prompt 建一个新版本(只重跑变化的步骤及下游,旧版本保留可对比)。')) return
  rebuilding.value = true
  try {
    const { job_id } = await jobStore.rebuildJob(jobId.value)
    showToast('已重建为新版本', 'success')
    router.push(`/content/${encodeURIComponent(job_id)}`)
  } catch (e: any) {
    showToast(e?.message || '重建失败', 'error')
    rebuilding.value = false
  }
}

// 删除
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
// 详情就绪后若初始 tab 即笔记/概念,触发懒加载。
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
      <!-- 头部 -->
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
              <span>{{ sourceDisplay }}</span>
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
              <template v-if="lineageVersions.length > 1">
                <span class="sep">·</span>
                <select class="ver-jump" :value="job.job_id" @change="jumpVersion"
                  title="同源内容的历史快照(重投/来源更新/pipeline 重建)——跳转查看/对比">
                  <option v-for="(v, i) in lineageVersions" :key="v.job_id" :value="v.job_id">
                    版本 {{ lineageVersions.length - i }}{{ v.is_current ? '(当前)' : '' }} · {{ fmtDateTime(v.created_at) }}
                  </option>
                </select>
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

      <!-- tabs -->
      <div class="tabs">
        <button v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="tab = t.key">
          <component :is="t.icon" :size="15" />{{ t.label }}
        </button>
        <button v-if="hasEvidence" :class="{ on: tab === 'evidence' }" @click="tab = 'evidence'">
          <ShieldCheck :size="15" />权威来源
        </button>
        <button v-if="hasTranslation" :class="{ on: tab === 'translated' }" @click="tab = 'translated'">
          <Languages :size="15" />译文
        </button>
        <button v-if="hasFigures" :class="{ on: tab === 'figures' }" @click="tab = 'figures'">
          <ImageIcon :size="15" />图表
        </button>
      </div>

      <!-- 笔记(article:智能版可隐藏、机械版=原文) -->
      <div v-show="tab === 'notes'">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap">
          <!-- 智能版:无智能笔记(如文章关笔记)时隐藏;机械版对文章即「原文」 -->
          <div class="seg" v-if="hasSmartNote">
            <button :class="{ on: !isMechanical }" @click="switchVariant(false)">智能版</button>
            <button :class="{ on: isMechanical }" @click="switchVariant(true)">{{ hasReadableOriginal ? '原文' : '机械版' }}</button>
          </div>
          <span v-else class="dim" style="font-size:12px">{{ hasReadableOriginal ? '原文' : '机械版' }}（未生成智能笔记）</span>

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

      <!-- 权威来源 -->
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

      <!-- 译文(非中文文章的中文全文译文) -->
      <div v-show="tab === 'translated'">
        <p class="lead" style="margin:-6px 0 12px"><Languages :size="13" /> 原文为非中文,以下是 AI 忠实全文译文(保留原结构与配图)。</p>
        <MarkdownViewer :content="translatedMd" :job-id="jobId" :domain="domain" />
      </div>

      <!-- 图表(论文按图注渲染的页面区域,含矢量图) -->
      <div v-show="tab === 'figures'">
        <p class="lead" style="margin:-6px 0 12px"><ImageIcon :size="13" /> 从 PDF 按图注渲染的图表({{ figuresWithImage.length }} 张,含矢量图)。</p>
        <figure v-for="f in figuresWithImage" :key="f.id" class="fig-card">
          <img :src="figureUrl(f.filename!)" :alt="f.caption" loading="lazy" />
          <figcaption><b>{{ f.id }}</b><span v-if="f.caption"> · {{ f.caption }}</span></figcaption>
        </figure>
      </div>

      <!-- 概念 -->
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

      <!-- 流水线 -->
      <div v-show="tab === 'proc'">
        <div v-if="jobDagSteps.length" class="card pad" style="margin-bottom:14px;padding:13px 15px">
          <div style="font-size:13px;font-weight:600;color:var(--ink-800);display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <GitBranch :size="14" />流程依赖图（DAG）
            <span style="font-weight:400;font-size:11px;color:var(--ink-500);display:inline-flex;gap:9px;margin-left:4px">
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--ok)"></i>完成</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--run)"></i>运行中</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--bad)"></i>失败</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:7px;height:7px;border-radius:50%;background:var(--ink-300)"></i>跳过/待运行</span>
              <span style="color:var(--ink-300)">|</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:3px;height:11px;border-radius:1px;background:var(--info)"></i>AI</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:3px;height:11px;border-radius:1px;background:var(--ink-400)"></i>CPU</span>
              <span style="display:inline-flex;align-items:center;gap:4px"><i style="width:3px;height:11px;border-radius:1px;background:var(--warn)"></i>GPU</span>
            </span>
            <span v-if="totalAi.calls" style="margin-left:auto;font-weight:600;color:var(--ink-700);display:inline-flex;align-items:center;gap:5px;font-size:12px">
              <Coins :size="13" style="color:var(--ink-400)" />AI 总开销 {{ fmtCost(totalAi.cost) }}<span v-if="totalAi.equiv" style="font-weight:400;color:var(--ink-400);font-size:11px">（等价）</span>
            </span>
          </div>
          <PipelineDag :steps="jobDagSteps" :status-by-key="stepStatusByKey" :selected="selectedStep" :usage-by-step="usageByStep" @select="selectedStep = $event" style="margin-top:10px" />
        </div>
        <!-- 步骤操作:对当前选中步(上方 DAG 点选)重跑;失败 job 可整体重试。紧贴步骤与产物摆放 -->
        <div v-if="jobStatus === 'done' || jobStatus === 'failed'" style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
          <button v-if="jobStatus === 'failed'" class="btn pri" @click="retryJob"><RotateCcw :size="14" />重试</button>
          <button v-if="selectedStep" class="btn" @click="rerunFromStep"><Play :size="14" />从「{{ selectedStepLabel }}」重跑</button>
          <button class="btn" :disabled="rebuilding" @click="rebuildJob"
            title="基于当前 pipeline/prompt 重建为新版本(只重跑变化步骤及下游,旧版本保留对比)">
            <GitBranch :size="14" />{{ rebuilding ? '重建中…' : '重建新版本' }}
          </button>
        </div>
        <!-- 本任务 Prompt 版本:AI 步显示派发时用的版本,与当前激活版本不一致则高亮 + 「重跑该步」 -->
        <div v-if="aiPromptRows.length" class="card pad" style="margin-bottom:12px">
          <div class="card-h"><FileText :size="15" />本任务 Prompt 版本</div>
          <div v-for="r in aiPromptRows" :key="r.step" class="pver-row">
            <span class="pver-step">{{ r.label }}</span>
            <span class="pver-tag" :class="r.stale ? 'pv-stale' : 'pv-ok'">
              本任务 prompt v{{ r.used }}<template v-if="r.stale"> · 当前 {{ r.current == null ? '默认(无覆盖)' : 'v' + r.current }}</template>
            </span>
            <span style="flex:1"></span>
            <button v-if="r.stale" class="btn sm" @click="rerunStep(r.step)"><Play :size="13" />重跑该步</button>
          </div>
          <div class="dim" style="font-size:12px;margin-top:6px">
            「本任务」= 该步派发时用的 prompt 版本快照;不一致表示之后改过 prompt,可「重跑该步」按当前版本重出(连同其下游)。
          </div>
        </div>
        <StepWorkbench :job-id="jobId" :steps="steps" :selected-step="selectedStep" />
      </div>

      <!-- 元信息 -->
      <div v-show="tab === 'info'">
        <!-- 内容本身(源)信息 -->
        <div class="card pad">
          <div class="card-h"><Info :size="15" />内容信息</div>
          <table class="kv">
            <tr><td>标题</td><td>{{ job.title || '—' }}</td></tr>
            <tr><td>类型</td><td>{{ contentTypeLabel(job.content_type) }}</td></tr>
            <tr><td>来源</td><td>{{ sourceDisplay }}</td></tr>
            <tr v-if="job.media?.authors?.length"><td>作者</td><td>{{ job.media.authors.join('、') }}</td></tr>
            <tr><td>发布时间</td><td>{{ fmtDateTime(job.published_at) }}</td></tr>
            <!-- 视频→时长+分辨率、文章→字数、通用→原始大小/字幕(metadata.json/parsed.json) -->
            <tr v-if="job.media?.duration_sec"><td>时长</td><td>{{ fmtDuration(job.media.duration_sec) }}</td></tr>
            <tr v-if="job.media?.resolution"><td>分辨率</td><td class="mono">{{ job.media.resolution }}</td></tr>
            <tr v-if="job.media?.video_codec"><td>视频编码</td><td class="mono">{{ job.media.video_codec }}</td></tr>
            <tr v-if="job.media?.audio_codec"><td>音频编码</td><td class="mono">{{ job.media.audio_codec }}</td></tr>
            <tr v-if="job.media?.fps"><td>帧率</td><td>{{ job.media.fps }} fps</td></tr>
            <tr v-if="job.media?.bitrate_kbps ?? job.media?.video_bitrate_kbps"><td>码率</td><td>{{ fmtBitrate(job.media.bitrate_kbps ?? job.media.video_bitrate_kbps) }}</td></tr>
            <tr v-if="job.media?.word_count"><td>字数</td><td>{{ job.media.word_count.toLocaleString() }} 字</td></tr>
            <tr v-if="job.media?.pages"><td>页数</td><td>{{ job.media.pages }} 页</td></tr>
            <tr v-if="job.media?.lang"><td>语言</td><td>{{ job.media.lang === 'zh' ? '中文' : (job.media.lang === 'non-zh' ? '非中文(英文等,自动翻译)' : job.media.lang) }}</td></tr>
            <tr v-if="job.media?.tags?.length"><td>标签</td><td>{{ job.media.tags.join('、') }}</td></tr>
            <tr v-if="job.media?.abstract"><td>摘要</td><td style="line-height:1.6">{{ job.media.abstract }}</td></tr>
            <tr v-if="job.media && (job.media.file_size_bytes != null || job.media.file_size_mb != null)">
              <td>原始文件大小</td><td>{{ fmtSize(job.media) }}</td>
            </tr>
            <tr v-if="['video','audio'].includes(job.content_type) && job.media && (job.media.has_subtitle !== undefined || job.media.has_danmaku !== undefined)">
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

        <!-- 处理(任务)信息 -->
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
.fig-card { margin: 0 0 22px; }
.fig-card img {
  max-width: 100%; display: block; border: 1px solid var(--line-soft);
  border-radius: 8px; background: #fff; padding: 8px;
}
.fig-card figcaption { margin-top: 6px; font-size: 13px; color: var(--ink-600); line-height: 1.5; }
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
/* 本任务 Prompt 版本行 */
.pver-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.pver-row + .pver-row { border-top: 1px solid var(--line-soft); }
.pver-step { font-size: 13px; font-weight: 600; color: var(--ink-700); }
.pver-tag { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.pv-ok { color: var(--ink-500, #6b7280); background: var(--mut-bg, #f1f5f9); }
.pv-stale { color: var(--warn-700, #b45309); background: var(--warn-bg, #fffbeb); }
</style>
