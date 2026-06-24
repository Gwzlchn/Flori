<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCollectionStore } from '../stores/collections'
import { useJobStore } from '../stores/jobs'
import { useGlobalStore } from '../stores/global'
import { useApi } from '../composables/useApi'
import StatusBadge from '../components/common/StatusBadge.vue'
import DeleteCollectionDialog from '../components/collection/DeleteCollectionDialog.vue'
import { fmtDateTime } from '../utils/datetime'
import { contentTypeIcon, contentTypePill, contentTypeLabel } from '../utils/contentType'
import type { Collection, JobSummary } from '../types'
import { sourceLabelOf, sourceBadge, sourceMeta, sourceHomeUrl, subState, SUB_STATE_META } from '../constants/sources'
import {
  Rss, Folder, RefreshCw, Info, ExternalLink, LayoutList, ChevronRight,
  Trash2,
} from 'lucide-vue-next'

// 集合详情（原型 #collection）：头部信息 + 订阅源（开关/同步） + 名下内容列表。
const route = useRoute()
const router = useRouter()
const store = useCollectionStore()
const jobStore = useJobStore()
const global = useGlobalStore()
const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const id = String(route.params.id)
const collection = ref<Collection | null>(null)
const jobs = ref<JobSummary[]>([])
const total = ref(0)
const loading = ref(false)
const notFound = ref(false)
const error = ref('')

const syncing = ref(false)
const togglingSync = ref(false)
const deleting = ref(false)
const showDelete = ref(false)

// 来源(派生)展示:徽标、图标、人类名、原始链接。
const srcBadge = computed(() => sourceBadge(sourceLabelOf(collection.value?.subscription)))
const srcIcon = computed(() => sourceMeta(collection.value?.subscription?.source_type || '')?.icon ?? Rss)
const srcTypeLabel = computed(() => sourceMeta(collection.value?.subscription?.source_type || '')?.label || '订阅源')
const srcHome = computed(() => collection.value?.subscription ? sourceHomeUrl(collection.value.subscription) : null)

// 订阅状态(5 态:订阅中/暂停/从未/出错/同步中)——取自 constants/sources,与侧栏/列表统一。
const sub = computed(() => collection.value?.subscription ?? null)
const subSt = computed(() => (sub.value ? subState(sub.value as any) : null))
const subMeta = computed(() => (subSt.value ? SUB_STATE_META[subSt.value] : null))
const syncError = computed(() => ((sub.value as any)?.last_sync_error as string | null) ?? null)
// 内容状态分布(后端集合详情返回 status_counts);用于信息卡补全 + 集合级重试可发现性。
const counts = computed(
  () => (collection.value as any)?.status_counts as
    { done: number; processing: number; failed: number; pending: number } | undefined,
)

async function load() {
  loading.value = true
  notFound.value = false
  error.value = ''
  try {
    collection.value = await store.get(id)
    // 面包屑显真实集合名(替代通用「集合详情」)
    global.setCrumbs([
      { t: '知识库', to: '/' },
      { t: '集合', to: '/collections' },
      { t: collection.value?.name || id },
    ])
    const res = await store.fetchJobs(id)
    jobs.value = res.items
    total.value = res.total
  } catch (e: any) {
    const msg = String(e?.message ?? '')
    if (msg.includes('404')) notFound.value = true
    else error.value = msg || '加载失败'
  } finally {
    loading.value = false
  }
}

// 立即同步：拉取 UP 全部视频，新视频自动建内容入本集合。返回 {new,total}。
async function syncNow() {
  const c = collection.value
  if (!c?.subscription || syncing.value) return
  syncing.value = true
  try {
    const r = await api.post<{ new: number; total: number }>(`/api/collections/${c.id}/sync`)
    showToast(`同步完成：新增 ${r.new} 个（共 ${r.total}）`, 'success')
    await load()
  } catch (e: any) {
    showToast(e?.message || '同步失败', 'error')
  } finally {
    syncing.value = false
  }
}

// 重试本集合下的失败任务(scoped 批量重试,复用全局 retry-failed + collection_id 过滤)。
const retrying = ref(false)
async function retryFailed() {
  const c = collection.value
  const n = counts.value?.failed ?? 0
  if (!c || !n || retrying.value) return
  if (!confirm(`重试本集合 ${n} 个失败任务?(各自从首个失败步重跑)`)) return
  retrying.value = true
  try {
    const { retried } = await jobStore.retryFailedInCollection(c.id)
    showToast(`已重试 ${retried} 个失败任务`, 'success')
    await load()
  } catch (e: any) {
    showToast(e?.message || '重试失败', 'error')
  } finally {
    retrying.value = false
  }
}

