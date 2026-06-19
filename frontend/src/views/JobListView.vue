<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useJobStore } from '../stores/jobs'
import { useDomainStore } from '../stores/domains'
import StatusBadge from '../components/common/StatusBadge.vue'
import { fmtDateTime } from '../utils/datetime'
import type { JobSummary, JobFacets } from '../types'
import {
  Inbox, Play, FileText, Newspaper, Headphones, ChevronRight, X, RotateCcw,
} from 'lucide-vue-next'

// 所有来源(原型 #content)：跨知识库的全部投递。三组筛选(状态/来源/知识库)，
// 组内多选、跨组取交集、各组可单独清除 + 清除全部。
// 过滤策略:某组恰好选中 1 项(且映射到单个后端值)时,作为 fetchList 的服务端单值参数;
// 该组选 0/≥2 项(或映射到多值)时,参数不传、改在返回页上对该维度做客户端过滤。
// chip 计数始终用后端聚合的 facets(对全量 jobs,不随已加载列表变化)。
const router = useRouter()
const jobStore = useJobStore()
const domainStore = useDomainStore()

const PAGE = 20
const offset = ref(0)
const loadError = ref('')

// 后端聚合分面(全量 jobs):chip 计数与知识库可选项的唯一来源。
const facets = ref<JobFacets>({ source: {}, domain: {}, status: {} })

// 三组筛选选中集合。
const fStatus = ref<Set<string>>(new Set())
const fSource = ref<Set<string>>(new Set())
const fDomain = ref<Set<string>>(new Set())

// 状态枚举(Job)。downloading/processing/pending 合并为「处理中」一档展示但底层映射到多状态。
const STATUS_OPTS: { key: string; label: string; match: string[] }[] = [
  { key: 'done', label: '已完成', match: ['done'] },
  { key: 'processing', label: '处理中', match: ['downloading', 'processing', 'pending'] },
  { key: 'failed', label: '失败', match: ['failed'] },
]
// 来源枚举：与后端 detect_source 返回值对齐(bilibili/youtube/arxiv/http_article/podcast/upload)。
const SOURCE_OPTS: { key: string; label: string; match: string[] }[] = [
  { key: 'bilibili', label: 'Bilibili', match: ['bilibili'] },
  { key: 'youtube', label: 'YouTube', match: ['youtube'] },
  { key: 'arxiv', label: 'arXiv', match: ['arxiv'] },
  { key: 'http_article', label: '公众号', match: ['http_article'] },
  { key: 'podcast', label: '播客', match: ['podcast'] },
  { key: 'upload', label: '本地', match: ['upload'] },
]

function toggle(group: 'status' | 'source' | 'domain', key: string) {
  const ref_ = group === 'status' ? fStatus : group === 'source' ? fSource : fDomain
  const next = new Set(ref_.value)
  next.has(key) ? next.delete(key) : next.add(key)
  ref_.value = next
  load()
}
function clearStatus() { fStatus.value = new Set(); load() }
function clearSource() { fSource.value = new Set(); load() }
function clearDomain() { fDomain.value = new Set(); load() }
function clearAll() { fStatus.value = new Set(); fSource.value = new Set(); fDomain.value = new Set(); load() }

const anyFilter = computed(() => fStatus.value.size || fSource.value.size || fDomain.value.size)

// 状态:多选时合并各档对应的底层枚举集合(供客户端过滤)。
const statusMatchSet = computed<Set<string>>(() => {
  const s = new Set<string>()
  for (const opt of STATUS_OPTS) if (fStatus.value.has(opt.key)) opt.match.forEach(m => s.add(m))
  return s
})
const sourceMatchSet = computed<Set<string>>(() => {
  const s = new Set<string>()
  for (const opt of SOURCE_OPTS) if (fSource.value.has(opt.key)) opt.match.forEach(m => s.add(m))
  return s
})

// ── 服务端单值下推:某组恰好选中 1 项且映射到单个后端值时,作为 fetchList 参数 ──
// 状态档可能对应多个底层枚举(如「处理中」→3 个),那种情况退化为客户端过滤。
const serverStatus = computed<string | undefined>(() => {
  if (fStatus.value.size !== 1) return undefined
  const opt = STATUS_OPTS.find(o => fStatus.value.has(o.key))
  return opt && opt.match.length === 1 ? opt.match[0] : undefined
})
const serverSource = computed<string | undefined>(() =>
  fSource.value.size === 1 ? [...fSource.value][0] : undefined)
const serverDomain = computed<string | undefined>(() =>
  fDomain.value.size === 1 ? [...fDomain.value][0] : undefined)

