<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, ExternalLink, LoaderCircle } from 'lucide-vue-next'
import type { PDFDocumentProxy, TextLayer as PDFTextLayer } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

const props = defineProps<{
  url: string
  page?: number
  bboxes?: [number, number, number, number][]
}>()

const canvas = ref<HTMLCanvasElement | null>(null)
const textLayer = ref<HTMLDivElement | null>(null)
const loading = ref(true)
const error = ref('')
const currentPage = ref(Math.max(1, props.page || 1))
const pageCount = ref(0)
const viewportSize = ref({ width: 1, height: 1, scale: 1 })
let documentProxy: PDFDocumentProxy | null = null
let pdfjsModule: typeof import('pdfjs-dist') | null = null
let textLayerRender: PDFTextLayer | null = null
let renderToken = 0

function clearTextLayer(): void {
  textLayerRender?.cancel()
  textLayerRender = null
  textLayer.value?.replaceChildren()
}

const percent = (value: number) => `${Number(value.toFixed(6))}%`
const overlayBoxes = computed(() => (props.bboxes || []).map((bbox) => ({
  left: percent(bbox[0] * viewportSize.value.scale / viewportSize.value.width * 100),
  top: percent(bbox[1] * viewportSize.value.scale / viewportSize.value.height * 100),
  width: percent((bbox[2] - bbox[0]) * viewportSize.value.scale / viewportSize.value.width * 100),
  height: percent((bbox[3] - bbox[1]) * viewportSize.value.scale / viewportSize.value.height * 100),
})))

async function renderPage(): Promise<void> {
  const token = ++renderToken
  clearTextLayer()
  if (!documentProxy || !canvas.value || !textLayer.value || !pdfjsModule) return
  const page = await documentProxy.getPage(currentPage.value)
  if (token !== renderToken || !canvas.value || !textLayer.value) return
  const viewport = page.getViewport({ scale: 1.6 })
  const context = canvas.value.getContext('2d')
  if (!context) throw new Error('canvas context unavailable')
  canvas.value.width = Math.ceil(viewport.width)
  canvas.value.height = Math.ceil(viewport.height)
  viewportSize.value = {
    width: viewport.width,
    height: viewport.height,
    scale: viewport.scale,
  }
  await page.render({ canvasContext: context, viewport }).promise
  const textContent = await page.getTextContent()
  if (token !== renderToken || !textLayer.value) return
  const layer = new pdfjsModule.TextLayer({
    textContentSource: textContent,
    container: textLayer.value,
    viewport,
  })
  textLayerRender = layer
  await layer.render()
  if (token !== renderToken || textLayerRender !== layer) {
    layer.cancel()
  }
}

async function load(): Promise<void> {
  renderToken += 1
  clearTextLayer()
  loading.value = true
  error.value = ''
  try {
    const pdfjs = await import('pdfjs-dist')
    pdfjsModule = pdfjs
    pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
    documentProxy?.destroy()
    documentProxy = await pdfjs.getDocument({ url: props.url, withCredentials: true }).promise
    pageCount.value = documentProxy.numPages
    currentPage.value = Math.min(pageCount.value, Math.max(1, props.page || 1))
    await nextTick()
    await renderPage()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'PDF 加载失败'
  } finally {
    loading.value = false
  }
}

async function move(delta: number): Promise<void> {
  const next = Math.min(pageCount.value, Math.max(1, currentPage.value + delta))
  if (next === currentPage.value) return
  currentPage.value = next
  loading.value = true
  try {
    await renderPage()
  } finally {
    loading.value = false
  }
}

watch(() => props.url, () => { void load() })
watch(() => props.page, (page) => {
  if (!documentProxy || !page || page === currentPage.value) return
  currentPage.value = Math.min(pageCount.value, Math.max(1, page))
  void renderPage()
})
onMounted(() => { void load() })
onBeforeUnmount(() => {
  renderToken += 1
  clearTextLayer()
  documentProxy?.destroy()
  documentProxy = null
  pdfjsModule = null
})
</script>