// 自动同步开关：订阅是集合属性，走集合端点 PUT {sync_enabled}。
async function toggleAutoSync() {
  const c = collection.value
  if (!c?.subscription || togglingSync.value) return
  togglingSync.value = true
  try {
    await api.put(`/api/collections/${c.id}`, { sync_enabled: !c.subscription.enabled })
    await load()
  } catch (e: any) {
    showToast(e?.message || '操作失败', 'error')
  } finally {
    togglingSync.value = false
  }
}

function openJob(j: JobSummary) {
  router.push(`/content/${j.job_id}`)
}

async function onDelete(purge: boolean) {
  const c = collection.value
  if (!c) return
  deleting.value = true
  try {
    await store.remove(c.id, purge)
    showToast(purge ? '集合及内容已删除' : '集合已删除（内容保留）', 'success')
    router.push('/collections')
  } catch (e: any) {
    showToast(e?.message || '删除失败', 'error')
    deleting.value = false
  }
}

const headerSub = computed(() => {
  const c = collection.value
  if (!c) return ''
  const parts: string[] = []
  if (c.domain) parts.push(c.domain)
  parts.push(`${c.job_count} 条内容`)
  if (c.subscription?.last_synced_at) parts.push(`上次同步 ${fmtDateTime(c.subscription.last_synced_at)}`)
  return parts.join(' · ')
})

onMounted(load)
onBeforeUnmount(() => global.setCrumbs(null))
</script>

