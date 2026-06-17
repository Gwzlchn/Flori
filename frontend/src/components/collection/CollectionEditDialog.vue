<script setup lang="ts">
import { reactive, ref, computed } from 'vue'
import type { Collection } from '../../types'
import { Library, Rss } from 'lucide-vue-next'

// 新建/编辑集合对话框。传入 collection 即编辑模式（domain 不可改）。
// 新建时可选类型：手动集合 / 订阅集合(B站 UP 主)。订阅是集合的属性，无独立订阅页。
// submitting/error 由父组件驱动：失败时对话框不关闭，内联展示错误。
const props = defineProps<{ collection?: Collection | null; submitting?: boolean; error?: string }>()
const emit = defineEmits<{
  submit: [payload: {
    name: string; domain: string; description: string; tags: string[]
    source_type?: string; source_id?: string
  }]
  cancel: []
}>()

const isEdit = ref(!!props.collection)
const type = ref<'manual' | 'subscription'>('manual')   // 仅新建时可选
const form = reactive({
  name: props.collection?.name ?? '',
  domain: props.collection?.domain ?? '',
  description: props.collection?.description ?? '',
  tagsText: (props.collection?.tags ?? []).join(', '),
  mid: '',   // 订阅：UP 主 mid
})

const isSub = computed(() => !isEdit.value && type.value === 'subscription')

// 订阅集合：必须填 mid + 真实 domain（不能 general/空）。
const canSubmit = computed(() => {
  if (isSub.value) {
    const d = form.domain.trim()
    return !!form.mid.trim().replace(/\D/g, '') && !!d && d !== 'general'
  }
  return !!form.name.trim()
})

function onSubmit() {
  if (!canSubmit.value) return
  const tags = form.tagsText.split(',').map((t) => t.trim()).filter(Boolean)
  const domain = form.domain.trim() || 'general'
  if (isSub.value) {
    const mid = form.mid.trim().replace(/\D/g, '')
    emit('submit', {
      name: form.name.trim() || `UP-${mid}`,
      domain, description: form.description.trim(), tags,
      source_type: 'bilibili_up', source_id: mid,
    })
  } else {
    emit('submit', { name: form.name.trim(), domain, description: form.description.trim(), tags })
  }
}
</script>

<template>
  <div class="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" @click.self="emit('cancel')">
    <div class="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
      <h3 class="text-lg font-bold mb-4">{{ isEdit ? '编辑集合' : '新建集合' }}</h3>

      <!-- 类型切换（仅新建） -->
      <div v-if="!isEdit" class="flex gap-2 mb-4">
        <button
          @click="type = 'manual'"
          class="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-sm rounded-lg border transition-colors"
          :class="type === 'manual' ? 'border-blue-300 bg-blue-50 text-blue-700' : 'border-gray-200 text-gray-600 hover:bg-gray-50'"
        >
          <Library :size="15" /> 手动集合
        </button>
        <button
          @click="type = 'subscription'"
          class="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-sm rounded-lg border transition-colors"
          :class="type === 'subscription' ? 'border-blue-300 bg-blue-50 text-blue-700' : 'border-gray-200 text-gray-600 hover:bg-gray-50'"
        >
          <Rss :size="15" /> 订阅 B站 UP 主
        </button>
      </div>

      <div class="space-y-3">
        <div v-if="isSub">
          <label class="block text-xs text-gray-500 mb-1">UP 主 mid（空间页 URL 里的数字）</label>
          <input
            v-model="form.mid" type="text" placeholder="例如 247209804"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">名称 {{ isSub ? '（可选，默认 UP-mid）' : '' }}</label>
          <input
            v-model="form.name" type="text" placeholder="集合名称"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">
            领域 (domain) {{ isSub ? '· 必填，不能为 general' : '' }}
          </label>
          <!-- domain 是集合的归属维度，创建后不可改（job 默认继承）。订阅集合必须真实 domain。 -->
          <input
            v-model="form.domain" :disabled="isEdit" type="text" placeholder="例如 finance / deep-learning"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:bg-gray-100 disabled:text-gray-400"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">描述</label>
          <textarea
            v-model="form.description" rows="2" placeholder="可选"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">标签（逗号分隔）</label>
          <input
            v-model="form.tagsText" type="text" placeholder="cv, nlp"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
        <p v-if="isSub" class="text-xs text-gray-400">订阅后自动拉取该 UP 全部视频走流水线，并定期追更。</p>
      </div>

      <p v-if="error" class="text-sm text-red-600 mt-3">{{ error }}</p>
      <div class="flex gap-3 justify-end mt-6">
        <button @click="emit('cancel')" :disabled="submitting" class="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
          取消
        </button>
        <button
          @click="onSubmit"
          :disabled="!canSubmit || submitting"
          class="px-4 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ submitting ? '保存中...' : (isEdit ? '保存' : (isSub ? '订阅并同步' : '创建')) }}
        </button>
      </div>
    </div>
  </div>
</template>
