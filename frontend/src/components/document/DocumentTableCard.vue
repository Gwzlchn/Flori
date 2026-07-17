<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ExternalLink, Table2 } from 'lucide-vue-next'
import type { AssetUrlResolver, DocumentTable, DocumentTableCell } from './types'
import { extractionReasons, extractionStatus } from './types'

const props = defineProps<{
  table: DocumentTable
  assetUrl: AssetUrlResolver
  sourceUrl?: string | null
}>()

const cells = computed(() => [...(props.table.cells ?? [])].sort((a, b) => a.row - b.row || a.col - b.col))
const rows = computed(() => {
  const grouped = new Map<number, DocumentTableCell[]>()
  for (const cell of cells.value) {
    const row = grouped.get(cell.row) ?? []
    row.push(cell)
    grouped.set(cell.row, row)
  }
  return [...grouped.entries()].sort((a, b) => a[0] - b[0])
})
const headerRows = computed(() => rows.value.filter(([, row]) => row.some((cell) => cell.role === 'column_header')))
const bodyRows = computed(() => rows.value.filter(([index]) => !headerRows.value.some(([headerIndex]) => headerIndex === index)))
const crop = computed(() => props.table.representations?.find((item) => item.kind === 'source_crop' && item.artifact)?.artifact ?? null)
const hasStructured = computed(() => cells.value.length > 0 && extractionStatus(props.table) !== 'rejected')
const status = computed(() => extractionStatus(props.table))
const reasons = computed(() => extractionReasons(props.table))
const view = ref<'structured' | 'source'>(hasStructured.value ? 'structured' : 'source')

watch(
  () => props.table.table_id,
  () => { view.value = hasStructured.value ? 'structured' : 'source' },
)
watch([hasStructured, crop], ([structured, sourceCrop]) => {
  if (!structured) view.value = 'source'
  else if (!sourceCrop) view.value = 'structured'
})

function cellTag(cell: DocumentTableCell): 'th' | 'td' {
  return cell.role === 'column_header' || cell.role === 'row_header' ? 'th' : 'td'
}

function cellScope(cell: DocumentTableCell): 'col' | 'row' | undefined {
  if (cell.role === 'column_header') return 'col'
  if (cell.role === 'row_header') return 'row'
  return undefined
}
</script>

<template>
  <article class="visual-card table-card" tabindex="-1" :aria-labelledby="`${table.table_id}-title`">
    <header class="visual-card-head">
      <div>
        <h3 :id="`${table.table_id}-title`">{{ table.label }}</h3>
        <p v-if="table.caption">{{ table.caption }}</p>
      </div>
      <span class="quality-state" :class="`quality-${status}`">{{ status }}</span>
    </header>

    <div v-if="hasStructured && crop" class="table-view-switch" role="group" aria-label="表格显示方式">
      <button type="button" :aria-pressed="view === 'structured'" :class="{ on: view === 'structured' }" @click="view = 'structured'">结构化表格</button>
      <button type="button" :aria-pressed="view === 'source'" :class="{ on: view === 'source' }" @click="view = 'source'">原始区域</button>
    </div>

    <div v-if="view === 'structured' && hasStructured" class="semantic-table-wrap" tabindex="0" aria-label="可横向滚动的结构化表格">
      <table>
        <caption>{{ table.caption || table.label }}</caption>
        <thead v-if="headerRows.length">
          <tr v-for="[rowIndex, row] in headerRows" :key="rowIndex">
            <component :is="cellTag(cell)" v-for="cell in row" :key="cell.cell_id"
              :scope="cellScope(cell)" :rowspan="cell.rowspan || 1" :colspan="cell.colspan || 1">{{ cell.text }}</component>
          </tr>
        </thead>
        <tbody>
          <tr v-for="[rowIndex, row] in bodyRows" :key="rowIndex">
            <component :is="cellTag(cell)" v-for="cell in row" :key="cell.cell_id"
              :scope="cellScope(cell)" :rowspan="cell.rowspan || 1" :colspan="cell.colspan || 1">{{ cell.text }}</component>
          </tr>
        </tbody>
      </table>
    </div>
    <figure v-else-if="crop" class="table-crop">
      <img :src="assetUrl(crop)" :alt="`${table.label} 原始区域：${table.caption}`" loading="lazy" />
      <figcaption>原始表格区域</figcaption>
    </figure>
    <div v-else class="visual-missing" role="status"><Table2 :size="18" />表格结构与原始区域均不可用</div>

    <ul v-if="table.footnotes?.length" class="table-footnotes" aria-label="表格脚注"><li v-for="note in table.footnotes" :key="note">{{ note }}</li></ul>
    <p v-if="reasons.length" class="quality-reasons" role="status">{{ reasons.join('；') }}</p>
    <a v-if="sourceUrl" class="source-jump" :href="sourceUrl"><ExternalLink :size="13" />查看原文位置</a>
  </article>
</template>

<style scoped>
.visual-card { scroll-margin-top: 96px; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface); padding: 16px; box-shadow: var(--sh-sm); }
.visual-card:focus { outline: 2px solid var(--brand-500); outline-offset: 3px; }
.visual-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.visual-card-head h3 { color: var(--ink-900); font-size: 15px; }
.visual-card-head p { margin-top: 3px; color: var(--ink-600); font-size: 13px; line-height: 1.55; }
.quality-state { flex: none; border-radius: 999px; padding: 2px 7px; font-size: 11px; }
.quality-complete { background: var(--ok-bg); color: var(--ok); }
.quality-degraded { background: var(--warn-bg); color: var(--warn); }
.quality-rejected { background: var(--bad-bg); color: var(--bad); }
.table-view-switch { display: inline-flex; margin-bottom: 10px; border: 1px solid var(--line); border-radius: var(--r-sm); overflow: hidden; }
.table-view-switch button { min-height: 36px; padding: 6px 10px; color: var(--ink-600); font-size: 12px; }
.table-view-switch button + button { border-left: 1px solid var(--line); }
.table-view-switch button.on { background: var(--brand-50); color: var(--brand-700); font-weight: 600; }
.table-view-switch button:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.semantic-table-wrap { max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: var(--r-sm); }
.semantic-table-wrap:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
table { width: 100%; min-width: max-content; border-collapse: collapse; color: var(--ink-700); font-size: 12.5px; }
caption { padding: 8px 10px; color: var(--ink-500); text-align: left; }
th, td { padding: 7px 10px; border-top: 1px solid var(--line-soft); border-right: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: var(--raised); color: var(--ink-900); font-weight: 650; }
.table-crop { overflow-x: auto; }
.table-crop img { display: block; max-width: none; min-width: min(100%, 640px); max-height: 72vh; width: auto; border: 1px solid var(--line-soft); border-radius: var(--r-sm); }
.table-crop figcaption { margin-top: 5px; color: var(--ink-500); font-size: 11.5px; }
.visual-missing { display: flex; min-height: 110px; align-items: center; justify-content: center; gap: 7px; border: 1px dashed var(--line); border-radius: var(--r-sm); color: var(--ink-500); background: var(--raised); }
.table-footnotes { margin: 9px 0 0 18px; color: var(--ink-500); font-size: 11.5px; }
.quality-reasons { margin-top: 9px; color: var(--warn); font-size: 12px; }
.source-jump { display: inline-flex; align-items: center; gap: 4px; margin-top: 10px; color: var(--brand-700); font-size: 12px; }

@media (max-width: 600px) {
  .visual-card { padding: 13px; }
  .table-view-switch { display: flex; }
  .table-view-switch button { min-height: 44px; flex: 1; }
}
</style>
