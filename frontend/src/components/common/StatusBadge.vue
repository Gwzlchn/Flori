<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const config = computed(() => {
  const map: Record<string, { bg: string; text: string; label: string }> = {
    pending: { bg: 'bg-gray-100', text: 'text-gray-600', label: '等待' },
    downloading: { bg: 'bg-blue-100', text: 'text-blue-700', label: '下载中' },
    processing: { bg: 'bg-blue-100', text: 'text-blue-700', label: '处理中' },
    done: { bg: 'bg-green-100', text: 'text-green-700', label: '完成' },
    failed: { bg: 'bg-red-100', text: 'text-red-700', label: '失败' },
    waiting: { bg: 'bg-gray-100', text: 'text-gray-600', label: '等待' },
    running: { bg: 'bg-blue-100', text: 'text-blue-700', label: '运行中' },
    skipped: { bg: 'bg-gray-100', text: 'text-gray-500', label: '跳过' },
    ready: { bg: 'bg-yellow-100', text: 'text-yellow-700', label: '就绪' },
    idle: { bg: 'bg-green-100', text: 'text-green-700', label: '空闲' },
    busy: { bg: 'bg-yellow-100', text: 'text-yellow-700', label: '忙碌' },
    'online-idle': { bg: 'bg-green-100', text: 'text-green-700', label: '空闲' },
    'online-busy': { bg: 'bg-blue-100', text: 'text-blue-700', label: '忙碌' },
    draining: { bg: 'bg-orange-100', text: 'text-orange-700', label: '排空中' },
    offline: { bg: 'bg-gray-200', text: 'text-gray-600', label: '离线' },
    stale: { bg: 'bg-gray-100', text: 'text-gray-500', label: '失联' },
  }
  return map[props.status] || { bg: 'bg-gray-100', text: 'text-gray-600', label: props.status }
})
</script>

<template>
  <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium" :class="[config.bg, config.text]">
    {{ config.label }}
  </span>
</template>
