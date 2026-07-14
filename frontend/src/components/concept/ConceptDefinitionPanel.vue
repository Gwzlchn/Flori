<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  FileText, History, Lock, LockOpen, Pencil, RefreshCw, Save, ShieldCheck, X,
} from 'lucide-vue-next'
import EvidenceLocatorLink from '../evidence/EvidenceLocatorLink.vue'
import type { ConceptDefinitionController } from '../../composables/useConceptDefinition'
import type { CanonicalEvidenceProjection, ConceptEvidence } from '../../types'

const props = defineProps<{
  controller: ConceptDefinitionController
  compact?: boolean
}>()

const detail = props.controller.detail
const loading = props.controller.loading
const error = props.controller.error
const actionBusy = props.controller.actionBusy
const actionError = props.controller.actionError
const actionMessage = props.controller.actionMessage

const editing = ref(false)
const draft = ref('')
const definitionChanged = computed(() => (
  detail.value !== null
  && draft.value.trim() !== detail.value.current_definition.definition
))

const attestationLabel = computed(() => {
  const labels = {
    none: '暂无可靠佐证',
    supported: '单源佐证',
    corroborated: '多源互证',
    strong: '强佐证',
  }
  return detail.value ? labels[detail.value.attestation.level] : ''
})

function asProjection(evidence: ConceptEvidence): CanonicalEvidenceProjection {
  const valid = evidence.reason === null && evidence.locator !== null && evidence.link !== null
  const stale = !valid && /stale|changed|mismatch|superseded|过期/i.test(evidence.reason || '')
  return {
    evidence_id: evidence.evidence_id,
    status: valid ? 'valid' : stale ? 'stale' : 'missing',
    reason: evidence.reason,
    job_id: evidence.job_id,
    note_type: evidence.note_type,
    chunk_id: evidence.chunk_id,
    section: evidence.section,
    evidence_fingerprint: null,
    source_fingerprint: evidence.source_fingerprint,
    locator: evidence.locator,
    link: evidence.link,
    validated_at: null,
  }
}

function beginEdit() {
  if (!detail.value || detail.value.definition_locked) return
  props.controller.clearActionNotice()
  draft.value = detail.value.current_definition.definition
  editing.value = true
}

async function save() {
  if (!definitionChanged.value) return
  const beforeVersion = detail.value?.current_definition.definition_version_id
  const beforeLockRevision = detail.value?.lock_revision
  const saved = await props.controller.saveDefinition(draft.value)
  if (
    saved
    || detail.value?.current_definition.definition_version_id !== beforeVersion
    || detail.value?.lock_revision !== beforeLockRevision
  ) editing.value = false
}

async function toggleLock() {
  if (!detail.value) return
  await props.controller.setLocked(!detail.value.definition_locked)
  editing.value = false
}

watch(
  () => detail.value ? `${detail.value.domain}\u0000${detail.value.term}` : null,
  (identity, previousIdentity) => {
    if (identity !== previousIdentity) {
      editing.value = false
      draft.value = ''
    }
  },
  { flush: 'sync' },
)

watch(
  () => detail.value?.current_definition.definition_version_id,
  () => {
    if (!editing.value && detail.value) draft.value = detail.value.current_definition.definition
  },
)
</script>