<template>
  <section class="page">
    <!-- 404 -->
    <div v-if="notFound" class="card pad" style="text-align:center;padding:40px 18px">
      <p class="muted" style="margin-bottom:14px">集合不存在或已删除</p>
      <button class="btn" @click="router.push('/collections')">返回集合列表</button>
    </div>

    <!-- 错误态 -->
    <div v-else-if="error && !collection" class="card pad" style="text-align:center">
      <p class="muted" style="margin-bottom:12px">{{ error }}</p>
      <button class="btn" @click="load"><RefreshCw :size="14" />重试</button>
    </div>

    <!-- 加载态 -->
    <div v-else-if="loading && !collection" class="card pad" style="text-align:center;color:var(--ink-500)">
      加载中…
    </div>

    <template v-else-if="collection">
      <!-- 头部：图标 + 名字 + 类型徽章 + 立即同步 -->
      <div style="display:flex;align-items:center;gap:13px;margin-bottom:6px">
        <span
          class="cic"
          :class="collection.subscription ? 'sub' : 'man'"
          style="width:42px;height:42px;border-radius:11px"
        >
          <component :is="collection.subscription ? srcIcon : Folder" :size="18" />
        </span>
        <div style="min-width:0">
          <div class="h1">
            {{ collection.name }}
            <span v-if="collection.subscription" class="badge" :class="srcBadge.cls" style="margin-left:4px">
              <component :is="srcBadge.icon" :size="12" />{{ srcBadge.text }}
            </span>
            <span v-else class="badge b-mut" style="margin-left:4px">手动</span>
          </div>
          <div class="lead">{{ headerSub }}</div>
        </div>
        <button
          v-if="collection.subscription"
          class="btn sm"
          style="margin-left:auto"
          :disabled="syncing"
          @click="syncNow"
        >
          <RefreshCw :size="13" :class="{ spin: syncing }" />{{ syncing ? '同步中…' : '立即同步' }}
        </button>
        <button
          class="btn sm del-btn"
          :style="{ marginLeft: collection.subscription ? '0' : 'auto' }"
          @click="showDelete = true"
        >
          <Trash2 :size="13" />删除
        </button>
      </div>

      <!-- 信息卡 + 订阅源卡 -->
      <div class="grid2" style="margin-top:18px;align-items:start">
        <div class="card pad">
          <div class="card-h"><Info :size="15" />集合信息</div>
          <table class="kv">
            <tr><td>ID</td><td class="mono">{{ collection.id }}</td></tr>
            <tr><td>知识库</td><td>{{ collection.domain || '—' }}</td></tr>
            <tr>
              <td>标签</td>
              <td>
                <template v-if="collection.tags.length">
                  <span v-for="t in collection.tags" :key="t" class="tag" style="margin-right:5px">{{ t }}</span>
                </template>
                <span v-else>—</span>
              </td>
            </tr>
            <tr><td>内容</td><td>{{ collection.job_count }} 条</td></tr>
            <tr><td>创建时间</td><td>{{ fmtDateTime(collection.created_at) }}</td></tr>
            <tr v-if="counts">
              <td>状态分布</td>
              <td>
                <div class="counts">
                  <span>完成 {{ counts.done }}</span><span class="sep">·</span>
                  <span>处理中 {{ counts.processing }}</span><span class="sep">·</span>
                  <span>待处理 {{ counts.pending }}</span><span class="sep">·</span>
                  <span :class="{ bad: counts.failed > 0 }">失败 {{ counts.failed }}</span>
                </div>
                <button
                  v-if="counts.failed > 0"
                  class="btn sm danger retry-btn"
                  :disabled="retrying"
                  @click="retryFailed"
                >
                  <RefreshCw :size="12" :class="{ spin: retrying }" />重试本集合失败 {{ counts.failed }}
                </button>
              </td>
            </tr>
            <tr><td>描述</td><td>{{ collection.description || '—' }}</td></tr>
          </table>
        </div>

        <div v-if="collection.subscription" class="card pad">
          <div class="card-h"><Rss :size="15" />订阅源</div>
          <table class="kv" style="margin-bottom:13px">
            <tr><td>来源</td><td>{{ srcTypeLabel }}</td></tr>
            <tr>
              <td>来源地址</td>
              <td>
                <a v-if="srcHome" class="ghost" style="color:var(--info)" :href="srcHome" target="_blank" rel="noopener">
                  {{ collection.subscription.source_id }}<ExternalLink :size="13" />
                </a>
                <span v-else class="mono" style="word-break:break-all">{{ collection.subscription.source_id }}</span>
              </td>
            </tr>
            <tr><td>已入库</td><td>{{ collection.job_count }}</td></tr>
            <tr><td>上次同步</td><td>{{ collection.subscription.last_synced_at ? fmtDateTime(collection.subscription.last_synced_at) : '从未' }}</td></tr>
            <tr>
              <td>追更状态</td>
              <td>
                <span class="substat">
                  <span class="sub-dot" :class="subSt"></span>{{ subMeta?.tip }}
                </span>
                <div v-if="subSt === 'error' && syncError" class="sub-err">{{ syncError }}</div>
              </td>
            </tr>
          </table>
          <div style="display:flex;align-items:center;gap:12px;padding-top:11px;border-top:1px solid var(--line-soft)">
            <button class="btn sm" :disabled="syncing" @click="syncNow">
              <RefreshCw :size="13" :class="{ spin: syncing }" />立即同步
            </button>
            <span style="margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-600)">
              自动同步
              <div
                class="switch"
                :class="{ on: collection.subscription.enabled, disabled: togglingSync }"
                role="switch"
                @click="toggleAutoSync"
              ></div>
            </span>
          </div>
        </div>
      </div>

      <!-- 名下内容列表 -->
      <div class="seclabel" style="margin:22px 0 12px"><LayoutList :size="14" />内容 · {{ total }}</div>

      <div v-if="loading && jobs.length === 0" class="card pad" style="text-align:center;color:var(--ink-500)">
        加载中…
      </div>
      <div v-else-if="jobs.length === 0" class="card pad" style="text-align:center;padding:30px 18px">
        <p class="muted">此集合暂无内容</p>
      </div>
      <div v-else class="list">
        <div v-for="j in jobs" :key="j.job_id" class="row" @click="openJob(j)">
          <span class="type-pill" :class="contentTypePill(j.content_type)">
            <component :is="contentTypeIcon(j.content_type)" :size="17" />
          </span>
          <div class="body">
            <div class="title">{{ j.title || j.job_id }}</div>
            <div class="meta">
              <StatusBadge :status="j.status" />
              <span>{{ contentTypeLabel(j.content_type) }}</span>
              <template v-if="j.source"><span class="sep">·</span><span>{{ j.source }}</span></template>
              <span class="sep">·</span>
              <span class="dim">{{ fmtDateTime(j.created_at) }}</span>
            </div>
          </div>
          <ChevronRight :size="16" class="dim" />
        </div>
      </div>
    </template>

    <DeleteCollectionDialog
      v-if="showDelete && collection"
      :collection="collection" :deleting="deleting"
      @close="showDelete = false" @confirm="onDelete"
    />
  </section>
</template>

<style scoped>
.spin { animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.switch.disabled { opacity: .5; pointer-events: none; }
.del-btn { color: var(--ink-500); }
.del-btn:hover { color: var(--bad); border-color: var(--bad-bd); background: var(--bad-bg); }
/* 订阅状态点(5 态) */
.substat { display: inline-flex; align-items: center; gap: 7px; }
.sub-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--ink-300); }
.sub-dot.active { background: var(--ok); }
.sub-dot.never { background: var(--warn); }
.sub-dot.error { background: var(--bad); }
.sub-dot.syncing { background: var(--info); animation: pulse 1.1s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
.sub-err { margin-top: 5px; font-size: 12px; color: var(--bad); word-break: break-all; }
/* 内容状态分布 + 集合级重试 */
.counts { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.counts .sep { color: var(--ink-300); }
.counts .bad { color: var(--bad); font-weight: 600; }
.retry-btn { margin-top: 8px; }
</style>
