<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import UiIcon from "./UiIcon.vue";

interface VisualMeta {
  artifactName: string;
  bbox: components["schemas"]["PdfRect"];
  caption: string;
  id: string;
  page: number;
}
interface VisualCard {
  artifact: components["schemas"]["ArtifactView"];
  meta: VisualMeta | undefined;
  sequence: number;
}

const props = defineProps<{
  artifacts: components["schemas"]["ArtifactView"][];
  documentText: string | undefined;
  fileUrls: ReadonlyMap<string, string>;
}>();
const emit = defineEmits<{
  locate: [page: number, bbox: components["schemas"]["PdfRect"], label: string];
}>();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isUnknownArray(value: unknown): value is unknown[] { return Array.isArray(value); }
function bboxOf(value: unknown): components["schemas"]["PdfRect"] | undefined {
  if (!isRecord(value)) return undefined;
  const { x1, y1, x2, y2 } = value;
  if (![x1, y1, x2, y2].every((item) => typeof item === "number" && Number.isFinite(item))) return undefined;
  if (typeof x1 !== "number" || typeof y1 !== "number" || typeof x2 !== "number" || typeof y2 !== "number") return undefined;
  return x2 > x1 && y2 > y1 ? { x1, y1, x2, y2 } : undefined;
}
function visualOf(value: unknown): VisualMeta | undefined {
  if (!isRecord(value)) return undefined;
  const bbox = bboxOf(value.bbox);
  if (typeof value.artifact_name !== "string" || typeof value.caption !== "string"
    || typeof value.id !== "string" || typeof value.page !== "number" || !bbox) return undefined;
  return { artifactName: value.artifact_name, bbox, caption: value.caption, id: value.id, page: value.page };
}
function documentVisuals(text: string | undefined): VisualMeta[] {
  if (!text) return [];
  try {
    const value: unknown = JSON.parse(text);
    if (!isRecord(value) || value.schema !== "flori.document_structure.v1") return [];
    const figures = value.figures;
    const tables = value.tables;
    if (!isUnknownArray(figures) || !isUnknownArray(tables)) return [];
    return figures.concat(tables).map(visualOf).filter((item): item is VisualMeta => item !== undefined);
  } catch { return []; }
}

const cards = computed<VisualCard[]>(() => {
  const metadata = documentVisuals(props.documentText);
  let figures = 0;
  let tables = 0;
  return props.artifacts.flatMap((artifact) => {
    if (artifact.kind !== "figure" && artifact.kind !== "table_region") return [];
    const sequence = artifact.kind === "figure" ? ++figures : ++tables;
    return [{ artifact, meta: metadata.find((item) => item.artifactName === artifact.name), sequence }];
  });
});
function jumpTo(card: VisualCard): void {
  document.getElementById(`visual-${card.artifact.artifact_id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}
function coordinate(value: number): string { return value.toFixed(1); }
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
      <button
        v-for="card in cards"
        :key="card.artifact.artifact_id"
        type="button"
        @click="jumpTo(card)"
      >
        <span>{{ card.artifact.kind === "figure" ? "Figure" : "Table" }} {{ card.sequence }}</span>
        <small>{{ card.meta?.caption || card.artifact.name }}</small>
      </button>
    </nav>
    <div class="visual-cards">
      <article
        v-for="card in cards"
        :id="`visual-${card.artifact.artifact_id}`"
        :key="card.artifact.artifact_id"
        class="visual-card"
      >
        <header>
          <div>
            <p class="eyebrow">
              {{ card.artifact.kind === "figure" ? "Figure" : "Table" }} {{ card.sequence }}
            </p>
            <h3>{{ card.meta?.caption || card.artifact.name }}</h3>
          </div>
          <button
            v-if="card.meta"
            type="button"
            class="btn secondary compact"
            @click="emit('locate', card.meta.page, card.meta.bbox, card.meta.caption)"
          >
            <UiIcon
              name="external"
              :size="13"
            />原文第 {{ card.meta.page }} 页
          </button>
        </header>
        <img
          v-if="fileUrls.get(card.artifact.artifact_id)"
          :src="fileUrls.get(card.artifact.artifact_id)"
          :alt="card.meta?.caption || card.artifact.name"
        >
        <p class="visual-caption">
          {{ card.meta?.caption || "提取区域没有单独 caption；可在原始产物中核对。" }}
        </p>
        <dl v-if="card.meta">
          <div><dt>页码</dt><dd>{{ card.meta.page }}</dd></div>
          <div><dt>区域</dt><dd>{{ coordinate(card.meta.bbox.x1) }}, {{ coordinate(card.meta.bbox.y1) }} → {{ coordinate(card.meta.bbox.x2) }}, {{ coordinate(card.meta.bbox.y2) }}</dd></div>
        </dl>
      </article>
    </div>
  </div>
  <p
    v-else
    class="empty-state"
  >
    没有提取到 Figure 或 Table 区域。
  </p>
</template>
