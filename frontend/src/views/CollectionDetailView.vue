<script setup lang="ts">
import { onMounted, ref, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCollectionStore } from '../stores/collections'
import { useJobStore } from '../stores/jobs'
import { useApi } from '../composables/useApi'
import JobCard from '../components/job/JobCard.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { fmtDateTime } from '../utils/datetime'
import type { Collection, JobSummary } from '../types'
import { ArrowLeft, Library, RefreshCw, Send, Rss, ExternalLink } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const store = useCollectionStore()
const jobStore = useJobStore()
const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const id = String(route.params.id)
const collection = ref<Collection | null>(null)
const jobs = ref<JobSummary[]>([])
const total = ref(0)
const loading = ref(false)
const notFound = ref(false)

// 投递到此集合：domain 默认继承 collection.domain，collection_id 直接绑定。
const url = ref('')
const submitting = ref(false)
const submitError = ref('')

// 订阅源操作状态
const syncing = ref(false)
const togglingSync = ref(false)

// B站 UP 主空间页（点名字/链接直达）
const upHomeUrl = (mid: string) => `https://space.bilibili.com/${mid}`

async function load() {
  loading.value = true
  notFound.value = false
  try {
    collection.value = await store.get(id)
    const res = await store.fetchJobs(id)
    jobs.value = res.items
    total.value = res.total
  } catch (e: any) {
    if (String(e?.message ?? '').includes('404')) notFound.value = true
  } finally {
    loading.value = false
  }
}

async function submit() {
  if (!url.value.trim() || !collection.value) return
  submitError.value = ''
  submitting.value = true
  try {
    // 走 jobs store；collection_id 绑定集合，domain 继承集合。
    await jobStore.createJob({
      url: url.value.trim(),
      domain: collection.value.domain,
      collection_id: collection.value.id,
    })
    url.value = ''
    await load()
  } catch (e: any) {
    submitError.value = e.message || '投递失败'
  } finally {
    submitting.value = false
  }
}

// 立即同步：拉取 UP 全部视频，新视频自动建 job 入本集合。
async function syncNow() {
  const c = collection.value
  if (!c?.subscription || syncing.value) return
  syncing.value = true
  try {
    const r = await api.post<{ new: number; total: number }>(`/api/collections/${c.id}/sync`)
    showToast(`同步完成：新增 ${r.new} 个（共 ${r.total}）`, 'success')
    await load()
  } catch (e: any) {
    showToast(e.message || '同步失败', 'error')
  } finally {
    syncing.value = false
  }
}

// 自动同步开关：关掉后定时任务不再追更该来源（仍可手动同步）。订阅是集合属性，打集合端点。
async function toggleAutoSync() {
  const c = collection.value
  if (!c?.subscription || togglingSync.value) return
  togglingSync.value = true
  try {
    await api.put(`/api/collections/${c.id}`, { sync_enabled: !c.subscription.enabled })
    await load()
  } catch (e: any) {
    showToast(e.message || '操作失败', 'error')
  } finally {
    togglingSync.value = false
  }
}

