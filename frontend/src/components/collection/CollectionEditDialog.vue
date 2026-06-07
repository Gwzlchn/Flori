<script setup lang="ts">
import { reactive, ref } from 'vue'
import type { Collection } from '../../types'

// 新建/编辑集合对话框。传入 collection 即编辑模式（domain 不可改）。
const props = defineProps<{ collection?: Collection | null }>()
const emit = defineEmits<{
  submit: [payload: { name: string; domain: string; description: string; tags: string[] }]
  cancel: []
}>()

const isEdit = ref(!!props.collection)
const form = reactive({
  name: props.collection?.name ?? '',
  domain: props.collection?.domain ?? 'general',
  description: props.collection?.description ?? '',
  tagsText: (props.collection?.tags ?? []).join(', '),
})

function onSubmit() {
  const name = form.name.trim()
  if (!name) return
  const tags = form.tagsText
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
  emit('submit', {
    name,
    domain: form.domain.trim() || 'general',
    description: form.description.trim(),
    tags,
  })
}
</script>

<template>
  <div class="fixed inset-0 z-50 bg-gray-900/50 flex items-center justify-center p-4" @click.self="emit('cancel')">
    <div class="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
      <h3 class="text-lg font-bold mb-4">{{ isEdit ? '编辑集合' : '新建集合' }}</h3>
      <div class="space-y-3">
        <div>
          <label class="block text-xs text-gray-500 mb-1">名称</label>
          <input
            v-model="form.name"
            type="text"
            placeholder="集合名称"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">领域 (domain)</label>
          <!-- domain 是集合的归属维度，创建后不可改（job 默认继承）。 -->
          <input
            v-model="form.domain"
            :disabled="isEdit"
            type="text"
            placeholder="例如 deep-learning"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:bg-gray-100 disabled:text-gray-400"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">描述</label>
          <textarea
            v-model="form.description"
            rows="2"
            placeholder="可选"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">标签（逗号分隔）</label>
          <input
            v-model="form.tagsText"
            type="text"
            placeholder="cv, nlp"
            class="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>
      </div>
      <div class="flex gap-3 justify-end mt-6">
        <button @click="emit('cancel')" class="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg transition-colors">
          取消
        </button>
        <button
          @click="onSubmit"
          :disabled="!form.name.trim()"
          class="px-4 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ isEdit ? '保存' : '创建' }}
        </button>
      </div>
    </div>
  </div>
</template>
