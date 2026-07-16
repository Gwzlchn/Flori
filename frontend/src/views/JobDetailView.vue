<script setup lang="ts">
import { ref, computed, watch, inject, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useJobStore } from '../stores/jobs'
import { useJobDetailController } from '../composables/useJobDetailController'
import JobHeaderPanel from '../components/job/detail/JobHeaderPanel.vue'
import JobNotesPanel from '../components/job/detail/JobNotesPanel.vue'
import JobReviewPanel from '../components/job/detail/JobReviewPanel.vue'
import JobConceptsPanel from '../components/job/detail/JobConceptsPanel.vue'
import JobPipelinePanel from '../components/job/detail/JobPipelinePanel.vue'
import JobInfoPanel from '../components/job/detail/JobInfoPanel.vue'
import JobEvidencePanel from '../components/job/detail/JobEvidencePanel.vue'
import JobDeleteDialog from '../components/job/detail/JobDeleteDialog.vue'
import { contentTypeIcon, contentTypePill, contentTypeLabel } from '../utils/contentType'
import { jobSourceLabel } from '../constants/sources'
import type { CanonicalEvidenceProjection, JobDetail, GlossaryTerm, JobConcept } from '../types'
import {
  BookOpen, Lightbulb, GitBranch, Info, RefreshCw, RotateCcw, ShieldCheck,
  Image as ImageIcon,
} from 'lucide-vue-next'

// 内容详情(原型 #detail):头部 + 4 tab,即笔记/概念/流水线/元信息。
// 一律默认落「笔记」(article/paper 有解析版原文,点开即可读,不等 AI 步)。
// 另含步骤操作(重试/重跑/删除);笔记侧支持版本切换、评审、采纳、换 provider 重跑。
const router = useRouter()
const api = useApi()
const jobStore = useJobStore()
const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

const {
  jobId, job, loading, loadError, steps, jobStatus, connected,
  fetchDetail, startPolling, stopPolling,
} = useJobDetailController({ onReset: resetJobView, onLoaded: handleDetailLoaded })

