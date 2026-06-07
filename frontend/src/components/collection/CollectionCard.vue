<script setup lang="ts">
import { useRouter } from 'vue-router'
import type { Collection } from '../../types'
import { Library, Pencil, Trash2 } from 'lucide-vue-next'

const props = defineProps<{ collection: Collection }>()
const emit = defineEmits<{ edit: [Collection]; remove: [Collection] }>()
const router = useRouter()

function open() {
  router.push(`/collections/${props.collection.id}`)
}
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl p-4 hover:shadow-sm transition-shadow">
    <div class="flex items-start gap-3">
      <div class="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center flex-shrink-0 mt-0.5">
        <Library :size="16" class="text-gray-500" />
      </div>
      <div class="flex-1 min-w-0 cursor-pointer" @click="open">
        <div class="flex items-center gap-2 mb-1">
          <h4 class="text-sm font-medium truncate">{{ collection.name }}</h4>
          <span class="text-xs text-gray-400">{{ collection.job_count }} 篇</span>
        </div>
        <div class="flex items-center gap-2 text-xs text-gray-500 flex-wrap">
          <span v-if="collection.domain && collection.domain !== 'general'">{{ collection.domain }}</span>
          <span
            v-for="t in collection.tags"
            :key="t"
            class="px-1.5 py-0.5 bg-gray-100 rounded text-gray-600"
          >{{ t }}</span>
        </div>
        <p v-if="collection.description" class="text-xs text-gray-400 mt-1 truncate">
          {{ collection.description }}
        </p>
      </div>
      <div class="flex items-center gap-1 flex-shrink-0">
        <button
          @click.stop="emit('edit', collection)"
          class="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
          title="编辑"
        >
          <Pencil :size="14" />
        </button>
        <button
          @click.stop="emit('remove', collection)"
          class="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
          title="删除"
        >
          <Trash2 :size="14" />
        </button>
      </div>
    </div>
  </div>
</template>
