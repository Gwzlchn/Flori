<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { useRouter } from 'vue-router'
import { useJobStore } from '../stores/jobs'
import { useDomainStore } from '../stores/domains'
import StatusBadge from '../components/common/StatusBadge.vue'
import { fmtDateTime } from '../utils/datetime'
import { contentTypeIcon, contentTypePill } from '../utils/contentType'
import { jobSourceLabel } from '../constants/sources'
import type { JobSummary, JobFacets } from '../types'
import { Inbox, ChevronRight, X, RotateCcw, Trash2, History } from 'lucide-vue-next'

const showToast = inject<(m: string, t?: 'success' | 'error' | 'info') => void>('showToast', () => {})

// 所有来源(原型 #content): 跨知识库的全部投递。三组筛选(状态/来源/知识库),
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

// 选择 / 删除(单条 + 批量)。删除走后端精准级联(队列/产物/用量/去重/DB)。
const selecting = ref(false)
const selected = ref<Set<string>>(new Set())
const deleting = ref(false)
function toggleSelecting() {
  selecting.value = !selecting.value
  if (!selecting.value) selected.value = new Set()
}
function toggleSel(id: string) {
  const n = new Set(selected.value)
  n.has(id) ? n.delete(id) : n.add(id)
  selected.value = n
}
async function deleteOne(id: string) {
  if (!confirm('删除这条内容?将级联清除其队列任务、产物、用量、订阅去重记录,不可恢复。')) return
  deleting.value = true
  try {
    await jobStore.deleteJob(id)
    showToast('已删除', 'success')
    await load()
  } catch { showToast('删除失败', 'error') } finally { deleting.value = false }
}
async function deleteSelected() {
  const ids = [...selected.value]
  if (!ids.length) return
  if (!confirm(`删除选中的 ${ids.length} 条内容?将级联清除各自的队列任务/产物/用量/去重记录,不可恢复。`)) return
  deleting.value = true
  try {
    const n = await jobStore.deleteJobs(ids)
    showToast(`已删除 ${n} 条`, 'success')
    selected.value = new Set()
    selecting.value = false
    await load()
  } catch { showToast('批量删除失败', 'error') } finally { deleting.value = false }
}

// 后端聚合分面(全量 jobs):chip 计数与知识库可选项的唯一来源。
const facets = ref<JobFacets>({ source: {}, domain: {}, status: {} })

// 三组筛选选中集合。
const fStatus = ref<Set<string>>(new Set())
const fSource = ref<Set<string>>(new Set())
const fDomain = ref<Set<string>>(new Set())