// 客户端交集过滤:仅对「未下推到服务端」的维度生效(已下推的维度服务端已过滤干净)。
const filtered = computed<JobSummary[]>(() => {
  return jobStore.list.filter(j => {
    if (!serverStatus.value && statusMatchSet.value.size && !statusMatchSet.value.has(j.status)) return false
    if (!serverSource.value && sourceMatchSet.value.size && !sourceMatchSet.value.has(j.source || '')) return false
    if (!serverDomain.value && fDomain.value.size && !fDomain.value.has(j.domain)) return false
    return true
  })
})

// 各 chip 计数:后端聚合 facets(全量),不随已加载列表变化。
// 状态档把底层枚举求和(「处理中」= downloading+processing+pending)。
function countByStatus(opt: { match: string[] }): number {
  return opt.match.reduce((n, m) => n + (facets.value.status[m] || 0), 0)
}
function countBySource(opt: { match: string[] }): number {
  return opt.match.reduce((n, m) => n + (facets.value.source[m] || 0), 0)
}
function countByDomain(d: string): number {
  return facets.value.domain[d] || 0
}

// 知识库 display_name(来自 domains store)友好显示,缺省回退 domain 名。
const domainDisplay = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  domainStore.domains.forEach(d => { m[d.domain] = d.display_name || d.domain })
  return m
})
function domainLabel(d: string): string {
  return domainDisplay.value[d] || d
}

// 知识库可选项:来自后端 facets.domain 的 keys(全量出现过的 domain),升序。
const domainOpts = computed<string[]>(() => Object.keys(facets.value.domain).sort())

const fbarText = computed(() => {
  if (!anyFilter.value) return `未筛选 —— 共 ${jobStore.total} 条内容`
  return `已筛选 —— 显示 ${filtered.value.length} / ${jobStore.list.length} 条已加载内容`
})

const hasMore = computed(() => jobStore.list.length < jobStore.total)

const SOURCE_LABELS: Record<string, string> = {
  bilibili: 'Bilibili', youtube: 'YouTube', arxiv: 'arXiv',
  http_article: '公众号', podcast: '播客', upload: '本地', other: '其它',
}
function sourceLabel(s: string | null): string {
  return s ? (SOURCE_LABELS[s] || s) : '—'
}
const typeIcon: Record<string, any> = {
  video: Play, paper: FileText, article: Newspaper, audio: Headphones,
}
function typeClass(t: string): string {
  return ({ video: 't-video', paper: 't-paper', article: 't-article', audio: 't-audio' } as Record<string, string>)[t] || 't-video'
}

// 拉一次后端聚合分面(供 chip 计数 + 知识库可选项)。
async function loadFacets() {
  try {
    facets.value = await jobStore.fetchFacets()
  } catch { /* 计数缺失退化为 0,不阻塞列表 */ }
}

// 列表加载:把可下推的维度作为服务端参数,翻页 offset 归零。
async function load() {
  loadError.value = ''
  offset.value = 0
  try {
    await jobStore.fetchList({
      status: serverStatus.value,
      source: serverSource.value,
      domain: serverDomain.value,
      limit: PAGE,
      offset: 0,
    })
  } catch (e: any) {
    loadError.value = e?.message || '加载失败'
  }
}

async function loadMore() {
  if (jobStore.loading || !hasMore.value) return
  offset.value += PAGE
  try {
    await jobStore.fetchList({
      status: serverStatus.value,
      source: serverSource.value,
      domain: serverDomain.value,
      limit: PAGE,
      offset: offset.value,
      append: true,
    })
  } catch (e: any) {
    loadError.value = e?.message || '加载失败'
  }
}

// 失败行的快捷重试(列表内，不跳详情)。
async function retry(jobId: string) {
  try {
    await jobStore.retryJob(jobId)
    const j = jobStore.list.find(x => x.job_id === jobId)
    if (j) j.status = 'processing'
  } catch { /* 静默：详情页可见错误 */ }
}

// 滚动到底自动加载(原型 .load-hint 行为)。监听窗口滚动。
function onScroll() {
  const nearBottom = window.innerHeight + window.scrollY >= document.body.offsetHeight - 240
  if (nearBottom) loadMore()
}

onMounted(() => {
  domainStore.fetchAll().catch(() => {})
  loadFacets()
  load()
  window.addEventListener('scroll', onScroll, { passive: true })
})
onUnmounted(() => window.removeEventListener('scroll', onScroll))

function goDetail(id: string) {
  router.push(`/content/${id}`)
}

// 列表为空(无任何加载结果)时区分：真空 vs 仅被筛选清空。
const isInitialLoading = computed(() => jobStore.loading && jobStore.list.length === 0)
</script>

