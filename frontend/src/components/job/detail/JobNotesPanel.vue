<script setup lang="ts">
import { ChevronDown, ExternalLink, FileText, Languages, List, RefreshCw, Star } from 'lucide-vue-next'
import MarkdownViewer from '../../notes/MarkdownViewer.vue'
import type { CanonicalEvidenceProjection } from '../../../types'
import { languageName } from '../../../utils/language'

type NoteVariant = 'smart' | 'original' | 'translated' | 'pdf'
interface Version { provider: string; model: string; version: string; file: string; review_file: string | null; overall: number | null; review_state?: string | null }
interface Provider { name: string; type: string; available: boolean; label: string }
interface Heading { id: string; text: string; level: number }
interface Term { term: string; zh_name?: string; aliases?: string[] }

defineProps<{
  jobId: string
  domain: string
  hasSmartNote: boolean
  hasTranslation: boolean
  hasReadableOriginal: boolean
  hasPaperPdf: boolean
  noteVariant: NoteVariant
  versions: Version[]
  activeFile: string | null
  rerunning: boolean
  showRerun: boolean
  providers: Provider[]
  noteLoading: boolean
  noteError: string
  isPaper: boolean
  paperPdfUrl: string
  noteContent: string
  originalLanguage?: string | null
  terms: Term[]
  evidenceIds: string[]
  canonicalEvidence: CanonicalEvidenceProjection[]
  headings: Heading[]
  versionLabel: (version: Version) => string
}>()

defineEmits<{
  switchVariant: [variant: NoteVariant]
  selectVersion: [file: string]
  toggleRerun: []
  rerun: [provider: Provider]
  headings: [headings: Heading[]]
  pdfPage: [page: number]
  evidenceCitation: [id: string]
}>()
</script>

<template>
  <div class="note-toolbar">
    <div v-if="hasSmartNote || hasTranslation || hasPaperPdf" class="seg">
      <button v-if="hasSmartNote" :class="{ on: noteVariant === 'smart' }" @click="$emit('switchVariant', 'smart')">智能版</button>
      <button v-if="!isPaper || hasReadableOriginal" :class="{ on: noteVariant === 'original' }" @click="$emit('switchVariant', 'original')">{{ hasReadableOriginal ? '原文' : '机械版' }}</button>
      <button v-if="hasTranslation" :class="{ on: noteVariant === 'translated' }" @click="$emit('switchVariant', 'translated')">译文</button>
      <button v-if="hasPaperPdf" :class="{ on: noteVariant === 'pdf' }" @click="$emit('switchVariant', 'pdf')">原文 PDF</button>
    </div>
    <span v-else class="dim" style="font-size:12px">{{ hasReadableOriginal ? '原文' : '机械版' }}（未生成智能笔记）</span>
    <template v-if="noteVariant === 'smart'">
      <span class="dim" style="font-size:12px;margin-left:6px">版本</span><span v-if="versions.length === 0" class="chip on" style="cursor:default">默认</span>
      <span v-for="version in versions" :key="version.file" class="chip version" :class="{ on: (activeFile ?? versions[0]?.file) === version.file }" @click="$emit('selectVersion', version.file)">
        <span>{{ version.provider }}/{{ version.model }} · {{ versionLabel(version) }}</span><template v-if="version.overall != null"><Star :size="11" />{{ version.overall }}</template>
        <span v-else-if="version.review_state === 'unreliable'" class="dim">评审不可靠</span><span v-else-if="version.review_state === 'legacy_unverified'" class="dim">旧版未验证</span>
      </span>
      <div class="rerun-menu">
        <button class="btn sm" :disabled="rerunning" @click="$emit('toggleRerun')"><RefreshCw :size="13" :class="rerunning ? 'pulse' : ''" />{{ rerunning ? '生成中…' : '换 provider 重跑' }}<ChevronDown :size="13" /></button>
        <div v-if="showRerun" class="card provider-menu">
          <button v-for="provider in providers" :key="provider.name" class="iconbtn" :disabled="!provider.available" :class="{ unavailable: !provider.available }" @click="$emit('rerun', provider)">
            <span>{{ provider.name }} <span class="dim">({{ provider.label }})</span></span><span v-if="!provider.available" class="dim">无 key</span>
          </button>
          <div v-if="providers.length === 0" class="dim empty-provider">无可用 provider</div>
        </div>
      </div>
    </template>
  </div>
  <slot />
  <div v-if="noteLoading" class="card pad"><div class="state"><span class="spinner" />加载笔记…</div></div>
  <div v-else-if="noteError" class="card pad"><div class="state"><FileText class="big" /><div class="t">{{ noteError }}</div></div></div>
  <div v-else-if="noteVariant === 'pdf' && hasPaperPdf" class="pdf-wrap">
    <div class="pdf-head">
      <span class="lead"><FileText :size="13" /> PDF 保留论文原始公式、图表和版式。</span>
      <a :href="paperPdfUrl" target="_blank" rel="noopener">新窗口打开<ExternalLink :size="13" /></a>
    </div>
    <iframe :src="paperPdfUrl" class="pdf-frame" title="论文 PDF 原文" loading="lazy" />
  </div>
  <div v-else class="notes-wrap">
    <p v-if="noteVariant === 'translated'" class="lead translated">
      <Languages :size="13" /><span>原文为{{ languageName(originalLanguage) }},以下为 AI 忠实全文译文(保留原结构与配图)。</span>
    </p>
    <div class="card pad prose max-w-none">
      <MarkdownViewer :content="noteContent" :job-id="jobId" :terms="terms" :domain="domain" :evidence-ids="evidenceIds" :canonical-evidence="canonicalEvidence"
        @headings="$emit('headings', $event)" @pdf-page="$emit('pdfPage', $event)" @evidence-citation="$emit('evidenceCitation', $event)" />
    </div>
    <nav v-if="headings.length" class="toc"><div class="seclabel"><List :size="14" />章节</div><a v-for="heading in headings" :key="heading.id" :href="`#${heading.id}`" :class="{ sub: heading.level >= 3 }">{{ heading.text }}</a></nav>
  </div>
</template>

<style scoped>
.note-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.version { max-width: 240px; }
.version > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.version svg { color: var(--amber); }
.rerun-menu { position: relative; margin-left: auto; }
.provider-menu { position: absolute; right: 0; top: calc(100% + 6px); width: 200px; z-index: 30; padding: 5px; box-shadow: var(--sh-lg); }
.provider-menu button { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 9px; border-radius: var(--r-sm); font-size: 12px; text-align: left; }
.provider-menu button > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.provider-menu button > span:last-child { font-size: 11px; flex: none; }
.provider-menu button.unavailable { opacity: .5; cursor: not-allowed; }
.empty-provider { font-size: 12px; padding: 8px 9px; }
.pdf-wrap { min-width: 0; }
.pdf-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.pdf-head .lead { display: inline-flex; align-items: center; gap: 5px; margin: 0; }
.pdf-head a { display: inline-flex; align-items: center; gap: 4px; flex: none; font-size: 12px; }
.pdf-frame { width: 100%; height: 82vh; border: 1px solid var(--line-soft); border-radius: 10px; background: #f9fafb; }
.translated { grid-column: 1/-1; margin: -6px 0 0; display: flex; align-items: flex-start; gap: 5px; }
.translated svg { flex: none; margin-top: 2px; }
@media (max-width: 600px) {
  .pdf-head { align-items: flex-start; }
  .pdf-frame { height: 72vh; }
}
</style>
