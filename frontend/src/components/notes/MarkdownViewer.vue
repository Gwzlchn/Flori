<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
// KaTeX 数学渲染:论文原文/译文含 $…$/$$…$$(LaTeX,arxiv HTML alttext 与 PDF 直喂译文都产)。
// html:false 不影响插件——katex 经 token 渲染器输出,非原始 HTML 透传。
import katexPlugin from '@vscode/markdown-it-katex'
import 'katex/dist/katex.min.css'

// terms/domain 用于笔记内联可点:正文里命中的已接受术语包成链接 → 该领域术语详情。
// 不传则不做术语链接(其它调用方无需改动)。
const props = defineProps<{ content: string; jobId: string; terms?: string[]; domain?: string }>()
const emit = defineEmits<{
  headings: [{ id: string; text: string; level: number }[]]
  // pdf-only 译文的图占位链接(#pdf-page=N):父组件切「原文」tab 并让 PDF iframe 跳该页(原生渲染保真)
  pdfPage: [number]
}>()

const router = useRouter()
const md = new MarkdownIt({ html: false, linkify: true, typographer: true })
md.use(katexPlugin, { throwOnError: false })   // 非法 LaTeX 红字降级,不炸整页渲染

// 术语链接状态(在 rendered 计算里按当前 props 更新;ruler 闭包读取)。
const termLink: { set: Set<string>; re: RegExp | null; linked: Set<string> } = {
  set: new Set(), re: null, linked: new Set(),
}
function escAttr(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
function buildTermRegex(terms: string[]): RegExp | null {
  const valid = [...new Set(terms.filter(t => t && t.length >= 2))].sort((a, b) => b.length - a.length)
  termLink.set = new Set(valid)
  if (!valid.length) return null
  const esc = valid.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  return new RegExp('(' + esc.join('|') + ')', 'g')
}

const defaultImageRule = md.renderer.rules.image!
md.renderer.rules.image = (tokens: any, idx: any, options: any, env: any, self: any) => {
  const token = tokens[idx]
  const src = token.attrGet('src') || ''
  if (src.startsWith('assets/') || src.startsWith('./assets/')) {
    const filename = src.replace(/^\.?\/?(assets\/)/, '')
    token.attrSet('src', `/api/jobs/${env.jobId}/assets/${filename}`)
    token.attrSet('loading', 'lazy')
    token.attrSet('class', 'rounded-lg max-w-full my-2')
  }
  return defaultImageRule(tokens, idx, options, env, self)
}

// 时间戳渲染为普通等宽文本:当前没有播放器/跳转能力,不做可点击外观以免误导。
md.core.ruler.after('inline', 'timestamp_marks', (state: any) => {
  for (const blockToken of state.tokens) {
    if (blockToken.type !== 'inline' || !blockToken.children) continue
    const newChildren: any[] = []
    for (const child of blockToken.children) {
      if (child.type === 'text') {
        const text = child.content
        const parts = text.split(/(\[\d{1,2}:\d{2}(?::\d{2})?\])/g)
        if (parts.length === 1) {
          newChildren.push(child)
          continue
        }
        for (const part of parts) {
          const match = part.match(/^\[(\d{1,2}:\d{2}(?::\d{2})?)\]$/)
          if (match) {
            const open = new state.Token('html_inline', '', 0)
            const ts = match[1]
            open.content = `<span class="timestamp-mark font-mono text-sm text-gray-500">[${ts}]</span>`
            newChildren.push(open)
          } else if (part) {
            const t = new state.Token('text', '', 0)
            t.content = part
            newChildren.push(t)
          }
        }
      } else {
        newChildren.push(child)
      }
    }
    blockToken.children = newChildren
  }
})

// 正文命中已接受术语 → 包成可点链接(仅 text 节点、不在链接/代码内、每词仅首次出现)。
md.core.ruler.after('timestamp_marks', 'term_links', (state: any) => {
  if (!termLink.re) return
  for (const blockToken of state.tokens) {
    if (blockToken.type !== 'inline' || !blockToken.children) continue
    let linkDepth = 0
    const newChildren: any[] = []
    for (const child of blockToken.children) {
      if (child.type === 'link_open') { linkDepth++; newChildren.push(child); continue }
      if (child.type === 'link_close') { linkDepth = Math.max(0, linkDepth - 1); newChildren.push(child); continue }
      if (child.type !== 'text' || linkDepth > 0) { newChildren.push(child); continue }
      termLink.re.lastIndex = 0
      const parts = child.content.split(termLink.re)
      if (parts.length === 1) { newChildren.push(child); continue }
      for (const part of parts) {
        if (!part) continue
        if (termLink.set.has(part) && !termLink.linked.has(part)) {
          termLink.linked.add(part)
          const a = new state.Token('html_inline', '', 0)
          a.content = `<a class="term-link" data-term="${escAttr(part)}">${escAttr(part)}</a>`
          newChildren.push(a)
        } else {
          const t = new state.Token('text', '', 0)
          t.content = part
          newChildren.push(t)
        }
      }
    }
    blockToken.children = newChildren
  }
})

// 渲染 + 标题 id 注入 + TOC 提取一次完成:标题 id / 目录通过 DOM 遍历(替代易碎的 HTML 正则)。
const renderedDoc = computed(() => {
  // 每次渲染前按当前 props 重建术语正则 + 清空"已链接"集合(每词仅首次出现)。
  termLink.re = buildTermRegex(props.terms || [])
  termLink.linked = new Set()
  let html = md.render(props.content, { jobId: props.jobId })

  // OCR 仅在显示时折叠:把 `> OCR：…` 渲染出的引用块包成默认收起的 <details>,
  // 原文仍保留在笔记里,只是阅读时不喧宾夺主。
  html = html.replace(
    /<blockquote>\s*<p>OCR：([\s\S]*?)<\/p>\s*<\/blockquote>/g,
    (_m: string, body: string) =>
      `<details class="ocr-fold"><summary>OCR</summary><div class="ocr-body">${body}</div></details>`,
  )

  // markdown-it html:false 输入受控,DOMParser 安全:遍历 h2/h3 注入稳定 id 并取 textContent 作目录。
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const headings: { id: string; text: string; level: number }[] = []
  let i = 0
  doc.querySelectorAll('h2, h3').forEach((el) => {
    const id = `heading-${i++}`
    el.id = id
    headings.push({ id, text: el.textContent || '', level: el.tagName === 'H3' ? 3 : 2 })
  })
  return { html: doc.body.innerHTML, headings }
})

const rendered = computed(() => renderedDoc.value.html)

watch(() => renderedDoc.value.headings, (hs) => emit('headings', hs), { immediate: true })

// 术语链接走 SPA 跳转(v-html 内的 <a> 不被 vue-router 接管,用事件委托);
// 正文图片点击开 lightbox 放大(同一委托)。
function onClick(e: MouseEvent) {
  const t = e.target as HTMLElement
  const pdfA = t?.closest?.('a[href^="#pdf-page="]') as HTMLAnchorElement | null
  if (pdfA) {
    e.preventDefault()
    const n = parseInt(pdfA.getAttribute('href')!.slice('#pdf-page='.length), 10)
    if (n > 0) emit('pdfPage', n)
    return
  }
  const a = t?.closest?.('.term-link') as HTMLElement | null
  if (a) {
    e.preventDefault()
    const term = a.getAttribute('data-term')
    if (term && props.domain) {
      router.push(`/kb/${encodeURIComponent(props.domain)}/concepts/${encodeURIComponent(term)}`)
    }
    return
  }
  if (t instanceof HTMLImageElement && !t.closest('a')) {  // 链接内的图仍走链接
    lightboxSrc.value = t.currentSrc || t.src
  }
}

// 图片 lightbox:点图放大,X/点遮罩/Esc 关闭。
const lightboxSrc = ref('')
function closeLightbox() { lightboxSrc.value = '' }
function onEsc(e: KeyboardEvent) { if (e.key === 'Escape') closeLightbox() }
watch(lightboxSrc, (v) => {
  if (v) document.addEventListener('keydown', onEsc)
  else document.removeEventListener('keydown', onEsc)
})
onBeforeUnmount(() => document.removeEventListener('keydown', onEsc))
</script>

<template>
  <div class="prose prose-sm max-w-none prose-headings:scroll-mt-20" v-html="rendered" @click="onClick" />
  <Teleport to="body">
    <div v-if="lightboxSrc" class="lightbox" @click.self="closeLightbox">
      <button class="lightbox-x" aria-label="关闭" @click="closeLightbox">×</button>
      <img :src="lightboxSrc" alt="">
    </div>
  </Teleport>
</template>

<style>
/* 默认收敛尺寸:宽随列、高最多 70vh(超高竖图不占满屏,点开 lightbox 看全) */
.prose img { max-width: 100%; max-height: 70vh; width: auto; border-radius: 0.5rem; cursor: zoom-in; }
/* 宽表在自身内横滚,不许撑破内容列(移动端 606px 表 vs 358px 列实测破页) */
.prose table { display: block; max-width: 100%; overflow-x: auto; }
.prose pre { overflow-x: auto; }
/* 图片 lightbox(Teleport 到 body,样式须非 scoped) */
.lightbox {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0, 0, 0, 0.82);
  display: flex; align-items: center; justify-content: center;
  padding: 4vh 4vw;
}
.lightbox img {
  max-width: 100%; max-height: 100%;
  border-radius: 6px; box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
}
.lightbox-x {
  position: absolute; top: 14px; right: 18px;
  width: 40px; height: 40px; border-radius: 50%; border: none;
  background: rgba(255, 255, 255, 0.14); color: #fff;
  font-size: 26px; line-height: 1; cursor: pointer;
}
.lightbox-x:hover { background: rgba(255, 255, 255, 0.28); }
/* 代码统一浅灰底黑字等宽,字号随正文:prose-sm 默认 0.857em 太小、pre 深底反白不合阅读习惯 */
.prose pre {
  background: #f3f4f6;
  color: #111827;
  font-size: 1em;
  line-height: 1.6;
  border: 1px solid #e5e7eb;
}
.prose pre code { background: transparent; color: inherit; font-size: 1em; padding: 0; }
.prose :not(pre) > code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 1em;
  font-weight: 500;
  color: #111827;
  background: #f3f4f6;
  padding: 0.08em 0.35em;
  border-radius: 0.25rem;
}
/* 去 typography 给行内 code 加的反引号伪元素(与正文引号叠加很吵) */
.prose :not(pre) > code::before,
.prose :not(pre) > code::after { content: none; }
.prose .timestamp-mark { text-decoration: none; }
.prose .term-link { color: #2563eb; cursor: pointer; text-decoration: none; border-bottom: 1px dashed #93c5fd; }
.prose .term-link:hover { background: #eff6ff; border-bottom-style: solid; }
.prose details.ocr-fold { margin: 0.2rem 0 0.7rem; }
.prose details.ocr-fold > summary { cursor: pointer; font-size: 0.72rem; color: #9ca3af; user-select: none; }
.prose details.ocr-fold > summary::before { content: "🔎 "; }
.prose details.ocr-fold .ocr-body { font-size: 0.78rem; color: #6b7280; background: #f9fafb; border-radius: 0.375rem; padding: 0.35rem 0.6rem; margin-top: 0.25rem; }
.prose details.ocr-fold .ocr-body p { margin: 0; }
</style>
