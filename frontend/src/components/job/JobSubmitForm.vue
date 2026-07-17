<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useJobStore } from '../../stores/jobs'
import { useGlobalStore } from '../../stores/global'
import { ArrowDown, ArrowUp, Plus, Send, Trash2, Upload, X } from 'lucide-vue-next'
import {
  DOCUMENT_KIND_CATALOG,
  contentTypeForUpload,
  ensureSourceCatalog,
  uploadAccept,
} from '../../constants/sources'

// bare:不渲染外层卡片/标题(嵌入弹窗时用)。done:投递成功后通知外层(如关闭弹窗)。
defineProps<{ bare?: boolean }>()
const emit = defineEmits<{ done: [] }>()

const router = useRouter()
const jobStore = useJobStore()
const globalStore = useGlobalStore()

const url = ref('')
const sourceMode = ref<'single' | 'multipart_video'>('single')
const videoTitle = ref('')
const videoParts = ref([
  { url: '', title: '' },
])
const domain = ref('general')
// AI 智能笔记开关:auto=按文档体裁默认(article 关/其余开)、on=强制生成、off=强制不生成。
const smartNote = ref<'auto' | 'on' | 'off'>('auto')
const processingMode = ref<'full' | 'mechanical_only'>('full')
const documentKind = ref('')
const selectedTags = ref<string[]>([])
const file = ref<File | null>(null)
const submitting = ref(false)
const error = ref('')

