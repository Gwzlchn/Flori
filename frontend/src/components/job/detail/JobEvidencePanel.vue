<script setup lang="ts">
import { ExternalLink, ShieldCheck } from 'lucide-vue-next'

type EvidenceItem = Record<string, any>
defineProps<{
  items: EvidenceItem[]
  manifestState: 'valid' | 'partial' | 'invalid' | 'legacy'
  manifestErrors: string[]
  safeUrl: (item: EvidenceItem) => string
  safeArtifact: (item: EvidenceItem) => string
  artifactUrl: (path: string) => string
  matches: (item: EvidenceItem) => Record<string, any>[]
  reasons: (item: EvidenceItem) => string[]
  verificationReasons: (item: EvidenceItem) => string[]
}>()
</script>

<template>
  <div class="card pad">
    <div class="card-h"><ShieldCheck :size="15" />权威来源<template v-if="items.length"> · {{ items.length }}</template></div>
    <p class="lead" style="margin:-6px 0 12px">候选来源经服务端受控下载与完整性校验。只有合格证据可从笔记 [E#] 跳转或打开原文。</p>
    <p v-if="manifestState === 'legacy'" class="lead warning">旧版证据未验证,链接已禁用。</p>
    <p v-else-if="manifestState === 'partial'" class="lead warning">部分证据未通过当前校验,仅已验证条目可用。</p>
    <p v-else-if="manifestState === 'invalid'" class="lead warning">证据清单校验失败,链接已禁用。</p>
    <p v-if="manifestErrors.length" class="lead warning">校验原因: {{ manifestErrors.join(' / ') }}</p>
    <div v-for="source in items" :key="source.id" class="card pad" style="margin-bottom:10px" :data-evidence-card="source.id">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
        <span class="chip on" style="cursor:default">{{ source.id }}</span><span class="badge">{{ source.source_tier || '来源待定' }}</span>
        <span style="font-size:11px;font-weight:600" :style="{ color: source.confidence === 'high' ? '#15803d' : '#b45309' }">{{ source.confidence }}</span><strong style="font-size:13.5px">{{ source.title }}</strong>
      </div>
      <div style="font-size:12px;color:var(--ink-500);margin-bottom:6px">
        {{ source.publisher }}
        <a v-if="safeUrl(source)" :href="safeUrl(source)" target="_blank" rel="noopener" style="margin-left:6px;display:inline-flex;align-items:center;gap:2px"><ExternalLink :size="12" />原文链接</a>
        <span v-else class="dim" style="margin-left:6px">链接不可用</span>
        <a v-if="safeArtifact(source)" :href="artifactUrl(safeArtifact(source))" target="_blank" rel="noopener" style="margin-left:6px">证据全文</a>
      </div>
      <div v-if="matches(source).length" class="dim" style="font-size:12px">命中锚点: {{ matches(source).map((match) => match.anchor).join(' / ') }}</div>
      <div v-if="reasons(source).length" class="dim" style="font-size:12px">{{ reasons(source).join(' / ') }}</div>
      <div v-if="verificationReasons(source).length" class="dim" style="font-size:12px">校验原因: {{ verificationReasons(source).join(' / ') }}</div>
    </div>
  </div>
</template>

<style scoped>.warning { color: var(--warn, #b45309); }</style>
