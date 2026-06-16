<script setup lang="ts">
import { computed, watch } from 'vue'
import MarkdownIt from 'markdown-it'

const props = defineProps<{ content: string; jobId: string }>()
const emit = defineEmits<{ headings: [{ id: string; text: string; level: number }[]] }>()

const md = new MarkdownIt({ html: false, linkify: true, typographer: true })

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

// 时间戳渲染为普通等宽文本：当前没有播放器/跳转能力，不做可点击外观以免误导。
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

const rendered = computed(() => {
  let headingIdx = 0
  let html = md.render(props.content, { jobId: props.jobId })

  html = html.replace(/<h([2-3])>/g, (_match: string, level: string) => {
    const id = `heading-${headingIdx++}`
    return `<h${level} id="${id}">`
  })

  // OCR 仅在显示时折叠：把 `> OCR：…` 渲染出的引用块包成默认收起的 <details>，
  // 原文仍保留在笔记里，只是阅读时不喧宾夺主。
  html = html.replace(
    /<blockquote>\s*<p>OCR：([\s\S]*?)<\/p>\s*<\/blockquote>/g,
    (_m: string, body: string) =>
      `<details class="ocr-fold"><summary>OCR</summary><div class="ocr-body">${body}</div></details>`,
  )

  return html
})

watch(rendered, (html) => {
  const headings: { id: string; text: string; level: number }[] = []
  const headingRegex = /<h([2-3])\s+id="([^"]*)">(.*?)<\/h[2-3]>/g
  let match
  while ((match = headingRegex.exec(html)) !== null) {
    headings.push({ level: parseInt(match[1]), id: match[2], text: match[3].replace(/<[^>]*>/g, '') })
  }
  emit('headings', headings)
}, { immediate: true })
</script>

<template>
  <div class="prose prose-sm max-w-none prose-headings:scroll-mt-20" v-html="rendered" />
</template>

<style>
.prose img { max-width: 100%; border-radius: 0.5rem; }
.prose .timestamp-mark { text-decoration: none; }
.prose details.ocr-fold { margin: 0.2rem 0 0.7rem; }
.prose details.ocr-fold > summary { cursor: pointer; font-size: 0.72rem; color: #9ca3af; user-select: none; }
.prose details.ocr-fold > summary::before { content: "🔎 "; }
.prose details.ocr-fold .ocr-body { font-size: 0.78rem; color: #6b7280; background: #f9fafb; border-radius: 0.375rem; padding: 0.35rem 0.6rem; margin-top: 0.25rem; }
.prose details.ocr-fold .ocr-body p { margin: 0; }
</style>
