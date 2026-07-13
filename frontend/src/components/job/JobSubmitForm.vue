<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useJobStore } from '../../stores/jobs'
import { useGlobalStore } from '../../stores/global'
import { Send, Upload, X } from 'lucide-vue-next'
import { ensureSourceCatalog, uploadAccept } from '../../constants/sources'

// bare:不渲染外层卡片/标题(嵌入弹窗时用)。done:投递成功后通知外层(如关闭弹窗)。
defineProps<{ bare?: boolean }>()
const emit = defineEmits<{ done: [] }>()

const router = useRouter()
const jobStore = useJobStore()
const globalStore = useGlobalStore()

const url = ref('')
const domain = ref('general')
// AI 智能笔记开关:auto=按内容类型默认(文章关/其余开)、on=强制生成、off=强制不生成。
const smartNote = ref<'auto' | 'on' | 'off'>('auto')
const selectedTags = ref<string[]>([])
const file = ref<File | null>(null)
const submitting = ref(false)
const error = ref('')

const domains = computed(() => {
  const list = globalStore.profiles.map(p => p.domain)
  if (!list.includes('general')) list.unshift('general')
  return list
})

onMounted(() => {
  globalStore.fetchProfiles()
  globalStore.fetchStyleTags()
  void ensureSourceCatalog()
})

function toggleTag(tag: string) {
  const idx = selectedTags.value.indexOf(tag)
  if (idx >= 0) selectedTags.value.splice(idx, 1)
  else selectedTags.value.push(tag)
}

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  file.value = input.files?.[0] ?? null
  if (file.value) url.value = ''
}

function clearFile() {
  file.value = null
}

async function submit() {
  if (!url.value.trim() && !file.value) return
  error.value = ''
  submitting.value = true
  try {
    let jobId: string
    if (file.value) {
      const res = await jobStore.uploadJob(file.value, domain.value, selectedTags.value)
      jobId = res.job_id
    } else {
      const res = await jobStore.createJob({
        url: url.value.trim(),
        domain: domain.value,
        style_tags: selectedTags.value,
        // auto 时省略字段(后端按内容类型定默认);on/off 显式传布尔。
        ...(smartNote.value === 'auto' ? {} : { smart_note: smartNote.value === 'on' }),
      })
      jobId = res.job_id
    }
    emit('done')
    router.push(`/content/${jobId}`)
  } catch (e: any) {
    error.value = e.message || '投递失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div data-submit-form :class="bare ? '' : 'bg-white rounded-xl border border-gray-200 p-4'">
    <h3 v-if="!bare" class="text-sm font-semibold text-gray-700 mb-3">快速投递</h3>
    <form @submit.prevent="submit" class="space-y-3">
      <div class="flex gap-2">
        <input
          v-model="url"
          type="text"
          placeholder="粘贴 URL (BV号 / arXiv / 文章 / 音频链接)"
          :disabled="!!file"
          class="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none disabled:bg-gray-50 disabled:text-gray-400"
        />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <select v-model="domain" class="px-2 py-1.5 border border-gray-300 rounded-lg text-sm bg-white">
          <option v-for="d in domains" :key="d" :value="d">{{ d }}</option>
        </select>

        <!-- AI 智能笔记开关:文章默认走轻链路(关),可强制开/关 -->
        <select v-model="smartNote" class="px-2 py-1.5 border border-gray-300 rounded-lg text-sm bg-white" title="是否生成 AI 智能笔记(概念提取与摘要始终生成)">
          <option value="auto">智能笔记:自动</option>
          <option value="on">智能笔记:开</option>
          <option value="off">智能笔记:关</option>
        </select>

        <button
          v-for="tag in globalStore.styleTags"
          :key="tag"
          type="button"
          @click="toggleTag(tag)"
          class="px-2 py-1 rounded-full text-xs border transition-colors"
          :class="selectedTags.includes(tag) ? 'bg-blue-100 border-blue-300 text-blue-700' : 'border-gray-300 text-gray-600 hover:bg-gray-50'"
        >
          {{ tag }}
        </button>
      </div>

      <div class="flex items-center gap-2">
        <label class="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded-lg cursor-pointer hover:bg-gray-50 transition-colors">
          <Upload :size="14" />
          <span>上传文件</span>
          <input type="file" :accept="uploadAccept()" class="hidden" @change="onFileChange" />
        </label>
        <span v-if="file" class="flex items-center gap-1 text-sm text-gray-600">
          {{ file.name }}
          <button type="button" @click="clearFile" class="text-gray-400 hover:text-gray-600"><X :size="14" /></button>
        </span>

        <div class="flex-1" />

        <button
          type="submit"
          :disabled="submitting || (!url.trim() && !file)"
          class="flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <Send :size="14" />
          <span>{{ submitting ? '投递中...' : '投递' }}</span>
        </button>
      </div>

      <p v-if="error" class="text-sm text-red-600">{{ error }}</p>
    </form>
  </div>
</template>