const domains = computed(() => {
  const list = globalStore.profiles.map(p => p.domain)
  if (!list.includes('general')) list.unshift('general')
  return list
})
const selectedUploadType = computed(() => file.value ? contentTypeForUpload(file.value.name) : undefined)
const isMultipartVideo = computed(() => sourceMode.value === 'multipart_video')
const canChooseDocumentKind = computed(() => !isMultipartVideo.value && (!file.value || selectedUploadType.value === 'document'))
const canSubmit = computed(() => {
  if (isMultipartVideo.value) {
    return videoParts.value.length > 0 && videoParts.value.every(part => part.url.trim())
  }
  return Boolean(url.value.trim() || file.value)
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

function setSourceMode(mode: 'single' | 'multipart_video') {
  sourceMode.value = mode
  error.value = ''
  if (mode === 'multipart_video') {
    file.value = null
    url.value = ''
    documentKind.value = ''
  }
}

function addVideoPart() {
  if (videoParts.value.length >= 128) return
  videoParts.value.push({ url: '', title: '' })
}

function removeVideoPart(index: number) {
  if (videoParts.value.length <= 1) return
  videoParts.value.splice(index, 1)
}

function moveVideoPart(index: number, offset: -1 | 1) {
  const target = index + offset
  if (target < 0 || target >= videoParts.value.length) return
  const [part] = videoParts.value.splice(index, 1)
  videoParts.value.splice(target, 0, part)
}

async function submit() {
  if (!canSubmit.value) return
  error.value = ''
  submitting.value = true
  try {
    let jobId: string
    if (isMultipartVideo.value) {
      const res = await jobStore.createJob({
        content_type: 'video',
        title: videoTitle.value.trim() || undefined,
        parts: videoParts.value.map(part => ({
          url: part.url.trim(),
          ...(part.title.trim() ? { title: part.title.trim() } : {}),
        })),
        domain: domain.value,
        style_tags: selectedTags.value,
        ...(smartNote.value === 'auto' ? {} : { smart_note: smartNote.value === 'on' }),
        ...(processingMode.value === 'mechanical_only' ? { mechanical_only: true } : {}),
      })
      jobId = res.job_id
    } else if (file.value) {
      const res = await jobStore.uploadJob(
        file.value,
        domain.value,
        selectedTags.value,
        documentKind.value || undefined,
        processingMode.value === 'mechanical_only',
      )
      jobId = res.job_id
    } else {
      const res = await jobStore.createJob({
        url: url.value.trim(),
        domain: domain.value,
        style_tags: selectedTags.value,
        ...(documentKind.value ? { document_kind: documentKind.value } : {}),
        // auto 时省略字段(后端按内容类型定默认);on/off 显式传布尔。
        ...(smartNote.value === 'auto' ? {} : { smart_note: smartNote.value === 'on' }),
        ...(processingMode.value === 'mechanical_only' ? { mechanical_only: true } : {}),
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
      <div class="source-tabs" role="tablist" aria-label="投递类型">
        <button type="button" :class="{ active: !isMultipartVideo }" data-test="single-source-mode" @click="setSourceMode('single')">单链接 / 文件</button>
        <button type="button" :class="{ active: isMultipartVideo }" data-test="multipart-video-mode" @click="setSourceMode('multipart_video')">视频 (单/多 Part)</button>
      </div>

      <div v-if="!isMultipartVideo" class="flex gap-2">
        <input
          v-model="url"
          type="text"
          placeholder="粘贴文档 / 文章 / 音频 URL"
          :disabled="!!file"
          class="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none disabled:bg-gray-50 disabled:text-gray-400"
        />
      </div>

      <div v-else class="multipart-editor" data-test="multipart-editor">
        <input
          v-model="videoTitle"
          type="text"
          placeholder="整场视频标题 (可选)"
          class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none"
          data-test="multipart-title"
        />
        <div class="part-list">
          <div v-for="(part, index) in videoParts" :key="index" class="part-row" :data-test="`part-row-${index + 1}`">
            <span class="part-index">P{{ String(index + 1).padStart(2, '0') }}</span>
            <input v-model="part.url" type="text" required placeholder="视频 URL / BV号" :data-test="`part-url-${index + 1}`" />
            <input v-model="part.title" type="text" placeholder="小标题 (可选)" :data-test="`part-title-${index + 1}`" />
            <div class="part-actions">
              <button type="button" title="上移" :disabled="index === 0" @click="moveVideoPart(index, -1)"><ArrowUp :size="14" /></button>
              <button type="button" title="下移" :disabled="index === videoParts.length - 1" @click="moveVideoPart(index, 1)"><ArrowDown :size="14" /></button>
              <button type="button" title="删除" :disabled="videoParts.length === 1" @click="removeVideoPart(index)"><Trash2 :size="14" /></button>
            </div>
          </div>
        </div>
        <button type="button" class="add-part" data-test="add-part" :disabled="videoParts.length >= 128" @click="addVideoPart"><Plus :size="14" />添加 Part</button>
        <p class="multipart-hint">按 P01 → PN 顺序处理；各 Part 独立转写，全部完成后只生成一套笔记。</p>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <select v-model="domain" class="px-2 py-1.5 border border-gray-300 rounded-lg text-sm bg-white">
          <option v-for="d in domains" :key="d" :value="d">{{ d }}</option>
        </select>

        <select
          v-if="canChooseDocumentKind"
          v-model="documentKind"
          class="px-2 py-1.5 border border-gray-300 rounded-lg text-sm bg-white"
          title="可选。留空时由来源给出可证明的默认体裁，无法证明则标记为未分类文档"
          data-test="document-kind"
        >
          <option value="">文档类别:自动</option>
          <option v-for="item in DOCUMENT_KIND_CATALOG" :key="item.kind" :value="item.kind">
            {{ item.label }}
          </option>
        </select>

        <select v-model="processingMode" class="px-2 py-1.5 border border-gray-300 rounded-lg text-sm bg-white" title="纯机械模式不会调度任何 AI 步骤,可在任务详情中继续 AI">
          <option value="full">处理模式:完整</option>
          <option value="mechanical_only">处理模式:纯机械</option>
        </select>

        <!-- AI 智能笔记开关:article 子类默认走轻链路(关),可强制开/关 -->
        <select v-model="smartNote" :disabled="processingMode === 'mechanical_only'" class="px-2 py-1.5 border border-gray-300 rounded-lg text-sm bg-white disabled:bg-gray-100" title="是否生成 AI 智能笔记(概念提取与摘要始终生成)">
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
        <label v-if="!isMultipartVideo" class="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded-lg cursor-pointer hover:bg-gray-50 transition-colors">
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
          :disabled="submitting || !canSubmit"
          data-test="submit-job"
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

<style scoped>
.source-tabs { display: inline-flex; padding: 3px; gap: 2px; border: 1px solid var(--line, #e5e7eb); border-radius: 9px; background: var(--raised, #f8fafc); }
.source-tabs button { padding: 5px 10px; border-radius: 6px; color: var(--ink-500, #64748b); font-size: 12px; font-weight: 600; }
.source-tabs button.active { color: var(--brand-700, #1d4ed8); background: white; box-shadow: 0 1px 2px rgb(15 23 42 / 8%); }
.multipart-editor { display: flex; flex-direction: column; gap: 9px; padding: 11px; border: 1px solid var(--line, #e5e7eb); border-radius: 10px; background: var(--raised, #f8fafc); }
.part-list { display: flex; flex-direction: column; gap: 7px; }
.part-row { display: grid; grid-template-columns: 42px minmax(190px, 1.5fr) minmax(120px, .8fr) auto; gap: 7px; align-items: center; }
.part-index { font: 700 12px/1 ui-monospace, monospace; color: var(--brand-700, #1d4ed8); }
.part-row input { min-width: 0; padding: 7px 9px; border: 1px solid #d1d5db; border-radius: 7px; background: white; font-size: 12px; outline: none; }
.part-row input:focus { border-color: #3b82f6; box-shadow: 0 0 0 2px rgb(59 130 246 / 15%); }
.part-actions { display: flex; gap: 3px; }
.part-actions button { padding: 5px; color: var(--ink-500, #64748b); border-radius: 5px; }
.part-actions button:hover:not(:disabled) { background: white; color: var(--ink-800, #1e293b); }
.part-actions button:disabled { opacity: .25; cursor: not-allowed; }
.add-part { align-self: flex-start; display: inline-flex; align-items: center; gap: 5px; color: var(--brand-700, #1d4ed8); font-size: 12px; font-weight: 600; }
.multipart-hint { margin: 0; color: var(--ink-500, #64748b); font-size: 11px; }
@media (max-width: 720px) {
  .part-row { grid-template-columns: 38px minmax(0, 1fr) auto; }
  .part-row > input:nth-of-type(2) { grid-column: 2 / 4; }
}
</style>
