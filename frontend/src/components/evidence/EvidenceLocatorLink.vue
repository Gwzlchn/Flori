<script setup lang="ts">
import { computed } from 'vue'
import { MapPin } from 'lucide-vue-next'
import type {
  CanonicalEvidenceLink,
  CanonicalEvidenceLocator,
  CanonicalEvidenceProjection,
  CanonicalEvidenceSourceProjection,
} from '../../types'

const props = defineProps<{
  evidence: CanonicalEvidenceProjection | null | undefined
  fallback?: string
}>()

function safeServerLink(
  link: CanonicalEvidenceLink | null | undefined,
  locator: CanonicalEvidenceLocator | null | undefined,
): CanonicalEvidenceLink | null {
  if (!link || !locator || link.kind !== locator.kind || typeof link.href !== 'string') return null
  if (!link.href.startsWith('/') || link.href.startsWith('//') || /[\x00-\x1f\\]/.test(link.href)) return null
  if (!['media', 'pdf', 'text', 'image'].includes(link.kind) || typeof link.label !== 'string' || !link.label.trim()) return null
  try {
    let decodedPath = link.href.split(/[?#]/, 1)[0]
    for (let i = 0; i < 3; i += 1) decodedPath = decodeURIComponent(decodedPath)
    if (decodedPath.split('/').includes('..')) return null
  } catch { return null }
  return link
}

const validEvidence = computed(() => {
  const evidence = props.evidence
  return evidence?.status === 'valid'
    && evidence.reason === null
    && /^ce_[0-9a-f]{64}$/.test(evidence.evidence_id)
})

// Resolver 产生链接,前端只接受同站绝对路径,不从 locator 或来源字段拼接。
const safeLink = computed(() => {
  if (!validEvidence.value) return null
  if (Array.isArray(props.evidence?.sources) && props.evidence.sources.length >= 2) return null
  return safeServerLink(props.evidence?.link, props.evidence?.locator)
})

const safeGroupSources = computed(() => {
  const sources = props.evidence?.sources
  if (!validEvidence.value || !Array.isArray(sources) || sources.length < 2) return null
  const seen = new Set<string>()
  const projected: Array<CanonicalEvidenceSourceProjection & { safeLink: CanonicalEvidenceLink }> = []
  for (const source of sources) {
    if (
      !source
      || !Number.isInteger(source.ordinal)
      || source.ordinal !== projected.length
      || typeof source.source_ref !== 'string'
      || !source.source_ref
      || typeof source.source_segment_id !== 'string'
      || !source.source_segment_id
      || typeof source.source_fingerprint !== 'string'
      || !source.source_fingerprint
      || seen.has(source.source_segment_id)
    ) return null
    const link = safeServerLink(source.link, source.locator)
    if (!link) return null
    seen.add(source.source_segment_id)
    projected.push({ ...source, safeLink: link })
  }
  return projected
})

const unavailableLabel = computed(() => {
  if (props.evidence?.status === 'stale') return '证据已过期'
  if (props.evidence?.status === 'missing') return '证据缺失'
  return props.fallback || '定位不可用'
})
</script>

<template>
  <span
    v-if="safeGroupSources"
    class="evidence-locator-group"
    :data-evidence-id="evidence!.evidence_id"
    :aria-label="`联合证据，${safeGroupSources.length} 个来源`"
  >
    <span class="evidence-group-label">联合证据 {{ safeGroupSources.length }} 项</span>
    <a
      v-for="source in safeGroupSources"
      :key="`${source.ordinal}:${source.source_segment_id}`"
      class="evidence-source-link"
      :href="source.safeLink.href"
      :data-source-segment-id="source.source_segment_id"
      :data-locator-kind="source.safeLink.kind"
    >
      <MapPin :size="12" />来源 {{ source.ordinal + 1 }} · {{ source.safeLink.label }}
    </a>
  </span>
  <a
    v-else-if="safeLink"
    class="evidence-locator"
    :href="safeLink.href"
    :data-evidence-id="evidence!.evidence_id"
    :data-locator-kind="safeLink.kind"
  >
    <MapPin :size="12" />{{ safeLink.label }}
  </a>
  <span
    v-else
    class="evidence-unavailable"
    :data-evidence-id="evidence?.evidence_id || undefined"
    :data-evidence-status="evidence?.status || 'missing'"
  >{{ unavailableLabel }}</span>
</template>

<style scoped>
.evidence-locator {
  display: inline-flex; align-items: center; gap: 3px; color: var(--brand-700);
  font-size: 11.5px; text-decoration: none; border-bottom: 1px dashed var(--brand-300);
}
.evidence-locator:hover, .evidence-source-link:hover { background: var(--brand-50); border-bottom-style: solid; }
.evidence-locator-group { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.evidence-group-label { color: var(--ink-500); font-size: 11.5px; font-weight: 600; }
.evidence-source-link {
  display: inline-flex; align-items: center; gap: 3px; color: var(--brand-700);
  font-size: 11.5px; text-decoration: none; border-bottom: 1px dashed var(--brand-300);
}
.evidence-unavailable { color: var(--ink-400); font-size: 11.5px; cursor: default; }
</style>
