<!-- 把内容处理状态（JobStatus）映射成中文文案 + badge 变体。参照原型第 3 节状态色。 -->
<script setup lang="ts">
import { computed } from 'vue'
import { Check, Download, RefreshCw, AlertTriangle, Clock } from 'lucide-vue-next'
import type { JobStatus, BadgeVariant } from '@/types'
import BaseBadge from './BaseBadge.vue'

const props = defineProps<{
  status: JobStatus
}>()

interface StatusMeta {
  label: string
  variant: BadgeVariant
  icon: typeof Check | null
}

const STATUS_MAP: Record<JobStatus, StatusMeta> = {
  pending: { label: '待处理', variant: 'warn', icon: Clock },
  downloading: { label: '下载中', variant: 'info', icon: Download },
  processing: { label: '处理中', variant: 'run', icon: RefreshCw },
  done: { label: '已完成', variant: 'ok', icon: Check },
  failed: { label: '失败', variant: 'bad', icon: AlertTriangle },
}

const meta = computed(() => STATUS_MAP[props.status])
</script>

<template>
  <BaseBadge :variant="meta.variant">
    <component :is="meta.icon" v-if="meta.icon" :size="12" />
    {{ meta.label }}
  </BaseBadge>
</template>
