<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useApi } from '../../composables/useApi'
import MarkdownViewer from '../notes/MarkdownViewer.vue'
import { fmtDateTime, fmtDuration } from '../../utils/datetime'
import { statusLabel } from '../../utils/status'
import type { StepInfo, StepUsage } from '../../types'
import { Check, X, Minus, Loader, Clock, ChevronRight, FileText, Braces, Package, Coins } from 'lucide-vue-next'

const props = defineProps<{ jobId: string; steps: StepInfo[] }>()
const api = useApi()

const statusIcon: Record<string, any> = { done: Check, failed: X, skipped: Minus, running: Loader }
const statusColor: Record<string, string> = {
  done: 'bg-green-500 text-white', failed: 'bg-red-500 text-white',
  running: 'bg-blue-500 text-white animate-pulse', skipped: 'bg-gray-300 text-gray-500',
  waiting: 'bg-gray-200 text-gray-400', ready: 'bg-yellow-400 text-white',
}
// 状态文案统一走 utils/status.statusLabel(避免与 StatusBadge 文案漂移);配色保留本组件 Tailwind 体系。

// 产出摘要:把 step.meta 渲染成友好「标签：值」。未知键回退原键,内部键跳过。
const META_LABELS: Record<string, string> = {
  frames: '关键帧', events: '时间节', lines: '字幕行', chunks: '分块', mode: '模式',
  kept: '保留帧', scenes: '场景数', count: '数量', danmaku: '弹幕条', sections: '章节',
  figures: '图表', duration: '时长', words: '字数', pages: '页数', provider: '模型',
}
const MODE_LABELS: Record<string, string> = { zh: '加标点', translate: '翻译为中文' }
const META_SKIP = new Set(['pct', 'current', 'total', 'exec_id', 'worker'])

interface AFile { path: string; kind: string }
interface Group { step: string; label: string; files: AFile[] }
const groups = ref<Group[]>([])
const filesByStep = computed<Record<string, AFile[]>>(() => {
  const m: Record<string, AFile[]> = {}
  for (const g of groups.value) m[g.step] = g.files
  return m
})

const sel = ref('')                      // 选中步骤名
const selFile = ref<AFile | null>(null)
const fileContent = ref('')
const fileLoading = ref(false)
const fileErr = ref('')
const artOpen = ref(true)        // 产物默认展开,可折叠
const logOpen = ref(false)       // 日志默认折叠
const logText = ref('')
const logLoading = ref(false)
const logErr = ref('')

const selStep = computed(() => props.steps.find(s => s.name === sel.value) || null)
const selFiles = computed(() => filesByStep.value[sel.value] || [])

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
    const r = await api.get<{ groups: Group[] }>(`/api/jobs/${props.jobId}/artifacts`)
    groups.value = r.groups || []
  } catch { groups.value = [] }
}

// 逐次 AI 调用明细(按步聚合;一个步可能多次调用 → 取该步全部行)。
const usage = ref<StepUsage[]>([])
async function loadUsage() {
  try {
    const r = await api.get<{ usage: StepUsage[] }>(`/api/jobs/${props.jobId}/usage`)
    usage.value = r.usage || []
  } catch { usage.value = [] }
}
const selUsage = computed(() => usage.value.filter(u => u.step === sel.value))
const fmtCost = (v: number) => `$${(v ?? 0).toFixed(4)}`
const costSuffix = (provider: string) => (provider === 'claude-cli' ? '（等价）' : '')

function pickDefault() {
  const s = props.steps
  if (!s.length) return
  const running = s.find(x => x.status === 'running')
  const lastDone = [...s].reverse().find(x => x.status === 'done' || x.status === 'failed')
  selectStep((running || lastDone || s[0]).name)
}

function selectStep(name: string) {
  if (!name) return
  sel.value = name
  selFile.value = null; fileContent.value = ''; fileErr.value = ''
  logOpen.value = false; logText.value = ''; logErr.value = ''
  const f = (filesByStep.value[name] || [])[0]
  if (f) viewFile(f)
}

async function viewFile(f: AFile) {
  selFile.value = f; fileErr.value = ''
  // 二进制(图片/视频/音频)直接用 <img>/<video>/<audio> 播放,不当文本拉取。
  if (f.kind === 'image' || f.kind === 'video' || f.kind === 'audio') { fileContent.value = ''; return }
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
    try { logText.value = await api.getText(`/api/jobs/${props.jobId}/steps/${sel.value}/log`) }
    catch (e: any) { logErr.value = e?.status === 404 ? '该步骤暂无日志' : (e?.message || '日志加载失败') }
    finally { logLoading.value = false }
  }
}

