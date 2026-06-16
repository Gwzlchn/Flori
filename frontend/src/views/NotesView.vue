<script setup lang="ts">
import { ref, computed, onMounted, watch, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import MarkdownViewer from '../components/notes/MarkdownViewer.vue'
import ChapterNav from '../components/notes/ChapterNav.vue'
import { ArrowLeft, BookOpen, FileText, RefreshCw, Star, ChevronDown } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const api = useApi()
const showToast = inject<(m: string, t?: string) => void>('showToast', () => {})

const jobId = computed(() => route.params.id as string)
const isMechanical = computed(() => route.params.type === 'mechanical')

const content = ref('')
const headings = ref<{ id: string; text: string; level: number }[]>([])
const loading = ref(true)
const error = ref('')
const title = ref('')

// 版本(按 provider)+ 重跑
type Version = { provider: string; overall: number | null }
const versions = ref<Version[]>([])
const activeProvider = ref<string | null>(null)   // null = 默认(最近一次)
type Provider = { name: string; type: string; available: boolean; label: string }
const providers = ref<Provider[]>([])
const showRerun = ref(false)
const rerunning = ref(false)

async function loadContent() {
  loading.value = true; error.value = ''
  try {
    const base = isMechanical.value
      ? `/api/jobs/${jobId.value}/notes/mechanical`
      : `/api/jobs/${jobId.value}/notes/smart`
    const endpoint = (!isMechanical.value && activeProvider.value)
      ? `${base}?provider=${encodeURIComponent(activeProvider.value)}`
      : base
    content.value = await api.getText(endpoint)
  } catch (e: any) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function loadVersions() {
  if (isMechanical.value) return
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

async function selectVersion(provider: string | null) {
  activeProvider.value = provider
  await loadContent()
}

async function rerunWith(provider: Provider) {
  if (!provider.available || rerunning.value) return
  if (!confirm(`用「${provider.name}」重新生成智能笔记 + 评分?\n旧版本会保留,生成后可切换对比。`)) return
  showRerun.value = false
  rerunning.value = true
  try {
    await api.post(`/api/jobs/${jobId.value}/rerun-smart`, { provider: provider.name })
    showToast(`已用 ${provider.name} 开始重跑,完成后会出现新版本`, 'success')
    pollForVersion(provider.name)
  } catch (e: any) {
    showToast(e.message || '重跑失败', 'error')
    rerunning.value = false
  }
}

// 轮询直到该 provider 版本出现(最多约 12 分钟)
function pollForVersion(provider: string) {
  let n = 0
  const timer = setInterval(async () => {
    n++
    await loadVersions()
    const got = versions.value.find(v => v.provider === provider)
    if (got || n > 48) {
      clearInterval(timer)
      rerunning.value = false
      if (got) {
        showToast(`${provider} 版本已生成`, 'success')
        await selectVersion(provider)
      }
    }
  }, 15000)
}

async function reload() {
  try {
    const detail = await api.get<{ title: string }>(`/api/jobs/${jobId.value}`)
    title.value = detail.title || jobId.value
  } catch { title.value = jobId.value }
  await Promise.all([loadContent(), loadVersions(), loadProviders()])
}

onMounted(reload)

// 智能笔记 ↔ 机械稿、以及不同 job 共用同一个 NotesView 实例,切换时组件不重挂、
// onMounted 不再触发 → 内容不会变。监听路由变化重新取(切 variant 时清掉 provider 选择)。
watch(() => [route.params.id, route.params.type], () => {
  activeProvider.value = null
  reload()
})

function onHeadings(h: { id: string; text: string; level: number }[]) {
  headings.value = h
}
const showChapters = ref(false)
</script>

<template>
  <div>
    <!-- Top bar -->
    <div class="flex items-center gap-3 mb-4">
      <button @click="router.back()" class="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">
        <ArrowLeft :size="16" />
      </button>
      <h2 class="text-lg font-bold truncate flex-1">{{ title }}</h2>
      <div class="flex items-center gap-1">
        <router-link
          :to="`/jobs/${jobId}/notes/smart`"
          class="px-2 py-1 text-xs rounded-md transition-colors"
          :class="!isMechanical ? 'bg-blue-100 text-blue-700 font-medium' : 'text-gray-500 hover:bg-gray-100'"
        >
          <BookOpen :size="12" class="inline mr-0.5" />
          智能版
        </router-link>
        <router-link
          :to="`/jobs/${jobId}/notes/mechanical`"
          class="px-2 py-1 text-xs rounded-md transition-colors"
          :class="isMechanical ? 'bg-blue-100 text-blue-700 font-medium' : 'text-gray-500 hover:bg-gray-100'"
        >
          <FileText :size="12" class="inline mr-0.5" />
          机械版
        </router-link>
      </div>
    </div>

    <!-- 版本条(仅智能版) -->
    <div v-if="!isMechanical" class="flex items-center flex-wrap gap-2 mb-3">
      <span class="text-xs text-gray-400">版本:</span>
      <button
        v-if="versions.length === 0"
        class="px-2 py-1 text-xs rounded-md bg-blue-100 text-blue-700 font-medium cursor-default"
      >默认</button>
      <button
        v-for="v in versions"
        :key="v.provider"
        @click="selectVersion(v.provider)"
        class="px-2 py-1 text-xs rounded-md transition-colors flex items-center gap-1"
        :class="(activeProvider ?? versions[0]?.provider) === v.provider ? 'bg-blue-100 text-blue-700 font-medium' : 'text-gray-500 hover:bg-gray-100 border border-gray-200'"
      >
        {{ v.provider }}
        <span v-if="v.overall != null" class="flex items-center gap-0.5 text-amber-600">
          <Star :size="11" />{{ v.overall }}
        </span>
      </button>

      <!-- 重跑(选 provider) -->
      <div class="relative ml-auto">
        <button
          @click="showRerun = !showRerun"
          :disabled="rerunning"
          class="px-2.5 py-1 text-xs rounded-md border border-gray-200 text-gray-600 hover:bg-gray-50 flex items-center gap-1 disabled:opacity-50"
        >
          <RefreshCw :size="12" :class="rerunning ? 'animate-spin' : ''" />
          {{ rerunning ? '生成中…' : '换 provider 重跑' }}
          <ChevronDown :size="12" />
        </button>
        <div v-if="showRerun" class="absolute right-0 mt-1 w-44 bg-white border border-gray-200 rounded-lg shadow-lg z-20 py-1">
          <button
            v-for="p in providers"
            :key="p.name"
            @click="rerunWith(p)"
            :disabled="!p.available"
            class="w-full text-left px-3 py-2 text-xs flex items-center justify-between transition-colors"
            :class="p.available ? 'hover:bg-gray-50 text-gray-700' : 'text-gray-300 cursor-not-allowed'"
          >
            <span>{{ p.name }} <span class="text-gray-400">({{ p.label }})</span></span>
            <span v-if="!p.available" class="text-[10px] text-gray-300">未配置 key</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Mobile chapter dropdown -->
    <div v-if="headings.length > 0" class="lg:hidden mb-3">
      <button
        @click="showChapters = !showChapters"
        class="w-full px-3 py-2 text-sm text-left bg-white border border-gray-200 rounded-lg flex items-center justify-between"
      >
        <span class="text-gray-600">章节导航 ({{ headings.length }})</span>
        <span class="text-gray-400 text-xs">{{ showChapters ? '收起' : '展开' }}</span>
      </button>
      <div v-if="showChapters" class="mt-1 bg-white border border-gray-200 rounded-lg p-3 max-h-64 overflow-y-auto">
        <ChapterNav :headings="headings" />
      </div>
    </div>

    <div v-if="loading" class="text-sm text-gray-400 py-8 text-center">加载中...</div>
    <div v-else-if="error" class="text-sm text-red-600 py-8 text-center">{{ error }}</div>

    <div v-else class="flex gap-6">
      <div class="flex-1 min-w-0 bg-white border border-gray-200 rounded-xl p-4 md:p-6">
        <MarkdownViewer :content="content" :job-id="jobId" @headings="onHeadings" />
      </div>
      <aside class="hidden lg:block w-56 flex-shrink-0">
        <div class="sticky top-6">
          <ChapterNav :headings="headings" />
        </div>
      </aside>
    </div>
  </div>
</template>
