<script setup lang="ts">
import type { Component } from 'vue'
import { ExternalLink } from 'lucide-vue-next'
import StatusBadge from '../../common/StatusBadge.vue'
import { fmtDateTime, fmtDuration } from '../../../utils/datetime'
import { contentTypeLabel } from '../../../utils/contentType'
import type { JobDetail } from '../../../types'

interface LineageVersion {
  job_id: string
  created_at: string
  is_current: boolean
}

defineProps<{
  job: JobDetail
  jobStatus: string
  connected: boolean
  typeIcon: Component
  typeClass: string
  sourceDisplay: string
  bv: string | null
  lineageVersions: LineageVersion[]
  genStart: number | null
  genEnd: number | null
  genDurSec: number | null
  anyRunning: boolean
}>()

defineEmits<{ jumpVersion: [event: Event] }>()
</script>

<template>
  <div class="card pad" style="margin-bottom:16px">
    <div style="display:flex;align-items:flex-start;gap:13px">
      <span class="type-pill" :class="typeClass" style="width:42px;height:42px"><component :is="typeIcon" /></span>
      <div style="flex:1;min-width:0">
        <div class="h1 sm" style="overflow:hidden;text-overflow:ellipsis">{{ job.title || job.job_id }}</div>
        <div class="meta" style="margin-top:5px">
          <StatusBadge :status="jobStatus" />
          <span class="badge b-mut">{{ contentTypeLabel(job.content_type) }}</span>
          <span>{{ sourceDisplay }}</span>
          <template v-if="job.domain"><span class="sep">·</span><span>{{ job.domain }}</span></template>
          <template v-if="bv"><span class="sep">·</span><span class="mono dim">{{ bv }}</span></template>
          <template v-if="job.url">
            <span class="sep">·</span>
            <a class="ghost" :href="job.url" target="_blank" rel="noopener" style="color:var(--info)">原始链接<ExternalLink :size="13" /></a>
          </template>
          <template v-if="lineageVersions.length > 1">
            <span class="sep">·</span>
            <select class="ver-jump" :value="job.job_id" title="同源内容的历史快照(重投/来源更新/pipeline 重建)——跳转查看/对比" @change="$emit('jumpVersion', $event)">
              <option v-for="(version, index) in lineageVersions" :key="version.job_id" :value="version.job_id">
                版本 {{ lineageVersions.length - index }}{{ version.is_current ? '(当前)' : '' }} · {{ fmtDateTime(version.created_at) }}
              </option>
            </select>
          </template>
        </div>
        <div class="dim" style="font-size:12px;margin-top:4px">
          上传于 {{ fmtDateTime(job.published_at) }} · 生成 {{ genStart ? fmtDateTime(genStart) : '—' }} →
          {{ anyRunning ? '进行中' : (genEnd ? fmtDateTime(genEnd) : '—') }} · 耗时 {{ genEnd ? fmtDuration(genDurSec) : '—' }}
        </div>
        <div v-if="jobStatus === 'processing'" class="dim" style="font-size:11.5px;margin-top:4px;display:flex;align-items:center;gap:6px">
          <span class="dot" :class="connected ? 'd-ok pulse' : 'd-bad'" />{{ connected ? '实时更新中' : '连接断开，重连中…' }}
        </div>
      </div>
    </div>
  </div>
</template>