onMounted(async () => { await Promise.all([loadGroups(), loadUsage()]); pickDefault() })
// steps 可能在 detail 加载后才到;若还没选中则补选默认。
watch(() => props.steps.map(s => s.name).join(','), () => { if (!sel.value) pickDefault() })
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl p-4">
    <h3 class="text-sm font-semibold text-gray-700 mb-3">步骤与产物</h3>
    <div class="grid md:grid-cols-[300px_1fr] gap-4">
      <!-- 左:步骤时间线(全部步骤直接铺开,不加内部滚动条) -->
      <div class="space-y-0 md:border-r md:border-gray-100 md:pr-2">
        <button
          v-for="(s, idx) in steps" :key="s.name" @click="selectStep(s.name)"
          class="w-full text-left rounded-lg px-2 py-2 flex gap-2.5 transition-colors"
          :class="sel === s.name ? 'bg-blue-50 ring-1 ring-blue-200' : 'hover:bg-gray-50'"
        >
          <div class="flex flex-col items-center">
            <span
              class="w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0"
              :class="statusColor[s.status] || statusColor.waiting"
            >
              <component :is="statusIcon[s.status]" v-if="statusIcon[s.status]" :size="13" />
              <span v-else class="text-[10px]">{{ idx + 1 }}</span>
            </span>
            <div v-if="idx < steps.length - 1" class="w-0.5 flex-1 min-h-[0.5rem] my-0.5 bg-gray-200" />
          </div>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5">
              <span class="text-sm font-medium truncate" :class="s.status === 'waiting' ? 'text-gray-400' : 'text-gray-800'">{{ stepLabel(s) }}</span>
              <span class="text-[10px] px-1 py-0.5 rounded flex-shrink-0" :class="statusColor[s.status] || statusColor.waiting">{{ statusLabel(s.status) }}</span>
            </div>
            <div class="text-[11px] text-gray-400 font-mono mt-0.5">{{ s.name }}</div>
            <div v-if="stepPct(s) != null" class="mt-1 w-full bg-gray-200 rounded-full h-1">
              <div class="bg-blue-500 h-full rounded-full transition-all" :style="{ width: `${stepPct(s)}%` }" />
            </div>
            <div v-else-if="s.duration_sec && ['done', 'failed'].includes(s.status)" class="text-[11px] text-gray-400 mt-0.5">耗时 {{ fmtDuration(s.duration_sec, { decimalSeconds: true }) }}</div>
          </div>
        </button>
      </div>

      <!-- 右:选中步骤详情 + 产物 -->
      <div class="min-w-0">
        <template v-if="selStep">
          <div class="flex items-center gap-2 flex-wrap">
            <h4 class="text-base font-semibold text-gray-800">{{ stepLabel(selStep) }}</h4>
            <span class="text-xs px-1.5 py-0.5 rounded" :class="statusColor[selStep.status] || statusColor.waiting">{{ statusLabel(selStep.status) }}</span>
            <span class="text-xs text-gray-400 font-mono">{{ selStep.name }}</span>
          </div>

          <!-- 时间仅对真正跑过的步骤显示;等待/就绪(可能被重跑重置)不展示旧时间 -->
          <div v-if="['done', 'failed', 'running'].includes(selStep.status)" class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500 mt-2">
            <span><Clock :size="12" class="inline -mt-0.5" /> 开始 {{ fmtDateTime(selStep.started_at) }}</span>
            <span>结束 {{ selStep.status === 'running' ? '进行中' : fmtDateTime(selStep.finished_at) }}</span>
            <span>耗时 {{ selStep.status === 'running' ? '进行中' : fmtDuration(selStep.duration_sec, { decimalSeconds: true }) }}</span>
            <span v-if="selStep.worker_id">由 <span class="font-mono text-gray-700">{{ selStep.worker_id }}</span> 完成</span>
          </div>
          <div v-else-if="['waiting', 'ready'].includes(selStep.status)" class="text-xs text-gray-400 mt-2">尚未运行</div>

          <div v-if="stepPct(selStep) != null" class="mt-2 w-full bg-gray-200 rounded-full h-1.5">
            <div class="bg-blue-500 h-full rounded-full" :style="{ width: `${stepPct(selStep)}%` }" />
          </div>

          <!-- 失败原因:仅失败步骤显示(done 步骤的历史 error 如 timeout 不算失败) -->
          <p v-if="selStep.error && selStep.status === 'failed'" class="text-xs text-red-600 mt-2 break-all bg-red-50 rounded p-2">✗ {{ selStep.error }}</p>

          <!-- 跳过说明 -->
          <div v-if="selStep.status === 'skipped'" class="mt-3 text-xs text-gray-500 bg-gray-50 rounded p-2">
            已跳过{{ selStep.meta?.reason ? '：' + selStep.meta.reason : '（不满足运行条件，例如视频自带字幕则无需语音转写）' }}
          </div>

          <!-- 产出摘要(可读,替代原始 JSON) -->
          <div v-if="metaRows(selStep).length" class="mt-3 flex flex-wrap gap-2">
            <span v-for="r in metaRows(selStep)" :key="r.k" class="text-xs bg-gray-50 border border-gray-100 rounded px-2 py-1 text-gray-600">
              {{ r.k }}：<span class="text-gray-800 font-medium">{{ r.v }}</span>
            </span>
          </div>

          <!-- AI 用量(本步 AI 调用:token/缓存/命中率/成本/轮数/worker;claude-cli 成本标「等价」) -->
          <div v-if="selUsage.length" class="mt-3 pt-3 border-t border-gray-100">
            <div class="text-xs font-semibold text-gray-700 flex items-center gap-1.5 mb-2"><Coins :size="13" class="text-gray-500" />AI 用量</div>
            <div v-for="(u, i) in selUsage" :key="i" class="text-xs text-gray-600 bg-gray-50 border border-gray-100 rounded p-2 mb-1.5">
              <div class="flex items-center gap-2 flex-wrap mb-1">
                <span class="font-mono text-gray-800">{{ u.model }}</span>
                <span class="text-gray-400">{{ u.provider }}</span>
                <span class="ml-auto text-gray-800 font-medium">{{ fmtCost(u.cost_usd) }}<span class="text-gray-400">{{ costSuffix(u.provider) }}</span></span>
              </div>
              <div class="flex flex-wrap gap-x-3 gap-y-0.5 text-gray-500">
                <span>入 {{ u.input_tokens.toLocaleString() }}</span>
                <span>出 {{ u.output_tokens.toLocaleString() }}</span>
                <span>读缓存 {{ u.cache_read_tokens.toLocaleString() }}</span>
                <span>写缓存 {{ u.cache_creation_tokens.toLocaleString() }}</span>
                <span>命中 {{ u.cache_hit_rate_pct }}%</span>
                <span v-if="u.num_turns">轮数 {{ u.num_turns }}</span>
                <span v-if="u.duration_sec">耗时 {{ fmtDuration(u.duration_sec, { decimalSeconds: true }) }}</span>
                <span v-if="u.worker_id">worker <span class="font-mono">{{ u.worker_id }}</span></span>
              </div>
            </div>
          </div>

          <!-- ════ 产物(本步产出的文件)════ -->
          <div v-if="['done', 'failed', 'running'].includes(selStep.status)" class="mt-4 pt-3 border-t border-gray-100">
            <div class="flex items-center gap-2 mb-2">
              <span class="text-xs font-semibold text-gray-700 flex items-center gap-1.5"><Package :size="13" class="text-gray-500" />产物 <span class="font-normal text-gray-400">（{{ selFiles.length }}）</span></span>
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
                <pre v-else class="text-xs whitespace-pre-wrap break-all">{{ fileContent }}</pre>
                <!-- 文本加载:浮层覆盖,旧内容保持原高度不塌缩 -->
                <div v-if="fileLoading" class="absolute inset-0 flex items-center justify-center bg-gray-50/70 text-xs text-gray-400 rounded-lg">加载中…</div>
              </div>
            </div>
            <div v-else class="text-xs text-gray-400">该步骤无产物文件</div>
            </template>
          </div>

          <!-- ════ 日志(本步运行日志)════ -->
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
              <pre v-else class="text-xs bg-gray-50 text-gray-800 border border-gray-200 rounded-lg p-3 max-h-72 overflow-auto whitespace-pre-wrap break-all">{{ logText }}</pre>
            </div>
          </div>
        </template>
        <div v-else class="text-sm text-gray-400 py-12 text-center">← 选择左侧步骤查看详情与产物</div>
      </div>
    </div>
  </div>
</template>