<template>
  <section class="pdfjs-reader" aria-label="PDF 原文阅读器">
    <header class="pdfjs-toolbar">
      <div class="pdfjs-pages">
        <button type="button" aria-label="上一页" :disabled="currentPage <= 1 || loading" @click="move(-1)"><ChevronLeft :size="16" /></button>
        <span>第 {{ currentPage }} / {{ pageCount || '—' }} 页</span>
        <button type="button" aria-label="下一页" :disabled="currentPage >= pageCount || loading" @click="move(1)"><ChevronRight :size="16" /></button>
      </div>
      <a :href="url" target="_blank" rel="noopener">下载或新窗口打开<ExternalLink :size="13" /></a>
    </header>
    <div v-if="error" class="pdfjs-state bad" role="alert">{{ error }}</div>
    <div v-else class="pdfjs-stage" :aria-busy="loading">
      <div v-if="loading" class="pdfjs-loading"><LoaderCircle :size="22" />正在渲染 PDF…</div>
      <div class="pdfjs-page">
        <canvas ref="canvas" />
        <div ref="textLayer" class="textLayer" aria-label="PDF 可选择文字层" />
        <span
          v-for="(box, index) in overlayBoxes"
          :key="index"
          class="pdfjs-highlight"
          :style="box"
          aria-label="证据高亮区域"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.pdfjs-reader { min-width: 0; }
.pdfjs-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.pdfjs-toolbar a, .pdfjs-pages { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; }
.pdfjs-pages button { display: grid; width: 36px; height: 36px; place-items: center; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
.pdfjs-pages button:disabled { opacity: .4; }
.pdfjs-stage { position: relative; min-height: 65vh; overflow: auto; border: 1px solid var(--line-soft); border-radius: 10px; background: #edf0f4; }
.pdfjs-page { position: relative; width: max-content; margin: 18px auto; box-shadow: 0 8px 28px rgba(34, 45, 63, .16); }
.pdfjs-page canvas { display: block; max-width: none; background: #fff; }
.pdfjs-highlight { position: absolute; z-index: 2; border: 2px solid #d98b00; border-radius: 3px; background: rgba(255, 214, 61, .34); box-shadow: 0 0 0 2px rgba(255,255,255,.72); pointer-events: none; }
.pdfjs-loading { position: sticky; top: 12px; z-index: 4; display: flex; width: max-content; align-items: center; gap: 7px; margin: 12px auto -48px; padding: 8px 12px; border-radius: 999px; background: rgba(255,255,255,.94); color: var(--ink-600); font-size: 12px; box-shadow: var(--sh-sm); }
.pdfjs-loading svg { animation: spin 1s linear infinite; }
.pdfjs-state { display: grid; min-height: 180px; place-items: center; border: 1px solid var(--line); border-radius: 10px; }
.pdfjs-state.bad { color: var(--bad); }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 600px) {
  .pdfjs-toolbar { align-items: flex-start; flex-direction: column; }
  .pdfjs-stage { min-height: 72vh; }
}
</style>

<style>
.pdfjs-page .textLayer {
  position: absolute;
  inset: 0;
  z-index: 1;
  overflow: clip;
  line-height: 1;
  text-align: initial;
  text-size-adjust: none;
  forced-color-adjust: none;
  transform-origin: 0 0;
  caret-color: CanvasText;
}

.pdfjs-page .textLayer :is(span, br) {
  position: absolute;
  color: transparent;
  white-space: pre;
  cursor: text;
  transform-origin: 0% 0%;
}

.pdfjs-page .textLayer > :not(.markedContent),
.pdfjs-page .textLayer .markedContent span:not(.markedContent) {
  z-index: 1;
}

.pdfjs-page .textLayer span.markedContent {
  top: 0;
  height: 0;
}

.pdfjs-page .textLayer span[role='img'] {
  cursor: default;
  user-select: none;
}

.pdfjs-page .textLayer ::selection {
  background: rgb(0 0 255 / 25%);
}

.pdfjs-page .textLayer br::selection {
  background: transparent;
}

.pdfjs-page .textLayer .endOfContent {
  position: absolute;
  inset: 100% 0 0;
  z-index: 0;
  display: block;
  cursor: default;
  user-select: none;
}

.pdfjs-page .textLayer.selecting .endOfContent {
  top: 0;
}
</style>
