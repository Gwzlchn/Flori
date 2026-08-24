<script setup lang="ts">
import { computed } from "vue";

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
  fileUrls: ReadonlyMap<string, string>;
}>();
const emit = defineEmits<{
  locate: [page: number, bbox: components["schemas"]["PdfRect"], label: string];
}>();

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
function jumpTo(card: VisualCard): void {
  document.getElementById(`visual-${card.meta.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
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
        :key="card.meta.id"
        type="button"
        @click="jumpTo(card)"
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
            @click="emit('locate', card.meta.page, card.meta.bbox, card.meta.caption)"
          >
            <UiIcon
              name="external"
              :size="13"
            />原文第 {{ card.meta.page }} 页
          </button>
        </header>
        <img
          v-if="card.artifact && fileUrls.get(card.artifact.artifact_id)"
          :src="fileUrls.get(card.artifact.artifact_id)"
          :alt="card.meta.caption"
        >
        <p class="visual-caption">
          {{ card.meta.caption }}
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
  </div>
  <p
    v-else
    class="empty-state"
  >
    没有提取到 Figure 或 Table 区域。
  </p>
</template>
