<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useApi } from '../../composables/useApi'
import MarkdownViewer from '../notes/MarkdownViewer.vue'
import AiLogPanel from './AiLogPanel.vue'
import { fmtDateTime, fmtDuration } from '../../utils/datetime'
import { fmtBytes } from '../../utils/format'
import { statusLabel } from '../../utils/status'
import type { StepInfo, StepUsage } from '../../types'
import { Check, X, Minus, Loader, Clock, ChevronRight, FileText, Braces, Package, Coins, HardDrive, RotateCcw } from 'lucide-vue-next'

// selectedStep 由父组件(JobDetailView 的 DAG 点选)驱动;本组件不自带步骤选择轨。
const props = defineProps<{
  jobId: string
  steps: StepInfo[]
  selectedStep?: string
  selectedPartId?: string | null
  canRerun?: boolean
}>()
defineEmits<{ rerun: [] }>()
const api = useApi()

const statusIcon: Record<string, any> = { done: Check, failed: X, skipped: Minus, running: Loader }
const statusColor: Record<string, string> = {
  done: 'bg-green-500 text-white', failed: 'bg-red-500 text-white',
  running: 'bg-blue-500 text-white animate-pulse', skipped: 'bg-gray-300 text-gray-500',
  waiting: 'bg-gray-200 text-gray-400', ready: 'bg-yellow-400 text-white',
}
// 状态文案统一走 utils/status.statusLabel(避免与 StatusBadge 文案漂移);配色保留本组件 Tailwind 体系。

// 产出摘要:把 step.meta 渲染成可读的标签值 chip。未知键回退原键,内部键跳过。
const META_LABELS: Record<string, string> = {
  frames: '关键帧', events: '时间节', lines: '字幕行', chunks: '分块', mode: '模式',
  kept: '保留帧', scenes: '场景数', count: '数量', danmaku: '弹幕条', sections: '章节',
  figures: '图表', duration: '时长', words: '字数', pages: '页数', provider: '模型',
}
const MODE_LABELS: Record<string, string> = { zh: '加标点', translate: '翻译为中文' }
// message=运行中实时进度文案(单独渲染在进度条旁,不作产出摘要 chip)。
const META_SKIP = new Set(['pct', 'current', 'total', 'exec_id', 'worker', 'message'])

interface AFile { path: string; kind: string; size?: number }
interface Group {
  scope_key: string
  part_id: string | null
  step: string
  label: string
  files: AFile[]
  total_bytes?: number
}
const groups = ref<Group[]>([])
const jobBytes = ref(0)          // 本 job 全部产物体积合计(/artifacts.total_bytes)
const selectedGroup = computed(() => groups.value.find(group => (
  group.step === sel.value && group.part_id === (props.selectedPartId || null)
)) || null)

const sel = computed(() => props.selectedStep || '')   // 选中步骤名(父驱动)
const selFile = ref<AFile | null>(null)
const fileContent = ref('')
const fileLoading = ref(false)
const fileErr = ref('')
const artOpen = ref(true)        // 产物默认展开,可折叠
const logOpen = ref(false)       // 日志默认折叠
const logText = ref('')
const logLoading = ref(false)
const logErr = ref('')
const aiLogOpen = ref(false)      // AI 审计日志(prompt 白盒化)默认折叠

const selStep = computed(() => props.steps.find(s => s.name === sel.value) || null)
const selFiles = computed(() => selectedGroup.value?.files || [])

const artUrl = (p: string) => `/api/jobs/${props.jobId}/artifact?path=${encodeURIComponent(p)}`
// 视频/音频走 range 流式端点(不整片加载),<video>/<audio> 才能正常播放/拖动。
const mediaUrl = (p: string) => `/api/jobs/${props.jobId}/media?path=${encodeURIComponent(p)}`
const fname = (p: string) => p.split('/').pop()
const stepLabel = (s: StepInfo) => s.label || s.name

function stepPct(s: StepInfo): number | null {
  return s.status === 'running' && s.meta?.pct != null ? s.meta.pct : null
}
function metaRows(s: StepInfo): { k: string; v: string }[] {
  const rows: { k: string; v: string }[] = []
  for (const [k, val] of Object.entries(s.meta || {})) {
    if (META_SKIP.has(k) || val == null || typeof val === 'object') continue
    let v = String(val)
    if (k === 'mode') v = MODE_LABELS[v] || v
    rows.push({ k: META_LABELS[k] || k, v })
  }
  return rows
}