<template>
  <div class="page">
    <!-- 页头 -->
    <div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:18px">
      <div>
        <div class="h1"><Inbox :size="18" />所有来源</div>
        <div class="lead">跨知识库的所有投递，可按来源、类型、状态筛选。</div>
      </div>
    </div>

    <!-- 三组筛选 -->
    <div class="filters">
      <div class="fgroup">
        <span class="flabel">按状态</span>
        <span
          v-for="opt in STATUS_OPTS" :key="opt.key"
          class="chip" :class="{ on: fStatus.has(opt.key) }"
          @click="toggle('status', opt.key)"
        >{{ opt.label }} <span class="n">{{ countByStatus(opt) }}</span></span>
        <button v-if="fStatus.size" class="fclear" @click="clearStatus"><X :size="11" />清除</button>
      </div>

      <div class="fgroup">
        <span class="flabel">按来源</span>
        <span
          v-for="opt in SOURCE_OPTS" :key="opt.key"
          class="chip" :class="{ on: fSource.has(opt.key) }"
          @click="toggle('source', opt.key)"
        >{{ opt.label }} <span class="n">{{ countBySource(opt) }}</span></span>
        <button v-if="fSource.size" class="fclear" @click="clearSource"><X :size="11" />清除</button>
      </div>

      <div class="fgroup">
        <span class="flabel">按知识库</span>
        <span v-if="domainOpts.length === 0" class="dim" style="font-size:12px">暂无</span>
        <span
          v-for="d in domainOpts" :key="d"
          class="chip" :class="{ on: fDomain.has(d) }"
          @click="toggle('domain', d)"
        >{{ domainLabel(d) }} <span class="n">{{ countByDomain(d) }}</span></span>
        <button v-if="fDomain.size" class="fclear" @click="clearDomain"><X :size="11" />清除</button>
      </div>

      <div class="fbar">
        <span>{{ fbarText }}</span>
        <button v-if="anyFilter" class="ghost" @click="clearAll"><X :size="14" />清除全部</button>
      </div>
    </div>

    <!-- 加载态(首屏) -->
    <div v-if="isInitialLoading" class="card pad">
      <div class="state"><span class="spinner" />正在加载内容…</div>
    </div>

    <!-- 错误态 -->
    <div v-else-if="loadError && jobStore.list.length === 0" class="card pad">
      <div class="state">
        <Inbox class="big" />
        <div class="t">{{ loadError }}</div>
        <button class="btn" @click="load"><RotateCcw :size="14" />重试</button>
      </div>
    </div>

    <!-- 空态(库里没有内容) -->
    <div v-else-if="jobStore.list.length === 0" class="card pad">
      <div class="state">
        <Inbox class="big" />
        <div class="t">还没有任何内容</div>
      </div>
    </div>

    <!-- 空态(被筛选清空) -->
    <div v-else-if="filtered.length === 0" class="card pad">
      <div class="state">
        <Inbox class="big" />
        <div class="t">没有符合当前筛选的内容</div>
        <button class="btn" @click="clearAll"><X :size="14" />清除筛选</button>
      </div>
    </div>

    <!-- 列表 -->
    <template v-else>
      <div class="list">
        <div
          v-for="j in filtered" :key="j.job_id"
          class="row" :style="j.status === 'failed' ? 'cursor:default' : ''"
          @click="j.status !== 'failed' ? goDetail(j.job_id) : null"
        >
          <span class="type-pill" :class="typeClass(j.content_type)">
            <component :is="typeIcon[j.content_type] || FileText" />
          </span>
          <div class="body">
            <div class="title">{{ j.title || j.job_id }}</div>
            <div class="meta">
              <StatusBadge :status="j.status" />
              <span>{{ sourceLabel(j.source) }}</span>
              <template v-if="j.domain">
                <span class="sep">·</span><span>{{ domainLabel(j.domain) }}</span>
              </template>
              <span class="sep">·</span>
              <span class="dim">{{ fmtDateTime(j.created_at) }}</span>
            </div>
          </div>
          <button
            v-if="j.status === 'failed'"
            class="btn sm" @click.stop="retry(j.job_id)"
          ><RotateCcw :size="13" />重试</button>
          <ChevronRight v-else :size="16" class="dim" />
        </div>
      </div>

      <!-- 翻页：滚动到底自动加载 + 手动兜底 -->
      <div v-if="hasMore" class="load-hint">
        <template v-if="jobStore.loading"><span class="spinner" style="width:15px;height:15px;border-width:2px" />加载中…</template>
        <button v-else class="ghost" @click="loadMore">加载更多（已显示 {{ jobStore.list.length }} / {{ jobStore.total }}）</button>
      </div>
      <div v-else-if="!anyFilter && jobStore.list.length > 0" class="load-hint">已全部加载 · 共 {{ jobStore.total }} 条</div>
    </template>
  </div>
</template>