<template>
  <div class="cdp" :class="{ compact }" data-test="concept-definition-panel">
    <div v-if="loading && !detail" class="cdp-state">正在校验定义与佐证…</div>
    <div v-else-if="error && !detail" class="cdp-state">
      <span>{{ error }}</span>
      <button class="btn sm" data-test="definition-retry" @click="controller.load">重试</button>
    </div>

    <template v-else-if="detail">
      <div class="cdp-head">
        <div class="card-h"><FileText :size="15" />证据定义</div>
        <span class="badge" :class="detail.definition_locked ? 'b-warn' : 'b-mut'">
          <Lock v-if="detail.definition_locked" :size="11" />
          <LockOpen v-else :size="11" />
          {{ detail.definition_locked ? '已锁定' : '可更新' }}
        </span>
        <div class="cdp-actions">
          <button
            class="btn sm" data-test="definition-edit"
            :disabled="detail.definition_locked || actionBusy || editing"
            @click="beginEdit"
          ><Pencil :size="12" />编辑</button>
          <button
            class="btn sm" data-test="definition-lock" :disabled="actionBusy"
            @click="toggleLock"
          >
            <LockOpen v-if="detail.definition_locked" :size="12" />
            <Lock v-else :size="12" />
            {{ detail.definition_locked ? '解锁' : '锁定' }}
          </button>
          <button
            class="btn sm" data-test="definition-resynthesize"
            :disabled="detail.definition_locked || actionBusy"
            @click="controller.resynthesize"
          ><RefreshCw :size="12" :class="{ spin: actionBusy }" />重综合</button>
        </div>
      </div>

      <div v-if="actionError" class="callout err cdp-notice" data-test="definition-error">
        {{ actionError }}
      </div>
      <div v-else-if="actionMessage" class="callout ok cdp-notice" data-test="definition-message">
        {{ actionMessage }}
      </div>

      <div v-if="editing" class="cdp-editor">
        <textarea v-model="draft" class="input" rows="5" data-test="definition-input" />
        <div class="cdp-editor-actions">
          <button class="btn sm" :disabled="actionBusy" @click="editing = false"><X :size="12" />取消</button>
          <button
            class="btn pri sm" data-test="definition-save"
            :disabled="actionBusy || !definitionChanged" @click="save"
          >
            <Save :size="12" />保存新版本
          </button>
        </div>
      </div>
      <p v-else-if="detail.current_definition.definition" class="cdp-definition">
        {{ detail.current_definition.definition }}
      </p>
      <p v-else class="muted">暂无定义</p>

      <div class="cdp-meta">
        <span>当前 v{{ detail.current_definition.version }}</span>
        <span>历史 {{ detail.definition_history_total }} 版</span>
        <span>出现 {{ detail.occurrence_total }} 处</span>
        <span>{{ attestationLabel }}</span>
      </div>

      <div class="cdp-attestation">
        <div class="seclabel"><ShieldCheck :size="13" />现场佐证</div>
        <div class="cdp-counts">
          {{ detail.attestation.evidence_count }} 条证据 ·
          {{ detail.attestation.job_count }} 条内容 ·
          {{ detail.attestation.source_fingerprint_count }} 个独立来源 ·
          {{ detail.attestation.content_type_count }} 种内容
        </div>
        <div v-if="detail.attestation.included.length" class="cdp-evidence-list">
          <div
            v-for="evidence in detail.attestation.included"
            :key="evidence.evidence_id"
            class="cdp-evidence"
          >
            <div class="cdp-evidence-head">
              <span>{{ evidence.section || evidence.note_type || evidence.content_type }}</span>
              <EvidenceLocatorLink :evidence="asProjection(evidence)" />
            </div>
            <p v-if="evidence.excerpt" class="cdp-excerpt">{{ evidence.excerpt }}</p>
          </div>
        </div>
        <p v-else class="muted cdp-empty">当前没有通过可靠性门禁的可定位证据</p>
        <details v-if="detail.attestation.excluded.length" class="cdp-excluded">
          <summary>已排除 {{ detail.attestation.excluded.length }} 条失效或不可靠证据</summary>
          <div
            v-for="evidence in detail.attestation.excluded"
            :key="evidence.evidence_id"
            class="cdp-evidence-head"
          >
            <span>{{ evidence.section || evidence.note_type || evidence.content_type }}</span>
            <EvidenceLocatorLink :evidence="asProjection(evidence)" :fallback="evidence.reason || '已排除'" />
          </div>
        </details>
      </div>

      <details v-if="detail.definition_history.length" class="cdp-history">
        <summary><History :size="13" />定义历史 · {{ detail.definition_history_total }}</summary>
        <div
          v-for="version in detail.definition_history"
          :key="version.definition_version_id"
          class="cdp-version"
          :data-current="version.definition_version_id === detail.current_definition.definition_version_id || undefined"
        >
          <div class="cdp-version-head">
            <strong>v{{ version.version }}</strong>
            <span>{{ version.strategy }}</span>
            <span>{{ version.source_evidence_ids.length }} 条来源证据</span>
          </div>
          <p>{{ version.definition || '（空定义）' }}</p>
        </div>
        <p v-if="detail.definition_history_total > detail.definition_history.length" class="muted cdp-empty">
          仅展示最近 {{ detail.definition_history_limit }} 个版本
        </p>
      </details>
    </template>
  </div>
</template>

<style scoped>
.cdp { min-width: 0; }
.cdp-state { display: flex; align-items: center; justify-content: center; gap: 10px; min-height: 86px; color: var(--ink-500); font-size: 13px; }
.cdp-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cdp-head .card-h { margin: 0; }
.cdp-actions { display: flex; gap: 6px; margin-left: auto; flex-wrap: wrap; }
.cdp-notice { margin: 10px 0 0; font-size: 12px; }
.cdp-notice.err { background: var(--bad-bg); border: 1px solid var(--bad-bd); color: var(--bad); }
.cdp-notice.ok { background: var(--ok-bg); border: 1px solid var(--ok-bd); color: var(--ok); }
.cdp-definition { color: var(--ink-700); line-height: 1.65; white-space: pre-wrap; margin: 12px 0 0; }
.cdp-editor { display: grid; gap: 8px; margin-top: 12px; }
.cdp-editor textarea { width: 100%; resize: vertical; line-height: 1.55; }
.cdp-editor-actions { display: flex; justify-content: flex-end; gap: 6px; }
.cdp-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--ink-500); font-size: 12px; margin-top: 12px; }
.cdp-attestation { border-top: 1px solid var(--line); margin-top: 14px; padding-top: 12px; }
.cdp-counts { color: var(--ink-500); font-size: 12px; margin-top: 6px; }
.cdp-evidence-list { display: grid; gap: 8px; margin-top: 9px; }
.cdp-evidence { background: var(--ink-50, #f7f7f5); border-radius: var(--r-sm); padding: 8px 10px; }
.cdp-evidence-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--ink-600); font-size: 12px; }
.cdp-excerpt { color: var(--ink-700); font-size: 12px; line-height: 1.5; margin: 6px 0 0; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.cdp-empty { font-size: 12px; margin: 8px 0 0; }
.cdp-excluded, .cdp-history { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 9px; }
.cdp-excluded summary, .cdp-history summary { display: flex; align-items: center; gap: 5px; color: var(--ink-500); cursor: pointer; font-size: 12px; }
.cdp-excluded .cdp-evidence-head { margin-top: 8px; }
.cdp-version { border-left: 2px solid var(--line); margin-top: 9px; padding: 2px 0 2px 9px; }
.cdp-version[data-current="true"] { border-left-color: var(--brand-500); }
.cdp-version-head { display: flex; flex-wrap: wrap; gap: 7px; color: var(--ink-500); font-size: 11px; }
.cdp-version p { color: var(--ink-700); font-size: 12px; line-height: 1.5; margin: 4px 0 0; white-space: pre-wrap; }
.compact .cdp-actions { margin-left: 0; width: 100%; }
.compact .cdp-actions .btn { flex: 1; justify-content: center; }
.spin { animation: cdp-spin .8s linear infinite; }
@keyframes cdp-spin { to { transform: rotate(360deg); } }
</style>
