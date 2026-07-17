<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ExternalLink, LoaderCircle } from 'lucide-vue-next'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import DocumentPdfPage from './DocumentPdfPage.vue'

const props = defineProps<{
  url: string
  page?: number
  bboxes?: [number, number, number, number][]
}>()

const stage = ref<HTMLElement | null>(null)
const documentProxy = shallowRef<PDFDocumentProxy | null>(null)
const loading = ref(true)
const error = ref('')
const currentPage = ref(Math.max(1, props.page || 1))
const pageCount = ref(0)
const pageNumbers = ref<number[]>([])
const defaultViewport = ref({ width: 960, height: 1280, scale: 1.6 })
let loadToken = 0
let scrollFrame = 0

const evidencePage = computed(() => Math.min(pageCount.value || 1, Math.max(1, props.page || 1)))

function scrollToPage(page: number, behavior: ScrollBehavior): void {
  const target = stage.value?.querySelector<HTMLElement>(`[data-page-number="${page}"]`)
  if (!stage.value || !target) return
  currentPage.value = page
  stage.value.scrollTo({ top: Math.max(0, target.offsetTop - 12), behavior })
}

function updateCurrentPage(): void {
  scrollFrame = 0
  if (!stage.value) return
  const stageRect = stage.value.getBoundingClientRect()
  const anchor = stageRect.top + Math.min(72, stageRect.height * .15)
  let bestPage = currentPage.value
  let bestDistance = Number.POSITIVE_INFINITY
  for (const element of stage.value.querySelectorAll<HTMLElement>('[data-page-number]')) {
    const rect = element.getBoundingClientRect()
    const containsAnchor = rect.top <= anchor && rect.bottom >= anchor
    const distance = containsAnchor ? 0 : Math.min(Math.abs(rect.top - anchor), Math.abs(rect.bottom - anchor))
    if (distance < bestDistance) {
      bestDistance = distance
      bestPage = Number(element.dataset.pageNumber) || bestPage
    }
    if (containsAnchor) break
  }
  currentPage.value = bestPage
}

function onScroll(): void {
  if (scrollFrame) return
  scrollFrame = requestAnimationFrame(updateCurrentPage)
}

async function load(): Promise<void> {
  const token = ++loadToken
  const previous = documentProxy.value
  documentProxy.value = null
  pageNumbers.value = []
  pageCount.value = 0
  loading.value = true
  error.value = ''
  previous?.destroy()
  try {
    const pdfjs = await import('pdfjs-dist')
    pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
    const proxy = await pdfjs.getDocument({ url: props.url, withCredentials: true }).promise
    if (token !== loadToken) {
      proxy.destroy()
      return
    }
    documentProxy.value = proxy
    pageCount.value = proxy.numPages
    const firstPage = await proxy.getPage(1)
    const viewport = firstPage.getViewport({ scale: 1.6 })
    defaultViewport.value = { width: viewport.width, height: viewport.height, scale: viewport.scale }
    pageNumbers.value = Array.from({ length: proxy.numPages }, (_, index) => index + 1)
    currentPage.value = evidencePage.value
    await nextTick()
    scrollToPage(evidencePage.value, 'auto')
  } catch (reason) {
    if (token === loadToken) error.value = reason instanceof Error ? reason.message : 'PDF 加载失败'
  } finally {
    if (token === loadToken) loading.value = false
  }
}

watch(() => props.url, () => { void load() })
watch(() => props.page, async () => {
  if (!documentProxy.value) return
  await nextTick()
  scrollToPage(evidencePage.value, 'smooth')
})

onMounted(() => { void load() })
onBeforeUnmount(() => {
  loadToken += 1
  if (scrollFrame) cancelAnimationFrame(scrollFrame)
  documentProxy.value?.destroy()
  documentProxy.value = null
})
</script>

<template>
  <section class="pdfjs-reader" aria-label="PDF 原文阅读器">
    <header class="pdfjs-toolbar">
      <span class="pdfjs-pages" aria-live="polite">第 {{ currentPage }} / {{ pageCount || '—' }} 页</span>
      <a :href="url" target="_blank" rel="noopener">新窗口打开<ExternalLink :size="13" /></a>
    </header>
    <div v-if="error" class="pdfjs-state bad" role="alert">{{ error }}</div>
    <div v-else ref="stage" class="pdfjs-stage" :aria-busy="loading" @scroll.passive="onScroll">
      <div v-if="loading" class="pdfjs-loading"><LoaderCircle :size="22" />正在加载 PDF…</div>
      <div v-if="documentProxy" class="pdfjs-pages-flow">
        <DocumentPdfPage
          v-for="pageNumber in pageNumbers"
          :key="`${url}:${pageNumber}`"
          :document-proxy="documentProxy"
          :page-number="pageNumber"
          :default-viewport="defaultViewport"
          :priority="pageNumber === evidencePage"
          :bboxes="pageNumber === evidencePage ? (bboxes || []) : []"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.pdfjs-reader { min-width: 0; }
.pdfjs-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.pdfjs-toolbar a, .pdfjs-pages { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; }
.pdfjs-stage { position: relative; height: clamp(560px, 78vh, 1200px); overflow: auto; overscroll-behavior: contain; border: 1px solid var(--line-soft); border-radius: 10px; background: #edf0f4; scroll-behavior: smooth; }
.pdfjs-pages-flow { width: max-content; min-width: 100%; padding: 18px 18px 1px; }
.pdfjs-loading { position: sticky; top: 12px; z-index: 4; display: flex; width: max-content; align-items: center; gap: 7px; margin: 12px auto -48px; padding: 8px 12px; border-radius: 999px; background: rgba(255,255,255,.94); color: var(--ink-600); font-size: 12px; box-shadow: var(--sh-sm); }
.pdfjs-loading svg { animation: spin 1s linear infinite; }
.pdfjs-state { display: grid; min-height: 180px; place-items: center; border: 1px solid var(--line); border-radius: 10px; }
.pdfjs-state.bad { color: var(--bad); }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 600px) {
  .pdfjs-stage { height: 72vh; min-height: 480px; }
  .pdfjs-pages-flow { padding: 12px 12px 1px; }
}
</style>
