<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import type { JobSummary } from '../../types'
import StatusBadge from '../common/StatusBadge.vue'
import ProgressBar from '../common/ProgressBar.vue'
import { Video, FileText, FileType, Newspaper, Headphones } from 'lucide-vue-next'

const props = defineProps<{ job: JobSummary }>()
const router = useRouter()

const icon = computed(() => {
  if (props.job.content_type === 'video') return Video
  if (props.job.content_type === 'paper') return FileText
  if (props.job.content_type === 'article') return Newspaper
  if (props.job.content_type === 'audio') return Headphones
  return FileType
})

const timeAgo = computed(() => {
  const diff = Date.now() - new Date(props.job.created_at).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins}分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}小时前`
  const days = Math.floor(hours / 24)
  return `${days}天前`
})

function go() {
  if (props.job.status === 'done') {
    router.push(`/jobs/${props.job.job_id}/notes/smart`)
  } else {
    router.push(`/jobs/${props.job.job_id}`)
  }
}
</script>

<template>
  <div @click="go" class="bg-white border border-gray-200 rounded-xl p-4 cursor-pointer hover:shadow-sm transition-shadow">
    <div class="flex items-start gap-3">
      <div class="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center flex-shrink-0 mt-0.5">
        <component :is="icon" :size="16" class="text-gray-500" />
      </div>
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-1">
          <h4 class="text-sm font-medium truncate">{{ job.title || job.job_id }}</h4>
          <StatusBadge :status="job.status" />
        </div>
        <div class="flex items-center gap-2 text-xs text-gray-500">
          <span v-if="job.source">{{ job.source }}</span>
          <span v-if="job.domain && job.domain !== 'general'">{{ job.domain }}</span>
          <span>{{ timeAgo }}</span>
        </div>
        <ProgressBar
          v-if="job.status === 'processing'"
          :pct="job.progress_pct"
          :animate="true"
          class="mt-2"
        />
      </div>
    </div>
  </div>
</template>