// 产物按类别铺开:图片(缩略图网格) / 字幕 / 文档 / 数据。类型由扩展名/kind 预先判定。
const CAT_ORDER = ['视频', '音频', '图片', '字幕', '文档', '数据']
function catOf(f: AFile): string {
  if (f.kind === 'video') return '视频'
  if (f.kind === 'audio') return '音频'
  if (f.kind === 'image') return '图片'
  if (f.path.endsWith('.srt') || f.path.endsWith('.ass')) return '字幕'
  if (f.kind === 'json') return '数据'
  return '文档'
}
const cats = computed(() => {
  const m: Record<string, AFile[]> = {}
  for (const f of selFiles.value) (m[catOf(f)] ||= []).push(f)
  return CAT_ORDER.filter(c => m[c]?.length).map(c => ({ cat: c, files: m[c] }))
})

async function loadGroups() {
  try {
    const r = await api.get<{ groups: Group[]; total_bytes?: number }>(`/api/jobs/${props.jobId}/artifacts`)
    groups.value = r.groups || []
    jobBytes.value = r.total_bytes || 0
  } catch { groups.value = []; jobBytes.value = 0 }
}

// 逐次 AI 调用明细.非 AI 步没有记录,不能回退展示 job 总计.
const usage = ref<StepUsage[]>([])
async function loadUsage() {
  try {
    const r = await api.get<{ usage: StepUsage[] }>(`/api/jobs/${props.jobId}/usage`)
    usage.value = r.usage || []
  } catch { usage.value = [] }
}
const selectedExecutionStep = computed(() => (
  props.selectedPartId ? `part:${props.selectedPartId}::${sel.value}` : sel.value
))
const selUsage = computed(() => usage.value.filter(u => u.step === selectedExecutionStep.value))
const fmtCost = (v: number) => `$${(v ?? 0).toFixed(4)}`

// 选中 AI 步的汇总只承担概览;展开后由 AiLogPanel 展示逐次调用与完整白盒审计.
const selUsageSummary = computed(() => {
  const rows = selUsage.value
  if (!rows.length) return null
  let input = 0, output = 0, cacheRead = 0, cacheCreation = 0, cost = 0, claudeCli = false
  for (const row of rows) {
    input += row.input_tokens
    output += row.output_tokens
    cacheRead += row.cache_read_tokens
    cacheCreation += row.cache_creation_tokens
    cost += row.cost_usd || 0
    if (row.provider === 'claude-cli') claudeCli = true
  }
  const cacheDenom = input + cacheRead + cacheCreation
  return {
    calls: rows.length,
    input,
    output,
    cacheRead,
    cacheCreation,
    cost,
    hit: cacheDenom ? Math.round((cacheRead / cacheDenom) * 1000) / 10 : 0,
    claudeCli,
  }
})

// 选中步的产物体积合计(后端按步给 total_bytes;无则回退各文件 size 之和)。
const selBytes = computed(() => {
  const g = selectedGroup.value
  if (!g) return 0
  return g.total_bytes ?? g.files.reduce((s, f) => s + (f.size || 0), 0)
})

// 选中步(父经 selectedStep 驱动)变化:重置文件/日志态,自动预览首个产物。
watch([sel, () => props.selectedPartId], () => {
  selFile.value = null; fileContent.value = ''; fileErr.value = ''
  logOpen.value = false; logText.value = ''; logErr.value = ''
  aiLogOpen.value = false
  const f = selectedGroup.value?.files[0]
  if (f) viewFile(f)
})

async function viewFile(f: AFile) {
  selFile.value = f; fileErr.value = ''
  // 只对文本/JSON 拉取预览;其余二进制(图片/视频/音频/PDF 等 'other')不当文本拉,
  // 由模板用 <img>/<video>/<audio> 或下载链接呈现。PDF 当文本拉取并渲染会卡死浏览器。
  if (f.kind !== 'text' && f.kind !== 'json') { fileContent.value = ''; return }
  fileLoading.value = true
  try {
    const t = await api.getText(artUrl(f.path))
    fileContent.value = f.kind === 'json'
      ? (() => { try { return JSON.stringify(JSON.parse(t), null, 2) } catch { return t } })()
      : t
  } catch (e: any) { fileErr.value = e.message || '加载失败' }
  finally { fileLoading.value = false }
}

