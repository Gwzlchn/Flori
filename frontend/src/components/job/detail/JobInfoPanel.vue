<script setup lang="ts">
import { ref } from 'vue'
import { ChevronDown, ExternalLink, GitBranch, Info, RotateCcw, Trash2 } from 'lucide-vue-next'
import StatusBadge from '../../common/StatusBadge.vue'
import { contentTypeLabel } from '../../../utils/contentType'
import { fmtDateTime, fmtDuration } from '../../../utils/datetime'
import type { JobDetail } from '../../../types'

defineProps<{
  job: JobDetail
  jobStatus: string
  sourceDisplay: string
  bv: string | null
  collectionId: string | null
  collectionName: string | null
  genEnd: number | null
  genDurSec: number | null
  anyRunning: boolean
}>()

defineEmits<{ retry: []; delete: [] }>()
const showArtifacts = ref(false)

function fmtBitrate(kbps?: number): string {
  if (kbps == null) return '—'
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbps` : `${kbps} kbps`
}

function fmtSize(media: { file_size_bytes?: number; file_size_mb?: number }): string {
  let bytes = media.file_size_bytes
  if (bytes == null && media.file_size_mb != null) bytes = media.file_size_mb * 1048576
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++ }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`
}
</script>

<template>
  <div class="card pad">
    <div class="card-h"><Info :size="15" />内容信息</div>
    <table class="kv"><tbody>
      <tr><td>标题</td><td>{{ job.title || '—' }}</td></tr><tr><td>类型</td><td>{{ contentTypeLabel(job.content_type) }}</td></tr>
      <tr><td>来源</td><td>{{ sourceDisplay }}</td></tr><tr v-if="job.media?.authors?.length"><td>作者</td><td>{{ job.media.authors.join('、') }}</td></tr>
      <tr><td>发布时间</td><td>{{ fmtDateTime(job.published_at) }}</td></tr>
      <tr v-if="job.media?.duration_sec"><td>时长</td><td>{{ fmtDuration(job.media.duration_sec) }}</td></tr>
      <tr v-if="job.media?.resolution"><td>分辨率</td><td class="mono">{{ job.media.resolution }}</td></tr>
      <tr v-if="job.media?.video_codec"><td>视频编码</td><td class="mono">{{ job.media.video_codec }}</td></tr>
      <tr v-if="job.media?.audio_codec"><td>音频编码</td><td class="mono">{{ job.media.audio_codec }}</td></tr>
      <tr v-if="job.media?.fps"><td>帧率</td><td>{{ job.media.fps }} fps</td></tr>
      <tr v-if="job.media?.bitrate_kbps ?? job.media?.video_bitrate_kbps"><td>码率</td><td>{{ fmtBitrate(job.media.bitrate_kbps ?? job.media.video_bitrate_kbps) }}</td></tr>
      <tr v-if="job.media?.word_count"><td>字数</td><td>{{ job.media.word_count.toLocaleString() }} 字</td></tr>
      <tr v-if="job.media?.pages"><td>页数</td><td>{{ job.media.pages }} 页</td></tr>
      <tr v-if="job.media?.lang"><td>语言</td><td>{{ job.media.lang === 'zh' ? '中文' : (job.media.lang === 'non-zh' ? '非中文(英文等,自动翻译)' : job.media.lang) }}</td></tr>
      <tr v-if="job.media?.tags?.length"><td>标签</td><td>{{ job.media.tags.join('、') }}</td></tr>
      <tr v-if="job.media?.abstract"><td>摘要</td><td style="line-height:1.6">{{ job.media.abstract }}</td></tr>
      <tr v-if="job.media && (job.media.file_size_bytes != null || job.media.file_size_mb != null)"><td>原始文件大小</td><td>{{ fmtSize(job.media) }}</td></tr>
      <tr v-if="['video','audio'].includes(job.content_type) && job.media && (job.media.has_subtitle !== undefined || job.media.has_danmaku !== undefined)">
        <td>字幕/弹幕</td><td><span class="badge" :class="job.media.has_subtitle ? 'b-ok' : 'b-mut'">{{ job.media.has_subtitle ? '有字幕' : '无字幕' }}</span><span v-if="job.media.has_danmaku" class="badge b-info" style="margin-left:5px">有弹幕</span></td>
      </tr>
      <tr v-if="bv"><td>BV 号</td><td class="mono">{{ bv }}</td></tr>
      <tr v-if="job.url"><td>原始链接</td><td><a class="ghost" :href="job.url" target="_blank" rel="noopener" style="color:var(--info)">{{ job.url }}<ExternalLink :size="13" /></a></td></tr>
    </tbody></table>
  </div>
  <div class="card pad" style="margin-top:16px">
    <div class="card-h"><GitBranch :size="15" />处理信息</div>
    <table class="kv"><tbody>
      <tr><td>Job ID</td><td class="mono">{{ job.job_id }}</td></tr><tr><td>状态</td><td><StatusBadge :status="jobStatus" /></td></tr>
      <tr><td>知识库</td><td>{{ job.domain || '—' }}</td></tr>
      <tr><td>集合</td><td><template v-if="collectionName">{{ collectionName }}<span v-if="collectionId" class="mono dim" style="font-size:11.5px;margin-left:6px">{{ collectionId }}</span></template><span v-else class="dim">未归集合</span></td></tr>
      <tr><td>创建于</td><td>{{ fmtDateTime(job.created_at) }}</td></tr><tr v-if="job.updated_at"><td>更新于</td><td>{{ fmtDateTime(job.updated_at) }}</td></tr>
      <tr><td>生成耗时</td><td>{{ genEnd ? fmtDuration(genDurSec) : (anyRunning ? '进行中' : '—') }}</td></tr>
    </tbody></table>
    <div v-if="job.artifacts?.length" class="artifacts">
      <button class="art-toggle" @click="showArtifacts = !showArtifacts"><ChevronDown :size="14" class="art-caret" :class="{ open: showArtifacts }" />产物路径 · {{ job.artifacts.length }}</button>
      <ul v-show="showArtifacts" class="art-list"><li v-for="path in job.artifacts" :key="path" class="mono">{{ path }}</li></ul>
    </div>
    <div style="margin-top:16px;display:flex;gap:8px">
      <button v-if="jobStatus === 'failed'" class="btn" @click="$emit('retry')"><RotateCcw :size="14" />重新提交</button>
      <button class="btn danger" @click="$emit('delete')"><Trash2 :size="14" />删除内容</button>
    </div>
  </div>
</template>

<style scoped>
.artifacts { margin-top: 16px; border-top: 1px solid var(--line-soft); padding-top: 12px; }
.art-toggle { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; color: var(--ink-700); background: none; cursor: pointer; padding: 0; }
.art-caret { transition: transform .15s; transform: rotate(-90deg); }
.art-caret.open { transform: rotate(0deg); }
.art-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.art-list li { font-size: 12px; color: var(--ink-600); padding: 3px 8px; border-radius: 5px; background: var(--raised); border: 1px solid var(--line-soft); word-break: break-all; }
</style>
