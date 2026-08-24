<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../api/client";
import UiIcon from "./UiIcon.vue";

type VisualMeta = components["schemas"]["DocumentFigure"] | components["schemas"]["DocumentTable"];
interface VisualCard {
  artifact: components["schemas"]["ArtifactView"] | undefined;
  category: "Figure" | "Table";
  meta: VisualMeta;
  sequence: number;
}

const props = defineProps<{
  artifacts: components["schemas"]["ArtifactView"][];
  structure: components["schemas"]["DocumentStructure"] | undefined;
  projections: components["schemas"]["HtmlVisualProjection"][] | undefined;
  fileUrls: ReadonlyMap<string, string>;
  activeVisualId: string;
}>();
const emit = defineEmits<{
  locate: [id: string, page: number, bbox: components["schemas"]["PdfRect"], label: string];
  locateHtml: [id: string, anchor: string, label: string];
}>();
const lightbox = ref<HTMLDialogElement>();
const previewCard = ref<VisualCard>();

const cards = computed<VisualCard[]>(() => {
  const result: VisualCard[] = [];
  for (const [index, meta] of (props.structure?.figures ?? []).entries()) {
    result.push({
      artifact: props.artifacts.find((artifact) => artifact.kind === "figure" && artifact.name === meta.artifact_name),
      category: "Figure",
      meta,
      sequence: index + 1,
    });
  }
  for (const [index, meta] of (props.structure?.tables ?? []).entries()) {
    result.push({
      artifact: props.artifacts.find((artifact) => artifact.kind === "table_region" && artifact.name === meta.artifact_name),
      category: "Table",
      meta,
      sequence: index + 1,
    });
  }
  return result;
});
function projectionOf(card: VisualCard): components["schemas"]["HtmlVisualProjection"] | undefined {
  const kind = card.category === "Figure" ? "figure" : "table_region";
  return props.projections?.find((item) => item.kind === kind && item.artifact_name === card.meta.artifact_name);
}
function activate(card: VisualCard, scroll = false): void {
  if (scroll) document.getElementById(`visual-${card.meta.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  const projection = projectionOf(card);
  if (projection?.status === "verified" && projection.html_anchor) {
    emit("locateHtml", card.meta.id, projection.html_anchor, card.meta.caption);
  } else emit("locate", card.meta.id, card.meta.page, card.meta.bbox, card.meta.caption);
}
function selectOffset(offset: number): void {
  const index = Math.max(0, cards.value.findIndex((card) => card.meta.id === props.activeVisualId));
  const target = cards.value[(index + offset + cards.value.length) % cards.value.length];
  if (target) activate(target, true);
}
function preview(card: VisualCard): void {
  previewCard.value = card;
  lightbox.value?.showModal();
}
function sourceLabel(card: VisualCard): string {
  const projection = projectionOf(card);
  if (projection?.status === "verified") return "HTML 原文";
  if (projection?.status === "caption_ambiguous") return "HTML caption 不唯一，回退 PDF";
  if (projection?.status === "anchor_missing") return "HTML anchor 缺失，回退 PDF";
  return "PDF 原文";
}
function coordinate(value: number): string { return value.toFixed(1); }
watch(() => [props.activeVisualId, cards.value, props.projections] as const, () => {
  const card = cards.value.find((item) => item.meta.id === props.activeVisualId);
  if (card) activate(card);
}, { immediate: true, deep: true });
</script>

<template>
  <div
    v-if="cards.length"
    class="visual-workspace"
  >
    <nav
      class="visual-catalog"
      aria-label="图表目录"
    >
      <p class="eyebrow">
        图表目录
      </p>
      <div class="visual-nav">
        <button
          type="button"
          @click="selectOffset(-1)"
        >
          上一项
        </button>
        <button
          type="button"
          @click="selectOffset(1)"
        >
          下一项
        </button>
      </div>
      <button
        v-for="card in cards"
        :key="card.meta.id"
        type="button"
        :aria-current="activeVisualId === card.meta.id ? 'true' : undefined"
        @click="activate(card, true)"
      >
        <span>{{ card.category }} {{ card.sequence }} · {{ card.meta.id }}</span>
        <small>{{ card.meta.caption }}</small>
      </button>
    </nav>
    <div class="visual-cards">
      <article
        v-for="card in cards"
        :id="`visual-${card.meta.id}`"
        :key="card.meta.id"
        class="visual-card"
        :class="{ 'is-active': activeVisualId === card.meta.id }"
      >
        <header>
          <div>
            <p class="eyebrow">
              {{ card.category }} {{ card.sequence }} · {{ card.meta.id }}
            </p>
            <h3>{{ card.meta.caption }}</h3>
          </div>
          <button
            type="button"
            class="btn secondary compact"
            @click="activate(card)"
          >
            <UiIcon
              name="external"
              :size="13"
            />{{ sourceLabel(card) }}
          </button>
        </header>
        <button
          v-if="card.artifact && fileUrls.get(card.artifact.artifact_id)"
          type="button"
          class="visual-image"
          :aria-label="`放大查看 ${card.meta.caption}`"
          @click="preview(card)"
        >
          <img
            :src="fileUrls.get(card.artifact.artifact_id)"
            :alt="card.meta.caption"
          >
        </button>
        <p class="visual-caption">
          {{ card.meta.caption }}
        </p>
        <p
          v-if="card.category === 'Table' && 'text' in card.meta"
          class="visual-text"
        >
          {{ card.meta.text }}
        </p>
        <p
          v-if="!card.artifact"
          class="error-text"
        >
          提取图片缺失；保留页码与 bbox，回原文核对。
        </p>
        <dl>
          <div><dt>页码</dt><dd>{{ card.meta.page }}</dd></div>
          <div><dt>区域</dt><dd>{{ coordinate(card.meta.bbox.x1) }}, {{ coordinate(card.meta.bbox.y1) }} → {{ coordinate(card.meta.bbox.x2) }}, {{ coordinate(card.meta.bbox.y2) }}</dd></div>
        </dl>
      </article>
    </div>
    <dialog
      ref="lightbox"
      class="visual-lightbox"
      @click.self="lightbox?.close()"
    >
      <button
        type="button"
        aria-label="关闭图表预览"
        @click="lightbox?.close()"
      >
        ×
      </button>
      <img
        v-if="previewCard?.artifact && fileUrls.get(previewCard.artifact.artifact_id)"
        :src="fileUrls.get(previewCard.artifact.artifact_id)"
        :alt="previewCard.meta.caption"
      >
      <p>{{ previewCard?.meta.caption }}</p>
    </dialog>
  </div>
  <p
    v-else
    class="empty-state"
  >
    没有提取到 Figure 或 Table 区域。
  </p>
</template>

<style scoped>
.visual-nav { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
.visual-nav button { justify-content: center; color: var(--muted); border: 1px solid var(--line); background: white; }
.visual-catalog button[aria-current="true"], .visual-card.is-active { border-color: #a8cef0; background: var(--brand-soft); }
.visual-text { max-height: 12em; overflow: auto; color: var(--muted); white-space: pre-wrap; }
.visual-image { width: 100%; padding: 0; border: 0; background: transparent; cursor: zoom-in; }
.visual-lightbox { width: min(1080px, 92vw); max-height: 90vh; padding: 38px 18px 18px; border: 0; border-radius: 8px; background: white; box-shadow: 0 20px 80px rgb(0 0 0 / 32%); }
.visual-lightbox::backdrop { background: rgb(20 20 20 / 66%); }.visual-lightbox > button { position: absolute; top: 8px; right: 10px; border: 0; background: transparent; font-size: 24px; cursor: pointer; }
.visual-lightbox img { display: block; max-width: 100%; max-height: 74vh; margin: auto; object-fit: contain; }.visual-lightbox p { margin: 10px auto 0; max-width: 82ch; }
</style>