onMounted(load)  // 进入集合详情页即加载集合信息 + 名下 job 列表
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center gap-2">
      <button @click="router.push('/collections')" class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors">
        <ArrowLeft :size="18" />
      </button>
      <h2 class="text-xl font-bold flex items-center gap-2 min-w-0">
        <component :is="collection?.subscription ? Rss : Library" :size="22" class="flex-shrink-0" :class="collection?.subscription ? 'text-blue-500' : ''" />
        <span class="truncate">{{ collection?.name ?? '集合详情' }}</span>
      </h2>
      <button @click="load()" class="ml-auto p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors">
        <RefreshCw :size="16" :class="loading ? 'animate-spin' : ''" />
      </button>
    </div>

    <div v-if="notFound">
      <EmptyState message="集合不存在或已删除" />
    </div>

    <template v-else-if="collection">
      <!-- 集合元信息 -->
      <div class="bg-white border border-gray-200 rounded-xl p-4">
        <div class="flex items-center gap-2 text-xs text-gray-500 flex-wrap mb-1">
          <span class="font-mono text-gray-400">{{ collection.id }}</span>
          <span v-if="collection.domain && collection.domain !== 'general'">{{ collection.domain }}</span>
          <span
            v-for="t in collection.tags"
            :key="t"
            class="px-1.5 py-0.5 bg-gray-100 rounded text-gray-600"
          >{{ t }}</span>
          <span>{{ collection.job_count }} 篇</span>
        </div>
        <p v-if="collection.description" class="text-sm text-gray-600">{{ collection.description }}</p>
      </div>

      <!-- 订阅源（仅订阅集合）：UP 主信息 + 自动追更开关 + 立即同步 -->
      <div v-if="collection.subscription" class="bg-white border border-gray-200 rounded-xl p-4">
        <div class="flex items-center gap-2 mb-3">
          <Rss :size="16" class="text-blue-500" />
          <h3 class="text-sm font-semibold text-gray-700">订阅源</h3>
          <span class="text-[10px] px-1.5 py-0.5 rounded" :class="collection.subscription.enabled ? 'bg-green-50 text-green-600' : 'bg-gray-100 text-gray-400'">
            {{ collection.subscription.enabled ? '自动追更中' : '已暂停自动追更' }}
          </span>
        </div>
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-y-2 gap-x-4 text-xs">
          <div>
            <div class="text-gray-400 mb-0.5">来源</div>
            <div class="text-gray-700">B站 UP 主</div>
          </div>
          <div>
            <div class="text-gray-400 mb-0.5">UP 主页</div>
            <a :href="upHomeUrl(collection.subscription.source_id)" target="_blank" rel="noopener"
               class="text-blue-600 hover:underline inline-flex items-center gap-1">
              {{ collection.subscription.source_id }}<ExternalLink :size="11" />
            </a>
          </div>
          <div>
            <div class="text-gray-400 mb-0.5">已入库</div>
            <div class="text-gray-700">{{ collection.job_count }} 个视频</div>
          </div>
          <div>
            <div class="text-gray-400 mb-0.5">上次同步</div>
            <div class="text-gray-700">{{ collection.subscription.last_synced_at ? fmtDateTime(collection.subscription.last_synced_at) : '从未' }}</div>
          </div>
        </div>
        <div class="flex items-center gap-3 mt-4 pt-3 border-t border-gray-100">
          <button @click="syncNow" :disabled="syncing"
                  class="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors">
            <RefreshCw :size="13" :class="syncing ? 'animate-spin' : ''" />
            {{ syncing ? '同步中…' : '立即同步' }}
          </button>
          <button @click="toggleAutoSync" :disabled="togglingSync"
                  class="flex items-center gap-2 text-xs text-gray-600 hover:text-gray-800 disabled:opacity-50">
            <span class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors"
                  :class="collection.subscription.enabled ? 'bg-blue-500' : 'bg-gray-300'">
              <span class="inline-block h-3 w-3 transform rounded-full bg-white transition-transform"
                    :class="collection.subscription.enabled ? 'translate-x-3.5' : 'translate-x-0.5'" />
            </span>
            自动同步
          </button>
        </div>
      </div>

      <!-- 投递到此集合 -->
      <div class="bg-white border border-gray-200 rounded-xl p-4">
        <h3 class="text-sm font-semibold text-gray-700 mb-3">投递到此集合</h3>
        <form @submit.prevent="submit" class="flex gap-2">
          <input
            v-model="url"
            type="text"
            placeholder="粘贴 URL (BV号 / arXiv / 链接)"
            class="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none"
          />
          <button
            type="submit"
            :disabled="submitting || !url.trim()"
            class="flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <Send :size="14" />
            <span>{{ submitting ? '投递中...' : '投递' }}</span>
          </button>
        </form>
        <p v-if="submitError" class="text-sm text-red-600 mt-2">{{ submitError }}</p>
      </div>

      <!-- 集合内 job 列表 -->
      <div v-if="loading && jobs.length === 0" class="text-sm text-gray-400 py-8 text-center">加载中...</div>
      <div v-else-if="jobs.length === 0">
        <EmptyState message="此集合暂无任务" />
      </div>
      <div v-else class="space-y-3">
        <JobCard v-for="j in jobs" :key="j.job_id" :job="j" />
      </div>
    </template>
  </div>
</template>