// 每个 job 的 DAG:流水线定义(含 needs)按 content_type 匹配 /api/pipelines,叠加各步实时状态着色。
const pipelinesDef = ref<{ name: string; steps: { key: string; label: string | null; pool: string | null; needs: string[] }[] }[]>([])
const jobDagSteps = computed(() => pipelinesDef.value.find(p => p.name === job.value?.content_type)?.steps || [])
const stepStatusByKey = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  for (const s of steps.value) m[s.name] = s.status
  return m
})
// DAG 与工作台共享的选中步(点 DAG 节点即选)。默认恒为首步(下载);每次进流水线 tab 重置,
// 不记忆上次点选(用户明确要求「都从下载开始」);同 tab 内点选不被 steps 刷新覆盖。
const selectedStep = ref('')
watch(steps, (s) => {
  if (selectedStep.value && s.some(x => x.name === selectedStep.value)) return
  if (!s.length) return
  selectedStep.value = s[0].name
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

// 同源 lineage 的所有快照:时间倒序;>1 则头部出历史版本跳转下拉。
interface LineageVersion { job_id: string; created_at: string; is_current: boolean; title: string | null; status: string }
const lineageVersions = ref<LineageVersion[]>([])
function jumpVersion(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  if (id && id !== jobId.value) router.push(`/content/${encodeURIComponent(id)}`)
}

// tab
type Tab = 'notes' | 'concepts' | 'proc' | 'info' | 'evidence' | 'figures'
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

async function handleDetailLoaded(_detail: JobDetail) {
  const fid = jobId.value
  api.get<{ versions: LineageVersion[] }>(`/api/jobs/${fid}/versions`).then(r => { if (jobId.value === fid) lineageVersions.value = r?.versions || [] }).catch(() => {})
  void loadEvidence()
  void loadOriginal()
  void loadTranslated()
  void loadFigures()
  api.get<{ pipelines?: any[] }>('/api/pipelines').then(r => { if (jobId.value === fid) pipelinesDef.value = Array.isArray(r) ? r : (r?.pipelines ?? []) }).catch(() => {})
  api.get<{ usage?: any[] }>(`/api/jobs/${fid}/usage`).then(r => { if (jobId.value === fid) jobUsageRows.value = r?.usage || [] }).catch(() => {})
  void loadPromptVersions()
  tab.value = 'notes'
}

// 切 job 必须重置的每-job 视图态。notesInit 不复位会让 ensureNotes 对新 job 直接 no-op,
// 上一个 job 的 noteContent 挂在新 job 标题下(跨 job 串台,实测踩过:Prompt 页显示 Hallucination 原文);
// 其余清空防切页瞬间闪现旧 job 内容。
function resetJobView() {
  notesInit = false; conceptsInit = false
  jobUsageRows.value = []
  lineageVersions.value = []; pipelinesDef.value = []
  noteContent.value = ''; noteError.value = ''
  canonicalEvidence.value = []
  versions.value = []; review.value = null
  originalMd.value = ''; translatedMd.value = ''
  evidence.value = null; figures.value = []
  jobConcepts.value = []; conceptsError.value = ''
  activeFile.value = null; noteVariant.value = 'smart'
  pdfJumpPage.value = 0
  selectedStep.value = ''
}

// 笔记 tab
const domain = computed(() => job.value?.domain || '')
// paper 的可检索原文和版式 PDF 是两种不同阅读面,不用 HTML→Markdown 代替 PDF 真相源。
type NoteVariant = 'smart' | 'original' | 'translated' | 'pdf'
const noteVariant = ref<NoteVariant>('smart')
const noteContent = ref('')
const canonicalEvidence = ref<CanonicalEvidenceProjection[]>([])
const noteLoading = ref(false)
const noteError = ref('')
const headings = ref<{ id: string; text: string; level: number }[]>([])
// 已采纳术语实体(供正文术语链接 + 采纳去重):zh_name/aliases 一并传给 MarkdownViewer,
// 中文说法/变体也高亮到同一实体。
const terms = ref<{ term: string; zh_name?: string; aliases?: string[] }[]>([])
const acceptedTermNames = computed(() => new Set(terms.value.map((t) => t.term)))

function currentCanonicalNoteType(): string {
  if (noteVariant.value === 'smart') return 'smart'
  if (noteVariant.value === 'translated') return 'translated'
  return hasReadableOriginal.value ? 'original' : 'mechanical'
}

async function loadCanonicalEvidence(fid: string) {
  const noteType = currentCanonicalNoteType()
  try {
    const response = await api.get<{ items: CanonicalEvidenceProjection[] }>(
      `/api/evidence/jobs/${fid}?note_type=${encodeURIComponent(noteType)}`)
    if (jobId.value === fid && currentCanonicalNoteType() === noteType) {
      canonicalEvidence.value = response.items || []
    }
  } catch {
    if (jobId.value === fid && currentCanonicalNoteType() === noteType) {
      canonicalEvidence.value = []
    }
  }
}

type Version = { provider: string; model: string; version: string; file: string; review_file: string | null; overall: number | null; review_state?: string | null }
const versions = ref<Version[]>([])
const activeFile = ref<string | null>(null)
const isArticle = computed(() => job.value?.content_type === 'article')
const isPaper = computed(() => job.value?.content_type === 'paper')
const paperHtmlSource = computed(() => isPaper.value && job.value?.source_kind === 'arxiv-html')
// 「原文」只表示可检索文本:article readability MD 或 arXiv HTML 解析文本。
// pdf-only / 旧 paper 的解析 MD 排版损伤无法恢复,只展示独立 PDF 变体。
const hasReadableOriginal = computed(() => isArticle.value || paperHtmlSource.value)
const hasPaperPdf = computed(() => isPaper.value && (job.value?.artifacts || []).some((path) => (
  path === 'input/source.pdf' || path.endsWith('/input/source.pdf')
)))
const pdfJumpPage = ref(0)   // 译文图占位点击跳原文 PDF 的目标页(0=无;iframe #page= 原生支持)
const paperPdfUrl = computed(() =>
  `/api/jobs/${jobId.value}/media?path=${encodeURIComponent('input/source.pdf')}`
  + (pdfJumpPage.value > 0 ? `#page=${pdfJumpPage.value}` : ''))
function onPdfPageJump(p: number) {
  pdfJumpPage.value = p
  noteVariant.value = 'pdf'
}
// 有无智能笔记:有版本即有(文章关笔记时为空 → 隐藏智能版、机械版即原文)
const hasSmartNote = computed(() => versions.value.length > 0)

type Provider = { name: string; type: string; available: boolean; label: string }
const providers = ref<Provider[]>([])
const showRerun = ref(false)
const rerunning = ref(false)
const pendingProvider = ref<Provider | null>(null)

// 评审
const review = ref<Record<string, any> | null>(null)
const reviewState = computed(() => review.value?.reliability_state || 'legacy_unverified')
const reviewReliable = computed(() => (
  reviewState.value === 'reliable' && review.value?.review_reliable === true
))
const stringList = (value: unknown) => Array.isArray(value)
  ? value.filter((item): item is string => typeof item === 'string')
  : []
const reviewReasons = computed(() => stringList(review.value?.reliability_reasons))
const reviewMissingConcepts = computed(() => stringList(review.value?.missing_concepts))
const reviewTop3 = computed(() => stringList(review.value?.top3_improvements))
const reviewIssues = computed(() => Array.isArray(review.value?.issues)
  ? review.value.issues.filter((item: unknown): item is Record<string, any> => (
    !!item && typeof item === 'object' && !Array.isArray(item)
  ))
  : [])
const DIM_LABELS: Record<string, string> = {
  completeness: '完整性', accuracy: '准确性', structure: '结构', terminology: '概念',
  visual_integration: '配图', readability: '可读性', formula_integrity: '公式',
  figure_references: '图表引用', conciseness: '口语净化', insight: '观点提炼',
}
const reviewDims = computed(() => {
  if (!reviewReliable.value) return []
  const r = review.value || {}
  return Object.entries(r)
    .filter(([k, v]) => k in DIM_LABELS && typeof v === 'number' && v >= 1 && v <= 5)
    .map(([k, v]) => ({ label: DIM_LABELS[k] || k, score: v as number }))
})
const keyTerms = computed(() => {
  if (!reviewReliable.value) return [] as { term: string; definition: string }[]
  const raw = review.value?.key_terms
  if (!Array.isArray(raw)) return [] as { term: string; definition: string }[]
  return raw
    .map((t: any) => typeof t === 'string'
      ? { term: t, definition: '' }
      : { term: String(t?.term ?? ''), definition: String(t?.definition ?? '') })
    .filter((t) => t.term.trim())
})
const reviewSourcePath = (label: string) => {
  if (!reviewReliable.value) return ''
  const raw = review.value?.review_input?.sources
  const sources = Array.isArray(raw) ? raw : []
  const source = sources.find((item: any) => item?.label === label)
  return safeArtifactPath(source?.artifact)
}
const safeArtifactPath = (value: unknown) => {
  if (typeof value !== 'string' || !value.startsWith('output/') || value.includes('\0')) return ''
  return value.split('/').includes('..') ? '' : value
}
const artifactUrl = (path: string) =>
  `/api/jobs/${jobId.value}/artifact?path=${encodeURIComponent(path)}`

// ★以下 loader 均带 job 切换守卫:捕获发起时的 fid,响应回填前校验仍是当前 job——
// 否则从 A 页切到 B 页时,A 的迟到响应会覆盖 B 的内容(与 fetchDetail 里 usage/lineage 同范式)。
async function loadTerms() {
  const fid = jobId.value
  if (!domain.value) { terms.value = []; return }
  try {
    const ts = await api.get<GlossaryTerm[]>(`/api/glossary?domain=${encodeURIComponent(domain.value)}&status=accepted`)
    if (jobId.value !== fid) return
    terms.value = ts.map(t => ({ term: t.term, zh_name: t.zh_name, aliases: t.aliases }))
  } catch { if (jobId.value === fid) terms.value = [] }
}

async function loadVersions() {
  const fid = jobId.value
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
    if (noteVariant.value === 'pdf') {
      noteContent.value = ''
      canonicalEvidence.value = []
      return
    } else if (noteVariant.value === 'translated') {
      text = translatedMd.value || await api.getText(
        `/api/jobs/${fid}/artifact?path=${encodeURIComponent('output/translated.md')}`)
    } else if (noteVariant.value === 'original' && hasReadableOriginal.value) {
      text = originalMd.value || await api.getText(
        `/api/jobs/${fid}/artifact?path=${encodeURIComponent('output/original.md')}`)
    } else {
      const base = noteVariant.value === 'original'
        ? `/api/jobs/${fid}/notes/mechanical`
        : `/api/jobs/${fid}/notes/smart`
      const url = (noteVariant.value === 'smart' && activeFile.value)
        ? `${base}?file=${encodeURIComponent(activeFile.value)}`
        : base
      text = await api.getText(url)
    }
    if (jobId.value !== fid) return
    noteContent.value = text
    await loadCanonicalEvidence(fid)
  } catch (e: any) {
    if (jobId.value !== fid) return
    noteError.value = e?.status === 404
      ? (noteVariant.value === 'translated' ? '译文尚未生成'
        : noteVariant.value === 'original' && hasReadableOriginal.value ? '原文未生成' : '笔记尚未生成')
      : (e?.message || '加载失败')
    noteContent.value = ''
    canonicalEvidence.value = []
  } finally {
    noteLoading.value = false
  }
}

async function loadReview() {
  const fid = jobId.value
  review.value = null
  if (noteVariant.value !== 'smart') return
  const v = versions.value.find(x => x.file === activeFile.value) || versions.value[0]
  const url = v?.review_file
    ? `/api/jobs/${fid}/review?file=${encodeURIComponent(v.review_file)}`
    : `/api/jobs/${fid}/review`
  try {
    const r = await api.get<Record<string, any>>(url)
    if (jobId.value === fid) {
      review.value = r && typeof r === 'object' && !Array.isArray(r) ? r : null
    }
  } catch { if (jobId.value === fid) review.value = null }
}

// 权威来源(evidence) tab
// 取证产物 evidence.json:模型搜索候选,服务端受控下载与校验。有则显示 tab,404 即无。
const evidence = ref<any | null>(null)
const evidenceItems = computed(() => Array.isArray(evidence.value?.evidence)
  ? evidence.value.evidence.filter((item: unknown): item is Record<string, any> => (
    !!item && typeof item === 'object' && !Array.isArray(item)
  ))
  : [])
const evidenceManifestErrors = computed(() => stringList(evidence.value?.manifest_errors))
const evidenceManifestState = computed(() => {
  const state = evidence.value?.manifest_state
  if (['verified', 'partial', 'invalid', 'legacy'].includes(state)) return state
  return evidence.value?.reliability_state === 'legacy_unverified' ? 'legacy' : 'invalid'
})
const evidenceMatches = (item: Record<string, any>) => Array.isArray(item.matches)
  ? item.matches.filter((match: unknown): match is Record<string, any> => (
    !!match && typeof match === 'object' && !Array.isArray(match)
    && typeof (match as Record<string, any>).anchor === 'string'
  ))
  : []
const evidenceReasons = (item: Record<string, any>) => stringList(item.eligibility_reasons)
const evidenceVerificationReasons = (item: Record<string, any>) => stringList(item.verification_reasons)
const evidenceItemVerified = (item: Record<string, any>) => (
  ['verified', 'partial'].includes(evidenceManifestState.value)
  && item.verification_state === 'verified'
  && item.eligible === true
  && item.confidence === 'high'
  && item.source_tier === '一手官方'
)
const safeEvidenceUrl = (item: Record<string, any>) => (
  evidenceItemVerified(item) && item.link_safe === true && typeof item.final_url === 'string'
  && /^https:\/\/[^\s\x00-\x1f\x7f]+$/i.test(item.final_url)
    ? item.final_url : ''
)
const safeEvidenceArtifact = (item: Record<string, any>) => (
  evidenceItemVerified(item) && item.link_safe === true ? safeArtifactPath(item.artifact) : ''
)
const hasEvidence = computed(() => evidenceItems.value.length > 0)
const eligibleEvidenceIds = computed(() => evidenceItems.value
  .filter((item: any) => safeEvidenceUrl(item) || safeEvidenceArtifact(item))
  .map((item: any) => String(item.id)))
async function onEvidenceCitation(id: string) {
  tab.value = 'evidence'
  await nextTick()
  document.querySelector(`[data-evidence-card="${id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
async function loadEvidence() {
  const fid = jobId.value
  try {
    const r = await api.get<any>(`/api/jobs/${fid}/evidence`)
    if (jobId.value === fid) {
      evidence.value = r && typeof r === 'object' && !Array.isArray(r) ? r : null
    }
  } catch { if (jobId.value === fid) evidence.value = null }
}

// 原文(article output/original.md)
// 可读原文 Markdown(图片本地化);在笔记 tab 作「原文」变体展示,404 即无。
const originalMd = ref('')
async function loadOriginal() {
  const fid = jobId.value
  if (!hasReadableOriginal.value) { originalMd.value = ''; return }
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
// 兼容旧 paper job 的 figures.json;当前链不生成该产物。只列仍有 filename 的历史渲染图。
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
  // 无智能笔记时优先可检索原文;pdf-only paper 直接落版式原文。
  if (!versions.value.length) {
    if (hasReadableOriginal.value) noteVariant.value = 'original'
    else if (hasPaperPdf.value) noteVariant.value = 'pdf'
  }
  await Promise.all([loadNote(), loadReview()])
}

async function switchVariant(v: NoteVariant) {
  if (noteVariant.value === v) return
  noteVariant.value = v
  activeFile.value = null
  await loadVersions()
  await Promise.all([loadNote(), loadReview()])
}

// AI 步实时刷新:智能笔记/译文/评审步翻到 done 时更新变体可用性与内容——
// ensureNotes 只跑一次,不刷会「已生成却显示未生成」(BERT 实测踩过)。
const _aiArtifactSteps = ['04_smart_article', '05_smart_paper', '11_smart',
                          '04_translate_article', '04_translate_paper',
                          '06_review', '05_review', '12_review']
const aiStepsDone = computed(() =>
  steps.value.filter(st => _aiArtifactSteps.includes(st.name) && st.status === 'done')
    .map(st => st.name).sort().join(','))
watch(aiStepsDone, (now, prev) => {
  if (!notesInit || now === prev || !now) return
  loadVersions(); loadTranslated()
  if (tab.value === 'notes') { loadNote(); loadReview() }
})

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
  startPolling(async () => {
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
  if (!domain.value || acceptedTermNames.value.has(term)) return
  try {
    try {
      await api.post(`/api/glossary/${encodeURIComponent(domain.value)}/${encodeURIComponent(term)}/accept`)
    } catch (e: any) {
      if (e?.status === 404) {
        await api.post(`/api/glossary?domain=${encodeURIComponent(domain.value)}`, { term, definition: definition || null })
      } else { throw e }
    }
    terms.value.push({ term })
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
type AiPromptRow = { step: string; label: string; used: string; current: string | null; stale: boolean }
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
    let current: string | null = null
    try {
      if (dom) {
        const dq = await api.get<{ active_version: string | null }>(
          `/api/prompts/${pipeline}/${step}?scope=domain&domain=${encodeURIComponent(dom)}`)
        if (dq.active_version != null) current = dq.active_version
      }
      if (current == null) {
        const gq = await api.get<{ active_version: string | null }>(
          `/api/prompts/${pipeline}/${step}?scope=global`)
        current = gq.active_version ?? null
      }
    } catch { /* 读不到当前版本时按未知处理,不阻断 */ }
    const label = jobDagSteps.value.find(x => x.key === step)?.label
      || steps.value.find(x => x.name === step)?.label || step
    rows.push({ step, label, used, current, stale: current !== used })
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
  else if (t === 'proc') selectedStep.value = steps.value[0]?.name || ''  // 进流水线恒从「下载」开始
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
      <JobHeaderPanel
        :job="job" :job-status="jobStatus" :connected="connected" :type-icon="typeIcon"
        :type-class="typeClass" :source-display="sourceDisplay" :bv="bv"
        :lineage-versions="lineageVersions" :gen-start="genStart" :gen-end="genEnd"
        :gen-dur-sec="genDurSec" :any-running="anyRunning" @jump-version="jumpVersion"
      />

      <!-- tabs -->
      <div class="tabs">
        <button v-for="t in TABS" :key="t.key" :class="{ on: tab === t.key }" @click="tab = t.key">
          <component :is="t.icon" :size="15" />{{ t.label }}
        </button>
        <button v-if="hasEvidence" :class="{ on: tab === 'evidence' }" @click="tab = 'evidence'">
          <ShieldCheck :size="15" />权威来源
        </button>
        <button v-if="hasFigures" :class="{ on: tab === 'figures' }" @click="tab = 'figures'">
          <ImageIcon :size="15" />图表
        </button>
      </div>

      <!-- 笔记(article:智能版可隐藏、机械版=原文) -->
      <div v-show="tab === 'notes'">
        <JobNotesPanel :job-id="jobId" :domain="domain" :has-smart-note="hasSmartNote" :has-translation="hasTranslation"
          :has-readable-original="hasReadableOriginal" :has-paper-pdf="hasPaperPdf" :note-variant="noteVariant" :versions="versions" :active-file="activeFile"
          :rerunning="rerunning" :show-rerun="showRerun" :providers="providers" :note-loading="noteLoading" :note-error="noteError"
          :is-paper="isPaper" :paper-pdf-url="paperPdfUrl" :note-content="noteContent"
          :terms="terms" :evidence-ids="eligibleEvidenceIds" :canonical-evidence="canonicalEvidence" :headings="headings"
          :version-label="verLabel" @switch-variant="switchVariant" @select-version="selectVersion"
          @toggle-rerun="showRerun = !showRerun" @rerun="rerunWith" @headings="headings = $event"
          @pdf-page="onPdfPageJump" @evidence-citation="onEvidenceCitation">
          <JobReviewPanel v-if="noteVariant === 'smart' && review" :review="review" :reliable="reviewReliable"
            :state="reviewState" :reasons="reviewReasons" :dimensions="reviewDims" :missing-concepts="reviewMissingConcepts"
            :improvements="reviewTop3" :issues="reviewIssues" :dimension-labels="DIM_LABELS" :key-terms="keyTerms"
            :accepted-terms="acceptedTermNames" :source-path="reviewSourcePath" :artifact-url="artifactUrl" @accept="acceptKeyTerm" />
        </JobNotesPanel>
      </div>

      <!-- 权威来源 -->
      <div v-show="tab === 'evidence'">
        <JobEvidencePanel
          :items="evidenceItems" :manifest-state="evidenceManifestState" :manifest-errors="evidenceManifestErrors"
          :safe-url="safeEvidenceUrl" :safe-artifact="safeEvidenceArtifact" :artifact-url="artifactUrl"
          :matches="evidenceMatches" :reasons="evidenceReasons" :verification-reasons="evidenceVerificationReasons"
        />
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
        <JobConceptsPanel :concepts="jobConcepts" :loading="conceptsLoading" :error="conceptsError"
          :occurrence-text="conceptOccText" @retry="loadConcepts" @select="goConcept" />
      </div>

      <!-- 流水线 -->
      <div v-show="tab === 'proc'">
        <JobPipelinePanel :job-id="jobId" :steps="steps" :dag-steps="jobDagSteps"
          :status-by-key="stepStatusByKey" :selected-step="selectedStep" :selected-step-label="selectedStepLabel"
          :usage-by-step="usageByStep" :total-ai="totalAi" :job-status="jobStatus" :rebuilding="rebuilding"
          :prompt-rows="aiPromptRows" @select-step="selectedStep = $event" @retry="retryJob"
          @rerun="rerunFromStep" @rebuild="rebuildJob" @rerun-prompt="rerunStep" />
      </div>

      <!-- 元信息 -->
      <div v-show="tab === 'info'">
        <JobInfoPanel :job="job" :job-status="jobStatus" :source-display="sourceDisplay" :bv="bv"
          :collection-id="collectionId" :collection-name="collectionName" :gen-end="genEnd"
          :gen-dur-sec="genDurSec" :any-running="anyRunning" @retry="retryJob" @delete="showDelete = true" />
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

    <JobDeleteDialog v-if="showDelete" @cancel="showDelete = false" @confirm="confirmDelete" />
  </div>
</template>

<style scoped>
.fig-card { margin: 0 0 22px; }
.fig-card img {
  max-width: 100%; display: block; border: 1px solid var(--line-soft);
  border-radius: 8px; background: #fff; padding: 8px;
}
.fig-card figcaption { margin-top: 6px; font-size: 13px; color: var(--ink-600); line-height: 1.5; }
</style>
