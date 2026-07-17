<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { LoaderCircle } from 'lucide-vue-next'
import type { PDFDocumentProxy, PDFPageProxy, TextLayer as PDFTextLayer } from 'pdfjs-dist'

interface ViewportSize {
  width: number
  height: number
  scale: number
}

const props = defineProps<{
  documentProxy: PDFDocumentProxy
  pageNumber: number
  defaultViewport: ViewportSize
  priority: boolean
  bboxes: [number, number, number, number][]
}>()

const shell = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const textLayer = ref<HTMLDivElement | null>(null)
const viewport = ref<ViewportSize>({ ...props.defaultViewport })
const status = ref<'idle' | 'rendering' | 'ready' | 'error'>('idle')
const error = ref('')
let observer: IntersectionObserver | null = null
let renderTask: ReturnType<PDFPageProxy['render']> | null = null
let textLayerRender: PDFTextLayer | null = null
let renderGeneration = 0
let nearViewport = false

const pageStyle = computed(() => ({
  width: `${Math.ceil(viewport.value.width)}px`,
  height: `${Math.ceil(viewport.value.height)}px`,
}))

const percent = (value: number) => `${Number(value.toFixed(6))}%`
const overlayBoxes = computed(() => props.bboxes.map((bbox) => ({
  left: percent(bbox[0] * viewport.value.scale / viewport.value.width * 100),
  top: percent(bbox[1] * viewport.value.scale / viewport.value.height * 100),
  width: percent((bbox[2] - bbox[0]) * viewport.value.scale / viewport.value.width * 100),
  height: percent((bbox[3] - bbox[1]) * viewport.value.scale / viewport.value.height * 100),
})))

function releasePage(): void {
  renderGeneration += 1
  renderTask?.cancel()
  renderTask = null
  textLayerRender?.cancel()
  textLayerRender = null
  textLayer.value?.replaceChildren()
  if (canvas.value) {
    canvas.value.width = 1
    canvas.value.height = 1
  }
  error.value = ''
  status.value = 'idle'
}

async function renderPage(): Promise<void> {
  if (status.value === 'rendering' || status.value === 'ready') return
  const generation = ++renderGeneration
  status.value = 'rendering'
  error.value = ''
  try {
    const page = await props.documentProxy.getPage(props.pageNumber)
    if (generation !== renderGeneration || !canvas.value || !textLayer.value) return
    const nextViewport = page.getViewport({ scale: props.defaultViewport.scale })
    viewport.value = {
      width: nextViewport.width,
      height: nextViewport.height,
      scale: nextViewport.scale,
    }
    const context = canvas.value.getContext('2d')
    if (!context) throw new Error('canvas context unavailable')
    canvas.value.width = Math.ceil(nextViewport.width)
    canvas.value.height = Math.ceil(nextViewport.height)
    renderTask = page.render({ canvasContext: context, viewport: nextViewport })
    const textContentPromise = page.getTextContent()
    await renderTask.promise
    const textContent = await textContentPromise
    if (generation !== renderGeneration || !textLayer.value) return
    const pdfjs = await import('pdfjs-dist')
    if (generation !== renderGeneration || !textLayer.value) return
    const layer = new pdfjs.TextLayer({
      textContentSource: textContent,
      container: textLayer.value,
      viewport: nextViewport,
    })
    textLayerRender = layer
    await layer.render()
    if (generation !== renderGeneration || textLayerRender !== layer) {
      layer.cancel()
      return
    }
    status.value = 'ready'
  } catch (reason) {
    if (generation !== renderGeneration) return
    error.value = reason instanceof Error ? reason.message : 'PDF 页面渲染失败'
    status.value = 'error'
  }
}

watch(() => props.priority, (priority) => {
  if (priority) void renderPage()
  else if (!nearViewport) releasePage()
})

onMounted(() => {
  if (typeof IntersectionObserver === 'undefined') {
    void renderPage()
    return
  }
  observer = new IntersectionObserver((entries) => {
    nearViewport = entries.some(entry => entry.isIntersecting)
    if (nearViewport) void renderPage()
    else if (!props.priority) releasePage()
  }, {
    root: shell.value?.closest('.pdfjs-stage') || null,
    rootMargin: '1200px 0px',
  })
  if (shell.value) observer.observe(shell.value)
  if (props.priority) void renderPage()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  releasePage()
})
</script>

<template>
  <article ref="shell" class="pdfjs-page-shell" :data-page-number="pageNumber" :aria-label="`PDF 第 ${pageNumber} 页`">
    <div class="pdfjs-page" :style="pageStyle" :aria-busy="status === 'rendering'">
      <canvas ref="canvas" />
      <div ref="textLayer" class="textLayer" aria-label="PDF 可选择文字层" />
      <span
        v-for="(box, index) in overlayBoxes"
        :key="index"
        class="pdfjs-highlight"
        :style="box"
        aria-label="证据高亮区域"
      />
      <div v-if="status === 'rendering'" class="pdfjs-page-state"><LoaderCircle :size="20" />正在渲染…</div>
      <div v-else-if="status === 'error'" class="pdfjs-page-state bad" role="alert">{{ error }}</div>
    </div>
  </article>
</template>

<style scoped>
.pdfjs-page-shell { width: max-content; max-width: none; margin: 0 auto 22px; }
.pdfjs-page { position: relative; overflow: hidden; background: #fff; box-shadow: 0 8px 28px rgba(34, 45, 63, .16); }
.pdfjs-page canvas { display: block; max-width: none; background: #fff; }
.pdfjs-highlight { position: absolute; z-index: 2; border: 2px solid #d98b00; border-radius: 3px; background: rgba(255, 214, 61, .34); box-shadow: 0 0 0 2px rgba(255,255,255,.72); pointer-events: none; }
.pdfjs-page-state { position: absolute; inset: 0; z-index: 3; display: flex; align-items: center; justify-content: center; gap: 7px; color: var(--ink-500); font-size: 12px; background: rgba(255,255,255,.72); }
.pdfjs-page-state svg { animation: spin 1s linear infinite; }
.pdfjs-page-state.bad { color: var(--bad); }
@keyframes spin { to { transform: rotate(360deg); } }
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

.pdfjs-page .textLayer ::selection { background: rgb(0 0 255 / 25%); }
.pdfjs-page .textLayer br::selection { background: transparent; }
.pdfjs-page .textLayer .endOfContent { position: absolute; inset: 100% 0 0; z-index: 0; display: block; cursor: default; user-select: none; }
.pdfjs-page .textLayer.selecting .endOfContent { top: 0; }
</style>