async function toggleLog() {
  logOpen.value = !logOpen.value
  if (logOpen.value && !logText.value && !logErr.value) {
    logLoading.value = true
    const base = props.selectedPartId
      ? `/api/jobs/${props.jobId}/parts/${props.selectedPartId}/steps/${sel.value}/log`
      : `/api/jobs/${props.jobId}/steps/${sel.value}/log`
    try { logText.value = await api.getText(base) }
    catch (e: any) { logErr.value = e?.status === 404 ? '该步骤暂无日志' : (e?.message || '日志加载失败') }
    finally { logLoading.value = false }
  }
}

onMounted(async () => {
  await Promise.all([loadGroups(), loadUsage()])
  // groups 到位后,若已有选中步则预览其首个产物(初次进入)。
  const f = selectedGroup.value?.files[0]
  if (f && !selFile.value) viewFile(f)
})
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl p-4">
    <div class="flex items-center gap-2 flex-wrap mb-3">
      <h3 class="text-sm font-semibold text-gray-700">步骤与产物</h3>
      <!-- 标题只保留 job 级产物体积.AI 开销按所选步骤展示,避免 CPU 步误挂全局总计. -->
      <div class="ml-auto flex items-center gap-3 text-xs text-gray-500">
        <span v-if="jobBytes" class="flex items-center gap-1" title="本内容全部产物体积">
          <HardDrive :size="12" class="text-gray-400" />产物 <span class="font-medium text-gray-700">{{ fmtBytes(jobBytes) }}</span>
        </span>
      </div>
    </div>
    <template v-if="selStep">
          <div class="flex items-center gap-2 flex-wrap">
            <h4 class="text-base font-semibold text-gray-800">{{ stepLabel(selStep) }}</h4>
            <span class="text-xs px-1.5 py-0.5 rounded" :class="statusColor[selStep.status] || statusColor.waiting">{{ statusLabel(selStep.status) }}</span>
            <span class="text-xs text-gray-400 font-mono">{{ selStep.name }}</span>
            <button v-if="canRerun" class="btn sm ml-auto" title="覆盖当前任务中该步骤及其后续产物" @click="$emit('rerun')">
              <RotateCcw :size="13" />重跑此步骤及后续
            </button>
          </div>

          <!-- 时间仅对真正跑过的步骤显示;等待/就绪(可能被重跑重置)不展示旧时间 -->
          <div v-if="['done', 'failed', 'running'].includes(selStep.status)" class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 mt-2">
            <span><Clock :size="12" class="inline -mt-0.5" /> 开始 {{ fmtDateTime(selStep.started_at) }}</span>
            <span>结束 {{ selStep.status === 'running' ? '进行中' : fmtDateTime(selStep.finished_at) }}</span>
            <span>耗时 {{ selStep.status === 'running' ? '进行中' : fmtDuration(selStep.duration_sec, { decimalSeconds: true }) }}</span>
            <span v-if="selStep.worker_id">由 <span class="font-mono text-gray-700">{{ selStep.worker_id }}</span> 完成</span>
          </div>
          <div v-else-if="['waiting', 'ready'].includes(selStep.status)" class="text-xs text-gray-400 mt-2">尚未运行</div>

          <div v-if="selStep.status === 'running' && (stepPct(selStep) != null || selStep.meta?.message)" class="mt-2">
            <div v-if="stepPct(selStep) != null" class="w-full bg-gray-200 rounded-full h-1.5">
              <div class="bg-blue-500 h-full rounded-full transition-all" :style="{ width: `${stepPct(selStep)}%` }" />
            </div>
            <!-- 实时进度文案(WS step_progress.message),如「扫描关键帧」-->
            <div v-if="selStep.meta?.message" class="text-xs text-gray-500 mt-1 truncate">{{ selStep.meta.message }}</div>
          </div>

          <!-- 失败原因:仅失败步骤显示(done 步骤的历史 error 如 timeout 不算失败) -->
          <p v-if="selStep.error && selStep.status === 'failed'" class="text-xs text-red-600 mt-2 break-all bg-red-50 rounded p-2">✗ {{ selStep.error }}</p>

          <!-- 跳过说明 -->
          <div v-if="selStep.status === 'skipped'" class="mt-3 text-xs text-gray-500 bg-gray-50 rounded p-2">
            已跳过{{ selStep.meta?.reason ? '：' + selStep.meta.reason : '（不满足运行条件，例如视频自带字幕则无需语音转写）' }}
          </div>

          <!-- 产出摘要:可读 chip,不直接展示原始 JSON -->
          <div v-if="metaRows(selStep).length" class="mt-3 flex flex-wrap gap-2">
            <span v-for="r in metaRows(selStep)" :key="r.k" class="text-xs bg-gray-50 border border-gray-100 rounded px-2 py-1 text-gray-600">
              {{ r.k }}：<span class="text-gray-800 font-medium">{{ r.v }}</span>
            </span>
          </div>

          <!-- 所选 AI 步汇总 + 逐次审计.一个箭头控制同一信息域,不再把 usage/log 拆成两块. -->
          <div v-if="selUsageSummary" class="mt-3 pt-3 border-t border-gray-100">
            <button
              class="w-full text-left flex items-center gap-2 flex-wrap text-xs"
              :aria-expanded="aiLogOpen" @click="aiLogOpen = !aiLogOpen"
            >
              <Coins :size="13" class="text-gray-500" />
              <span class="font-semibold text-gray-700">AI 用量</span>
              <ChevronRight :size="12" :class="aiLogOpen ? 'rotate-90' : ''" class="transition-transform text-blue-600" />
              <span class="font-medium text-gray-800">{{ fmtCost(selUsageSummary.cost) }}<span class="text-gray-400">{{ selUsageSummary.claudeCli ? '（等价）' : '' }}</span></span>
              <span class="text-gray-500">{{ selUsageSummary.calls }} 次</span>
              <span class="text-gray-500">入 {{ selUsageSummary.input.toLocaleString() }}</span>
              <span class="text-gray-500">出 {{ selUsageSummary.output.toLocaleString() }}</span>
              <span class="text-gray-500">读缓存 {{ selUsageSummary.cacheRead.toLocaleString() }}</span>
              <span class="text-gray-500">写缓存 {{ selUsageSummary.cacheCreation.toLocaleString() }}</span>
              <span class="text-gray-500">命中 {{ selUsageSummary.hit }}%</span>
              <span class="ml-auto text-gray-400">{{ aiLogOpen ? '收起审计日志' : '展开审计日志' }}</span>
            </button>
            <AiLogPanel v-if="aiLogOpen" :job-id="jobId" :step="sel" :part-id="selectedPartId" />
          </div>

          <!-- 产物(本步产出的文件) -->
          <div v-if="['done', 'failed', 'running'].includes(selStep.status)" class="mt-4 pt-3 border-t border-gray-100">
            <div class="flex items-center gap-2 mb-2">
              <span class="text-xs font-semibold text-gray-700 flex items-center gap-1.5"><Package :size="13" class="text-gray-500" />产物 <span class="font-normal text-gray-400">（{{ selFiles.length }}<template v-if="selBytes"> · {{ fmtBytes(selBytes) }}</template>）</span></span>
              <button @click="artOpen = !artOpen" class="text-xs text-blue-600 hover:text-blue-700 flex items-center gap-0.5">
                <ChevronRight :size="12" :class="artOpen ? 'rotate-90' : ''" class="transition-transform" />{{ artOpen ? '收起' : '展开' }}
              </button>
            </div>
            <template v-if="artOpen">
            <div v-if="selFiles.length" class="space-y-3">
              <div v-for="grp in cats" :key="grp.cat">
                <div class="text-xs font-medium text-gray-600 mb-1.5">{{ grp.cat }} <span class="text-gray-400 font-normal">({{ grp.files.length }})</span></div>
                <!-- 图片:全部缩略图网格 -->
                <div v-if="grp.cat === '图片'" class="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-1.5">
                  <button
                    v-for="f in grp.files" :key="f.path" @click="viewFile(f)"
                    class="block rounded overflow-hidden border"
                    :class="selFile?.path === f.path ? 'ring-2 ring-blue-400 border-blue-300' : 'border-gray-200 hover:border-gray-300'"
                  >
                    <img :src="artUrl(f.path)" loading="lazy" class="w-full h-16 object-cover" />
                  </button>
                </div>
                <!-- 字幕/文档/数据:文件名全部列出 -->
                <div v-else class="flex flex-wrap gap-1.5">
                  <button
                    v-for="f in grp.files" :key="f.path" @click="viewFile(f)"
                    class="text-xs px-2 py-1 rounded border flex items-center gap-1"
                    :class="selFile?.path === f.path ? 'bg-blue-100 border-blue-200 text-blue-700' : 'border-gray-200 text-gray-600 hover:bg-gray-50'"
                  >
                    <component :is="grp.cat === '数据' ? Braces : FileText" :size="11" />
                    <span>{{ fname(f.path) }}</span>
                    <span v-if="f.size" class="text-gray-400">{{ fmtBytes(f.size) }}</span>
                  </button>
                </div>
              </div>
              <!-- 选中文件预览:容器留 min-height、加载态用浮层覆盖(不塌缩内容),避免点产物时页面抖动 -->
              <div v-if="selFile" class="relative border border-gray-100 rounded-lg p-3 bg-gray-50/40 min-h-[16rem]">
                <img v-if="selFile.kind === 'image'" :src="artUrl(selFile.path)" class="max-w-full rounded border border-gray-200" />
                <video v-else-if="selFile.kind === 'video'" :src="mediaUrl(selFile.path)" controls preload="metadata" class="max-w-full rounded border border-gray-200" />
                <audio v-else-if="selFile.kind === 'audio'" :src="mediaUrl(selFile.path)" controls class="w-full" />
                <div v-else-if="fileErr" class="text-xs text-red-600">{{ fileErr }}</div>
                <MarkdownViewer v-else-if="selFile.path.endsWith('.md')" :content="fileContent" :job-id="jobId" />
                <pre v-else-if="selFile.kind === 'text' || selFile.kind === 'json'" class="text-xs whitespace-pre-wrap break-all max-h-[45vh] overflow-auto">{{ fileContent }}</pre>
                <!-- 二进制/不可文本预览(PDF 等):给下载/新标签打开链接,不当文本渲染(防卡死)。 -->
                <a v-else :href="artUrl(selFile.path)" target="_blank" rel="noopener"
                   class="text-xs text-blue-600 hover:text-blue-700 inline-flex items-center gap-1">
                  <Package :size="13" />在新标签打开 / 下载（{{ selFile.path.split('/').pop() }}）
                </a>
                <!-- 文本加载:浮层覆盖,旧内容保持原高度不塌缩 -->
                <div v-if="fileLoading" class="absolute inset-0 flex items-center justify-center bg-gray-50/70 text-xs text-gray-400 rounded-lg">加载中…</div>
              </div>
            </div>
            <div v-else class="text-xs text-gray-400">该步骤无产物文件</div>
            </template>
          </div>

          <!-- 日志(本步运行日志) -->
          <div v-if="['done', 'failed', 'running'].includes(selStep.status)" class="mt-4 pt-3 border-t border-gray-100">
            <div class="flex items-center gap-2 mb-1.5">
              <span class="text-xs font-semibold text-gray-700 flex items-center gap-1.5"><FileText :size="13" class="text-gray-500" />日志</span>
              <button @click="toggleLog" class="text-xs text-blue-600 hover:text-blue-700 flex items-center gap-0.5">
                <ChevronRight :size="12" :class="logOpen ? 'rotate-90' : ''" class="transition-transform" />{{ logOpen ? '收起' : '展开' }}
              </button>
            </div>
            <div v-if="logOpen">
              <div v-if="logLoading" class="text-xs text-gray-400">加载中…</div>
              <div v-else-if="logErr" class="text-xs text-gray-400">{{ logErr }}</div>
              <div v-else-if="!logText.trim()" class="text-xs text-gray-400">该步骤无日志输出</div>
              <pre v-else class="text-xs bg-gray-50 text-gray-800 border border-gray-200 rounded-lg p-3 whitespace-pre-wrap break-all max-h-[45vh] overflow-auto">{{ logText }}</pre>
            </div>
          </div>
    </template>
    <div v-else class="text-sm text-gray-400 py-12 text-center">从上方流程图点选步骤，查看详情与产物</div>
  </div>
</template>
