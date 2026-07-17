<script setup lang="ts">
import { computed } from 'vue'
import DocumentFigureCard from './DocumentFigureCard.vue'
import DocumentTableCard from './DocumentTableCard.vue'
import VisualCatalogNav from './VisualCatalogNav.vue'
import { useVisualNavigation } from '../../composables/useVisualNavigation'
import type {
  AssetUrlResolver,
  DocumentFigure,
  DocumentQualityReport,
  DocumentTable,
  SourceUrlResolver,
  VisualCatalogItem,
} from './types'

const props = defineProps<{
  figures: DocumentFigure[]
  tables: DocumentTable[]
  quality?: DocumentQualityReport | null
  assetUrl: AssetUrlResolver
  sourceUrl?: SourceUrlResolver
}>()

type FigureVisual = {
  id: string; kind: 'figure'; label: string; caption: string; order: number; value: DocumentFigure
}
type TableVisual = {
  id: string; kind: 'table'; label: string; caption: string; order: number; value: DocumentTable
}
type Visual = FigureVisual | TableVisual

function compareVisual(left: Visual, right: Visual): number {
  return left.order - right.order || left.id.localeCompare(right.id)
}

const figureVisuals = computed<FigureVisual[]>(() => props.figures.map((value, index): FigureVisual => ({
  id: value.figure_id, kind: 'figure', label: value.label, caption: value.caption,
  order: value.order ?? index, value,
})).sort(compareVisual))
const tableVisuals = computed<TableVisual[]>(() => props.tables.map((value, index): TableVisual => ({
  id: value.table_id, kind: 'table', label: value.label, caption: value.caption,
  order: value.order ?? index, value,
})).sort(compareVisual))
const figureCatalog = computed<VisualCatalogItem[]>(() => figureVisuals.value.map((item) => ({
  id: item.id, kind: item.kind, label: item.label, caption: item.caption, order: item.order,
})))
const tableCatalog = computed<VisualCatalogItem[]>(() => tableVisuals.value.map((item) => ({
  id: item.id, kind: item.kind, label: item.label, caption: item.caption, order: item.order,
})))
const visuals = computed<Visual[]>(() => [
  ...figureVisuals.value,
  ...tableVisuals.value,
])
const visualIds = computed(() => visuals.value.map((item) => item.id))
const { activeVisualId, registerVisual, selectVisual } = useVisualNavigation(visualIds)

function setVisualRef(id: string, element: any): void {
  registerVisual(id, element?.$el ?? element ?? null)
}

function sourceUrl(id: string): string | null {
  return props.sourceUrl?.(id) ?? null
}
</script>

<template>
  <section class="document-visuals" aria-labelledby="document-visuals-title">
    <header class="document-visuals-head">
      <div>
        <h2 id="document-visuals-title">图表</h2>
        <p>图 {{ figures.length }} · 表 {{ tables.length }}</p>
      </div>
      <span v-if="quality" class="document-quality" :class="`quality-${quality.status}`">{{ quality.status }}</span>
    </header>
    <p v-if="quality && quality.status !== 'complete'" class="document-quality-reasons" role="status">
      {{ quality.reasons.join('；') }}
    </p>

    <div v-if="visuals.length" class="document-visuals-layout">
      <VisualCatalogNav :figures="figureCatalog" :tables="tableCatalog" :active-id="activeVisualId" @select="selectVisual" />
      <div class="document-visual-list">
        <template v-for="item in visuals" :key="item.id">
          <DocumentFigureCard
            v-if="item.kind === 'figure'"
            :ref="(element) => setVisualRef(item.id, element)"
            :figure="item.value"
            :asset-url="assetUrl"
            :source-url="sourceUrl(item.id)"
          />
          <DocumentTableCard
            v-else
            :ref="(element) => setVisualRef(item.id, element)"
            :table="item.value"
            :asset-url="assetUrl"
            :source-url="sourceUrl(item.id)"
          />
        </template>
      </div>
    </div>
    <div v-else class="document-visual-empty" role="status">当前文档没有可展示的图或表。</div>
  </section>
</template>

<style scoped>
.document-visuals-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.document-visuals-head h2 { color: var(--ink-900); font-size: 18px; }
.document-visuals-head p { margin-top: 2px; color: var(--ink-500); font-size: 12px; }
.document-quality { border-radius: 999px; padding: 3px 8px; font-size: 11px; }
.quality-complete { background: var(--ok-bg); color: var(--ok); }
.quality-degraded { background: var(--warn-bg); color: var(--warn); }
.quality-rejected { background: var(--bad-bg); color: var(--bad); }
.document-quality-reasons { margin-bottom: 12px; color: var(--warn); font-size: 12px; }
.document-visuals-layout { display: grid; grid-template-columns: minmax(180px, 230px) minmax(0, 1fr); gap: 22px; align-items: start; }
.document-visual-list { display: flex; min-width: 0; flex-direction: column; gap: 18px; }
.document-visual-empty { display: grid; min-height: 130px; place-items: center; border: 1px dashed var(--line); border-radius: var(--r-md); color: var(--ink-500); background: var(--raised); }

@media (max-width: 900px) {
  .document-visuals-layout { grid-template-columns: minmax(0, 1fr); gap: 12px; }
}
</style>
