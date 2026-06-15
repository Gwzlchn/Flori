<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useCollectionStore } from '../stores/collections'
import { useJobStore } from '../stores/jobs'
import JobCard from '../components/job/JobCard.vue'
import EmptyState from '../components/common/EmptyState.vue'
import type { Collection, JobSummary } from '../types'
import { ArrowLeft, Library, RefreshCw, Send } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const store = useCollectionStore()
const jobStore = useJobStore()

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

onMounted(load)  // 进入集合详情页即加载集合信息 + 名下 job 列表
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center gap-2">
      <button @click="router.push('/collections')" class="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors">
        <ArrowLeft :size="18" />
      </button>
      <h2 class="text-xl font-bold flex items-center gap-2">
        <Library :size="22" />
        {{ collection?.name ?? '集合详情' }}
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
