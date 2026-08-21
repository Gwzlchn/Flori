<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";

type NotePart = { kind: "text" | "evidence"; value: string };

const props = defineProps<{
  note: string | undefined;
  summary: string | undefined;
  pdfUrl: string | undefined;
  evidence: components["schemas"]["EvidenceView"] | undefined;
  activeEvidenceId: string;
  status: string;
}>();
defineEmits<{ select: [evidenceId: string] }>();

function segments(text: string | undefined): NotePart[] {
  if (text === undefined) return [];
  const pattern = /\[\[evidence:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]\]/g;
  const parts: NotePart[] = [];
  let cursor = 0;
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    const [marker, id] = match;
    if (marker === undefined || id === undefined) continue;
    if (match.index > cursor) parts.push({ kind: "text", value: text.slice(cursor, match.index) });
    parts.push({ kind: "evidence", value: id });
    cursor = match.index + marker.length;
  }
  if (cursor < text.length) parts.push({ kind: "text", value: text.slice(cursor) });
  return parts;
}

const readings = computed(() => [
  { title: "摘要", parts: segments(props.summary) },
  { title: "智能笔记", parts: segments(props.note) },
].filter((reading) => reading.parts.length > 0));

const locator = computed(() => {
  const found = props.evidence?.locator;
  return found?.kind === "pdf" ? found.value : undefined;
});

const viewerSrc = computed(() => {
  const url = props.pdfUrl;
  if (url === undefined) return undefined;
  const page = locator.value?.page;
  return page === undefined ? url : `${url}#page=${page}`;
});
</script>

<template>
  <section
    class="card"
    aria-labelledby="reader-title"
  >
    <h2 id="reader-title">
      阅读与证据
    </h2>
    <div class="reader">
      <div>
        <article
          v-for="reading in readings"
          :key="reading.title"
        >
          <h3>{{ reading.title }}</h3>
          <div class="note-body">
            <template
              v-for="(part, index) in reading.parts"
              :key="index"
            >
              <button
                v-if="part.kind === 'evidence'"
                type="button"
                class="cite"
                :aria-pressed="part.value === activeEvidenceId"
                @click="$emit('select', part.value)"
              >
                证据 {{ part.value.slice(0, 8) }}
              </button>
              <span v-else>{{ part.value }}</span>
            </template>
          </div>
        </article>
        <p
          v-if="!readings.length"
          class="hint"
        >
          当前 Job 还没有可阅读的智能笔记或摘要。
        </p>
      </div>
      <div>
        <p
          class="notice"
          aria-live="polite"
        >
          {{ status }}
        </p>
        <template v-if="evidence && locator">
          <blockquote>{{ evidence.quote }}</blockquote>
          <p>PDF 第 {{ locator.page }} 页 · 原文 Artifact {{ evidence.source_artifact_id }}</p>
          <p class="bbox">
            bbox x1={{ locator.bbox.x1 }} y1={{ locator.bbox.y1 }} x2={{ locator.bbox.x2 }} y2={{ locator.bbox.y2 }}
          </p>
        </template>
        <template v-if="viewerSrc">
          <!-- 仅改变 #page 不会让已加载的内嵌查看器跳页，因此按 src 重建元素。 -->
          <object
            :key="viewerSrc"
            :data="viewerSrc"
            type="application/pdf"
            class="viewer"
          >
            <p>浏览器未内嵌显示 PDF，请使用下方链接。</p>
          </object>
          <a
            :href="viewerSrc"
            target="_blank"
            rel="noopener"
          >在新窗口打开当前页</a>
        </template>
        <p
          v-else
          class="hint"
        >
          缺少原始 PDF，无法在此定位页面。
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.reader {
  display: grid;
  gap: 1rem;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}
.note-body {
  white-space: pre-wrap;
  line-height: 1.7;
}
.cite {
  font: inherit;
  margin: 0 0.15rem;
  cursor: pointer;
}
.bbox {
  font-family: monospace;
}
.viewer {
  width: 100%;
  height: 32rem;
}
</style>
