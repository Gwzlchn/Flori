<script setup lang="ts">
import { ref, computed } from 'vue'
import type { Worker } from '../../types'
import StatusBadge from '../common/StatusBadge.vue'
import { useWorkerStore } from '../../stores/workers'
import { Edit3, Trash2, PauseCircle, PlayCircle, Tag } from 'lucide-vue-next'

const props = defineProps<{ worker: Worker }>()
const workerStore = useWorkerStore()

const editing = ref(false)
const noteInput = ref(props.worker.admin_note || '')
const editingTags = ref(false)
const tagsInput = ref(props.worker.tags.join(' '))

// 在线/离线一律以后端 status 为准,前端不再用时间戳自算(时区会算错,导致在线判离线)。
const statusLight = computed(() => {
  const map: Record<string, string> = {
    'online-idle': 'bg-green-500',
    'online-busy': 'bg-blue-500',
    draining: 'bg-orange-500',
    offline: 'bg-gray-400',
    stale: 'bg-gray-300',
  }
  return map[props.worker.status] || 'bg-gray-400'
})

const isOnline = computed(() => props.worker.status.startsWith('online'))
const isDraining = computed(() => props.worker.status === 'draining')
const isRemovable = computed(() =>
  props.worker.status === 'offline' || props.worker.status === 'stale'
)

// 后端时间戳是 UTC,可能没带时区后缀;按 UTC 解析,避免浏览器按本地时区算偏(曾差 8h)。
function parseUTC(s: string): number {
  return new Date(/[Zz]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z').getTime()
}

const uptimeText = computed(() => {
  if (!props.worker.started_at) return ''
  const sec = (Date.now() - parseUTC(props.worker.started_at)) / 1000
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
})

const heartbeatText = computed(() => {
  if (!props.worker.last_heartbeat) return '未知'
  const sec = Math.floor((Date.now() - parseUTC(props.worker.last_heartbeat)) / 1000)
  if (sec < 0) return '刚刚'
  if (sec < 60) return `${sec}s 前`
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`
  return `${Math.floor(sec / 3600)}h 前`
})

const gpuText = computed(() => {
  if (!props.worker.gpu_name) return ''
  const mem = props.worker.gpu_memory_mb
    ? ` ${Math.round(props.worker.gpu_memory_mb / 1024)}G`
    : ''
  return props.worker.gpu_name + mem
})

async function toggleDrain() {
  if (isDraining.value) {
    await workerStore.undrain(props.worker.id)
  } else {
    await workerStore.drain(props.worker.id)
  }
}

async function saveNote() {
  await workerStore.updateNote(props.worker.id, noteInput.value)
  editing.value = false
}

async function saveTags() {
  const tags = tagsInput.value.split(/[\s,]+/).filter(Boolean)
  await workerStore.updateTags(props.worker.id, tags)
  editingTags.value = false
}

async function remove() {
  // 离线/失联直接删；在线的需要 force 二次确认。
  const force = isOnline.value || isDraining.value
  const msg = force
    ? `${props.worker.id} 仍在线，强制移除？`
    : `确定移除 ${props.worker.id} 的记录？`
  if (confirm(msg)) {
    await workerStore.remove(props.worker.id, force)
  }
}
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl p-4">
    <div class="flex items-start gap-3">
      <span class="w-3 h-3 rounded-full mt-1.5 flex-shrink-0" :class="statusLight" />
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="font-mono text-sm font-medium">{{ worker.id }}</span>
          <StatusBadge :status="worker.status" />
          <span class="px-1.5 py-0.5 bg-gray-100 rounded text-xs text-gray-600 uppercase">{{ worker.type }}</span>
        </div>

        <!-- Tags -->
        <div v-if="!editingTags" class="flex items-center gap-1 flex-wrap mt-1">
          <span
            v-for="t in worker.tags"
            :key="t"
            class="px-1.5 py-0.5 bg-indigo-50 text-indigo-600 rounded text-xs"
          >{{ t }}</span>
          <span v-if="worker.tags.length === 0" class="text-xs text-gray-400">无标签</span>
        </div>
        <div v-else class="mt-1 flex gap-2">
          <input v-model="tagsInput" class="flex-1 px-2 py-1 border border-gray-300 rounded text-sm" placeholder="空格分隔标签" />
          <button @click="saveTags" class="px-2 py-1 bg-blue-600 text-white text-xs rounded">保存</button>
          <button @click="editingTags = false" class="px-2 py-1 text-gray-500 text-xs">取消</button>
        </div>

        <div class="text-xs text-gray-500 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
          <span v-if="worker.hostname">{{ worker.hostname }}</span>
          <span v-if="gpuText">{{ gpuText }}</span>
          <span>完成 {{ worker.tasks_completed }}</span>
          <span>失败 {{ worker.tasks_failed }}</span>
          <span v-if="uptimeText">运行 {{ uptimeText }}</span>
          <span>心跳 {{ heartbeatText }}</span>
        </div>
        <div v-if="worker.current_step" class="text-xs text-blue-600 mt-1">
          {{ worker.current_step }} ({{ worker.current_job }})
        </div>
        <div v-if="worker.admin_note && !editing" class="text-xs text-gray-500 mt-1 italic">
          {{ worker.admin_note }}
        </div>

        <!-- Edit note -->
        <div v-if="editing" class="mt-2 flex gap-2">
          <input v-model="noteInput" class="flex-1 px-2 py-1 border border-gray-300 rounded text-sm" placeholder="备注" />
          <button @click="saveNote" class="px-2 py-1 bg-blue-600 text-white text-xs rounded">保存</button>
          <button @click="editing = false" class="px-2 py-1 text-gray-500 text-xs">取消</button>
        </div>
      </div>
    </div>

    <!-- Actions -->
    <div class="flex gap-2 mt-3 pt-3 border-t border-gray-100">
      <button
        @click="toggleDrain"
        class="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-gray-50 transition-colors"
        :class="isDraining ? 'text-green-600' : 'text-orange-600'"
      >
        <component :is="isDraining ? PlayCircle : PauseCircle" :size="14" />
        {{ isDraining ? '恢复' : '排空' }}
      </button>
      <button @click="editingTags = true" class="flex items-center gap-1 px-2 py-1 text-xs text-gray-600 rounded hover:bg-gray-50 transition-colors">
        <Tag :size="14" />
        标签
      </button>
      <button @click="editing = true" class="flex items-center gap-1 px-2 py-1 text-xs text-gray-600 rounded hover:bg-gray-50 transition-colors">
        <Edit3 :size="14" />
        备注
      </button>
      <button @click="remove" class="flex items-center gap-1 px-2 py-1 text-xs text-red-600 rounded hover:bg-red-50 transition-colors">
        <Trash2 :size="14" />
        {{ isRemovable ? '移除' : '强制移除' }}
      </button>
    </div>
  </div>
</template>
