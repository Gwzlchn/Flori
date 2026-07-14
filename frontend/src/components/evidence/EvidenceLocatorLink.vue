<script setup lang="ts">
import { computed } from 'vue'
import { MapPin } from 'lucide-vue-next'
import type { CanonicalEvidenceProjection } from '../../types'

const props = defineProps<{
  evidence: CanonicalEvidenceProjection | null | undefined
  fallback?: string
}>()

// Resolver 产生链接,前端只接受同站绝对路径,不从 locator 或 job_id 拼接。
const safeLink = computed(() => {
  const evidence = props.evidence
  const link = evidence?.link
  if (
    evidence?.status !== 'valid'
    || evidence.reason !== null
    || !/^ce_[0-9a-f]{64}$/.test(evidence.evidence_id)
    || !evidence.locator
    || !link
    || link.kind !== evidence.locator.kind
    || typeof link.href !== 'string'
  ) return null
  if (!link.href.startsWith('/') || link.href.startsWith('//') || /[\x00-\x1f\\]/.test(link.href)) return null
  if (!['media', 'pdf', 'text', 'image'].includes(link.kind) || typeof link.label !== 'string' || !link.label.trim()) return null
  try {
    let decodedPath = link.href.split(/[?#]/, 1)[0]
    for (let i = 0; i < 3; i += 1) decodedPath = decodeURIComponent(decodedPath)
    if (decodedPath.split('/').includes('..')) return null
  } catch { return null }
  return link
})

const unavailableLabel = computed(() => {
  if (props.evidence?.status === 'stale') return '证据已过期'
  if (props.evidence?.status === 'missing') return '证据缺失'
  return props.fallback || '定位不可用'
})
</script>

<template>
  <a
    v-if="safeLink"
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
.evidence-locator:hover { background: var(--brand-50); border-bottom-style: solid; }
.evidence-unavailable { color: var(--ink-400); font-size: 11.5px; cursor: default; }
</style>
