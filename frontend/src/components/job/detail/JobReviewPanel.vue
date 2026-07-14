<script setup lang="ts">
import { Star } from 'lucide-vue-next'

interface ReviewDimension { label: string; score: number }
interface KeyTerm { term: string; definition: string }
type ReviewIssue = Record<string, any>

defineProps<{
  review: Record<string, any>
  reliable: boolean
  state: string
  reasons: string[]
  dimensions: ReviewDimension[]
  missingConcepts: string[]
  improvements: string[]
  issues: ReviewIssue[]
  dimensionLabels: Record<string, string>
  keyTerms: KeyTerm[]
  acceptedTerms: Set<string>
  sourcePath: (label: string) => string
  artifactUrl: (path: string) => string
}>()

defineEmits<{ accept: [term: string, definition: string] }>()
</script>

<template>
  <div class="review" style="margin-bottom:16px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <b style="font-size:13.5px;color:var(--ink-900)">质量评审</b>
      <span v-if="reliable && review.overall != null" class="badge b-warn"><Star :size="12" />{{ review.overall }} / 5</span>
      <span v-if="reliable" class="badge b-ok">可靠</span><span v-else-if="state === 'unreliable'" class="badge b-warn">不可靠,仅供诊断</span><span v-else class="badge">旧版未验证</span>
      <span class="dim" style="font-size:12px">{{ review.provider }}<template v-if="review.model"> / {{ review.model }}</template><template v-if="review.generated_at"> · {{ review.generated_at }}</template></span>
    </div>
    <div v-if="!reliable" class="lead" style="margin:8px 0;color:var(--warn,#b45309)">本次评审不会用于术语采纳或知识沉淀。<template v-if="reasons.length">原因:{{ reasons.join(' / ') }}</template></div>
    <div v-if="dimensions.length" class="dims">
      <div v-for="dimension in dimensions" :key="dimension.label" class="dim-g"><div class="row-l">{{ dimension.label }}<b>{{ dimension.score }}</b></div><div class="track"><span :style="{ width: Math.max(0, Math.min(100, dimension.score * 20)) + '%' }" /></div></div>
    </div>
    <div v-if="missingConcepts.length" class="review-copy"><span class="dim">缺失概念：</span>{{ missingConcepts.join(' / ') }}</div>
    <div v-if="improvements.length" class="review-copy"><span class="dim">改进建议：</span><ol><li v-for="(item, index) in improvements" :key="index">{{ item }}</li></ol></div>
    <div v-if="issues.length" class="review-issues" style="margin-bottom:8px">
      <div class="dim" style="font-size:12.5px;margin-bottom:4px">结构化问题:</div>
      <div v-for="(issue, index) in issues" :key="index" class="card pad issue">
        <div class="issue-head"><span class="badge">{{ dimensionLabels[issue.dimension] || issue.dimension || issue.type }}</span><b>{{ issue.claim || issue.message }}</b><span class="dim">{{ issue.severity }}</span></div>
        <div v-if="issue.message && issue.message !== issue.claim" style="margin-top:3px">{{ issue.message }}</div>
        <div v-if="reliable && issue.evidence_status === 'supported' && issue.locator" class="dim" style="margin-top:3px">
          证据 {{ issue.locator.source }}: “{{ issue.locator.quote }}”
          <a v-if="sourcePath(issue.locator.source)" :href="artifactUrl(sourcePath(issue.locator.source))" target="_blank" rel="noopener" style="margin-left:6px">查看来源</a>
        </div>
        <div v-else-if="issue.evidence_status === 'insufficient'" class="dim" style="margin-top:3px">证据不足: {{ issue.reason }}</div>
      </div>
    </div>
    <div v-if="keyTerms.length" style="font-size:12.5px;color:var(--ink-600)">
      <span class="dim">已讲清的概念（可采纳）：</span>
      <span v-for="term in keyTerms" :key="term.term" class="key-term"><b>{{ term.term }}</b><button class="btn sm" :disabled="!reliable || acceptedTerms.has(term.term)" :class="{ accepted: acceptedTerms.has(term.term) }" @click="$emit('accept', term.term, term.definition)">{{ acceptedTerms.has(term.term) ? '✓ 已采纳' : '采纳' }}</button></span>
    </div>
  </div>
</template>

<style scoped>
.review-copy { font-size: 12.5px; color: var(--ink-600); margin-bottom: 6px; }
.review-copy ol { margin: 4px 0 0 18px; }
.issue { padding: 8px 10px; margin-bottom: 6px; font-size: 12.5px; }
.issue-head { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.key-term { display: inline-flex; align-items: center; margin: 0 6px 4px 0; }
.key-term b { color: var(--ink-900); }
.key-term button { padding: 2px 8px; margin-left: 6px; }
.key-term button.accepted { color: var(--ok); border-color: var(--ok-bd); }
</style>
