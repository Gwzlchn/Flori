<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ExternalLink, ImageOff, X } from 'lucide-vue-next'
import type { AssetUrlResolver, DocumentFigure } from './types'
import { extractionReasons, extractionStatus } from './types'

const props = defineProps<{
  figure: DocumentFigure
  assetUrl: AssetUrlResolver
  sourceUrl?: string | null
}>()

const status = computed(() => extractionStatus(props.figure))
const reasons = computed(() => extractionReasons(props.figure))
const media = computed(() => props.figure.media ?? [])
const availableMedia = computed(() => media.value.filter((item) => item.artifact))
const preview = ref<{ src: string; alt: string } | null>(null)
const previewClose = ref<HTMLButtonElement | null>(null)
let previewTrigger: HTMLElement | null = null

function mediaAlt(role?: string | null, alt?: string | null): string {
  return alt?.trim() || [props.figure.label, role, props.figure.caption].filter(Boolean).join(' · ')
}

async function openPreview(artifact: string, alt: string, event: MouseEvent): Promise<void> {
  previewTrigger = event.currentTarget as HTMLElement
  preview.value = { src: props.assetUrl(artifact), alt }
  await nextTick()
  previewClose.value?.focus()
}

function closePreview(): void {
  preview.value = null
  void nextTick(() => previewTrigger?.focus())
}

function trapPreviewFocus(event: KeyboardEvent): void {
  event.preventDefault()
  previewClose.value?.focus()
}
</script>

<template>
  <article class="visual-card figure-card" tabindex="-1" :aria-labelledby="`${figure.figure_id}-title`">
    <header class="visual-card-head">
      <div>
        <h3 :id="`${figure.figure_id}-title`">{{ figure.label }}</h3>
        <p v-if="figure.caption">{{ figure.caption }}</p>
      </div>
      <span class="quality-state" :class="`quality-${status}`">{{ status }}</span>
    </header>

    <div v-if="availableMedia.length" class="figure-media" :class="{ multiple: availableMedia.length > 1 }">
      <figure v-for="item in availableMedia" :key="item.media_id" class="figure-panel">
        <button
          type="button"
          class="figure-zoom"
          :aria-label="`放大查看 ${mediaAlt(item.role, item.alt)}`"
          @click="openPreview(item.artifact!, mediaAlt(item.role, item.alt), $event)"
        >
          <img :src="assetUrl(item.artifact!)" :alt="mediaAlt(item.role, item.alt)" loading="lazy" />
        </button>
        <figcaption v-if="item.role">{{ item.role }}</figcaption>
      </figure>
    </div>
    <div v-else class="visual-missing" role="status"><ImageOff :size="18" />原始图像不可用</div>

    <p v-if="reasons.length" class="quality-reasons" role="status">{{ reasons.join('；') }}</p>
    <a v-if="sourceUrl" class="source-jump" :href="sourceUrl"><ExternalLink :size="13" />查看原文位置</a>

    <Teleport to="body">
      <div v-if="preview" class="figure-lightbox" role="dialog" aria-modal="true" aria-label="图像预览"
        @click.self="closePreview" @keydown.esc="closePreview" @keydown.tab="trapPreviewFocus">
        <button ref="previewClose" type="button" class="figure-lightbox-close" aria-label="关闭图像预览" @click="closePreview"><X :size="20" /></button>
        <img :src="preview.src" :alt="preview.alt" />
      </div>
    </Teleport>
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
.figure-media { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; }
.figure-media.multiple { grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr)); }
.figure-panel { min-width: 0; }
.figure-zoom { display: grid; width: 100%; min-height: 120px; place-items: center; border: 1px solid var(--line-soft); border-radius: var(--r-sm); background: var(--raised); overflow: hidden; }
.figure-zoom:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
.figure-panel img { display: block; max-width: 100%; max-height: 72vh; width: auto; height: auto; object-fit: contain; }
.figure-panel figcaption { margin-top: 5px; color: var(--ink-500); font-size: 11.5px; text-align: center; }
.visual-missing { display: flex; min-height: 120px; align-items: center; justify-content: center; gap: 7px; border: 1px dashed var(--line); border-radius: var(--r-sm); color: var(--ink-500); background: var(--raised); }
.quality-reasons { margin-top: 9px; color: var(--warn); font-size: 12px; }
.source-jump { display: inline-flex; align-items: center; gap: 4px; margin-top: 10px; color: var(--brand-700); font-size: 12px; }
.figure-lightbox { position: fixed; inset: 0; z-index: 1100; display: grid; place-items: center; padding: 4vh 4vw; background: rgba(0, 0, 0, .84); }
.figure-lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; }
.figure-lightbox-close { position: absolute; top: 14px; right: 18px; display: grid; width: 44px; height: 44px; place-items: center; border-radius: 50%; background: rgba(255, 255, 255, .14); color: #fff; }

@media (max-width: 600px) {
  .visual-card { padding: 13px; }
  .figure-media.multiple { grid-template-columns: minmax(0, 1fr); }
}
</style>