// 状态枚举(Job)。downloading/processing/pending 合并为「处理中」一档展示但底层映射到多状态。
const STATUS_OPTS: { key: string; label: string; match: string[] }[] = [
  { key: 'done', label: '已完成', match: ['done'] },
  { key: 'pending_activation', label: '待激活', match: ['pending_activation'] },
  { key: 'processing', label: '处理中', match: ['downloading', 'processing', 'pending'] },
  { key: 'failed', label: '失败', match: ['failed'] },
]
// 来源枚举: 与后端 detect_source 返回值对齐(bilibili/youtube/arxiv/http_article/podcast/upload)。
const SOURCE_OPTS: { key: string; label: string; match: string[] }[] = [
  { key: 'bilibili', label: 'Bilibili', match: ['bilibili'] },
  { key: 'youtube', label: 'YouTube', match: ['youtube'] },
  { key: 'arxiv', label: 'arXiv', match: ['arxiv'] },
  { key: 'http_article', label: '网页文章', match: ['http_article'] },
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

// 服务端单值下推: 某组恰好选中 1 项且映射到单个后端值时,作为 fetchList 参数。
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

// 来源标签 / 内容类型图标·配色:统一走共享单一来源(constants/sources、utils/contentType)。

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

const retryingAll = ref(false)
async function onRetryAllFailed() {
  if (!confirm('重试所有失败的 job?(各自从首个失败步重跑;缺凭证类如无 cookie 的 YouTube 会再次失败)')) return
  retryingAll.value = true
  try {
    const { retried } = await jobStore.retryAllFailed()
    showToast(`已发起重试 ${retried} 个失败 job`, 'success')
    await load()
  } catch {
    showToast('批量重试失败', 'error')
  } finally {
    retryingAll.value = false
  }
}

// 批量重建所有定义已过期(pipeline/prompt 已变)的内容为新版本,旧版保留可对比。
const rebuilding = ref(false)
async function onRebuildStale() {
  if (!confirm('重建所有「定义已过期」的内容?为受影响内容建新版本(只重跑变化的步骤及下游,旧版本保留对比)。')) return
  rebuilding.value = true
  try {
    const { rebuilt } = await jobStore.rebuildStale()
    showToast(rebuilt > 0 ? `已重建 ${rebuilt} 个过期内容` : '没有过期内容', 'success')
    await load()
  } catch {
    showToast('批量重建失败', 'error')
  } finally {
    rebuilding.value = false
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

// 失败行的快捷重试(列表内,不跳详情)。乐观置「处理中」,失败给出可见提示。
async function retry(jobId: string) {
  try {
    await jobStore.retryJob(jobId)
    const j = jobStore.list.find(x => x.job_id === jobId)
    if (j) j.status = 'processing'
    showToast('已提交重试', 'success')
  } catch (e: any) {
    showToast(e?.message || '重试失败', 'error')
  }
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

// 列表为空(无任何加载结果)时区分: 真空 vs 仅被筛选清空。
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
      <button class="btn" style="margin-left:auto" :disabled="retryingAll" @click="onRetryAllFailed">
        <RotateCcw :size="14" />{{ retryingAll ? '重试中…' : '重试全部失败' }}
      </button>
      <button class="btn" data-testid="rebuild-stale" :disabled="rebuilding" @click="onRebuildStale"
        title="为「pipeline/prompt 定义已变」的内容重建新版本(只重跑变化步骤,旧版保留)">
        <History :size="14" />{{ rebuilding ? '重建中…' : '重建所有过期' }}
      </button>
      <button class="btn" data-testid="select-toggle" :class="{ on: selecting }" @click="toggleSelecting">
        <component :is="selecting ? X : Trash2" :size="14" />{{ selecting ? '退出选择' : '选择删除' }}
      </button>
    </div>

    <!-- 批量删除条(仅选择模式) -->
    <div v-if="selecting" class="batchbar">
      <span>已选 <b>{{ selected.size }}</b> 条</span>
      <button
        class="btn sm danger" data-testid="batch-delete"
        :disabled="!selected.size || deleting" @click="deleteSelected"
      ><Trash2 :size="13" />{{ deleting ? '删除中…' : `删除选中 (${selected.size})` }}</button>
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
          class="row" :class="{ sel: selecting && selected.has(j.job_id) }"
          :style="(selecting || j.status !== 'failed') ? '' : 'cursor:default'"
          @click="selecting ? toggleSel(j.job_id) : (j.status !== 'failed' ? goDetail(j.job_id) : null)"
        >
          <input
            v-if="selecting" type="checkbox" class="rowcheck"
            :checked="selected.has(j.job_id)" @click.stop="toggleSel(j.job_id)"
          />
          <span class="type-pill" :class="contentTypePill(j.content_type)">
            <component :is="contentTypeIcon(j.content_type)" />
          </span>
          <div class="body">
            <div class="title">{{ j.title || j.job_id }}</div>
            <div class="meta">
              <StatusBadge :status="j.status" />
              <span>{{ jobSourceLabel(j.source) }}</span>
              <template v-if="j.domain">
                <span class="sep">·</span><span>{{ domainLabel(j.domain) }}</span>
              </template>
              <span class="sep">·</span>
              <span class="dim">{{ fmtDateTime(j.created_at) }}</span>
              <template v-if="(j.versions ?? 1) > 1">
                <span class="sep">·</span>
                <span class="badge b-mut" :title="`同源内容共 ${j.versions} 个快照,可在详情页跳转历史版本`">{{ j.versions }} 版本</span>
              </template>
            </div>
          </div>
          <template v-if="!selecting">
            <button
              v-if="j.status === 'failed'"
              class="btn sm" @click.stop="retry(j.job_id)"
            ><RotateCcw :size="13" />重试</button>
            <button
              class="btn sm danger" data-testid="row-delete" title="删除"
              :disabled="deleting" @click.stop="deleteOne(j.job_id)"
            ><Trash2 :size="13" /></button>
            <ChevronRight v-if="j.status !== 'failed'" :size="16" class="dim" />
          </template>
        </div>
      </div>

      <!-- 翻页: 滚动到底自动加载 + 手动兜底 -->
      <div v-if="hasMore" class="load-hint">
        <template v-if="jobStore.loading"><span class="spinner" style="width:15px;height:15px;border-width:2px" />加载中…</template>
        <button v-else class="ghost" @click="loadMore">加载更多（已显示 {{ jobStore.list.length }} / {{ jobStore.total }}）</button>
      </div>
      <div v-else-if="!anyFilter && jobStore.list.length > 0" class="load-hint">已全部加载 · 共 {{ jobStore.total }} 条</div>
    </template>
  </div>
</template>

<style scoped>
.batchbar {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 12px; padding: 8px 12px;
  background: var(--bad-bg, #fdecec); border: 1px solid var(--bad, #d33); border-radius: 8px;
  font-size: 13px;
}
.btn.danger { color: var(--bad, #d33); border-color: var(--bad, #d33); }
.btn.danger:hover:not(:disabled) { background: var(--bad-bg, #fdecec); }
.row.sel { background: var(--brand-50, #eef3ff); }
.rowcheck { width: 16px; height: 16px; cursor: pointer; flex: 0 0 auto; }
</style>
