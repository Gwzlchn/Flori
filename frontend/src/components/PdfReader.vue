<script setup lang="ts">
import { computed, ref } from "vue";

import type { components } from "../api/client";
import JobPanel from "./JobPanel.vue";
import MarkdownContent from "./MarkdownContent.vue";

type ReaderTab = "note" | "summary" | "visuals" | "pipeline" | "artifacts";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  note: string | undefined;
  summary: string | undefined;
  translation: string | undefined;
  pdfUrl: string | undefined;
  evidence: components["schemas"]["EvidenceView"] | undefined;
  activeEvidenceId: string;
  status: string;
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
}>();
const emit = defineEmits<{ select: [evidenceId: string]; refresh: [] }>();
const tab = ref<ReaderTab>("note");

const tabs: { id: ReaderTab; label: string }[] = [
  { id: "note", label: "笔记" },
  { id: "summary", label: "摘要" },
  { id: "visuals", label: "图表" },
  { id: "pipeline", label: "Pipeline" },
  { id: "artifacts", label: "原始产物" },
];
const locator = computed(() => props.evidence?.locator.kind === "pdf" ? props.evidence.locator.value : undefined);
const viewerSrc = computed(() => {
  if (!props.pdfUrl) return undefined;
  return locator.value ? `${props.pdfUrl}#page=${locator.value.page}` : props.pdfUrl;
});
const visuals = computed(() => props.job.artifacts.filter((artifact) =>
  artifact.kind === "figure" || artifact.kind === "table_region",
));
</script>

<template>
  <section
    class="reader-card card"
    aria-labelledby="reader-title"
  >
    <div class="reader-heading">
      <div>
        <p class="eyebrow">
          Published knowledge
        </p>
        <h2 id="reader-title">
          论文阅读
        </h2>
      </div>
      <div
        class="reader-tabs"
        role="tablist"
        aria-label="阅读内容"
      >
        <button
          v-for="item in tabs"
          :id="`tab-${item.id}`"
          :key="item.id"
          type="button"
          role="tab"
          :aria-selected="tab === item.id"
          :aria-controls="`panel-${item.id}`"
          @click="tab = item.id"
        >
          {{ item.label }}
        </button>
      </div>
    </div>

    <div class="reader-layout">
      <div class="reader-content">
        <section
          v-if="tab === 'note'"
          id="panel-note"
          role="tabpanel"
          aria-labelledby="tab-note"
        >
          <MarkdownContent
            v-if="note"
            :content="note"
            :active-evidence-id="activeEvidenceId"
            @select="emit('select', $event)"
          />
          <p
            v-else
            class="empty-state"
          >
            智能笔记尚未生成。Pipeline 完成后会在这里显示。
          </p>
          <details
            v-if="translation"
            class="translation-block"
          >
            <summary>查看全文翻译</summary>
            <MarkdownContent
              :content="translation"
              :active-evidence-id="activeEvidenceId"
              @select="emit('select', $event)"
            />
          </details>
        </section>
        <section
          v-else-if="tab === 'summary'"
          id="panel-summary"
          role="tabpanel"
          aria-labelledby="tab-summary"
        >
          <MarkdownContent
            v-if="summary"
            :content="summary"
            :active-evidence-id="activeEvidenceId"
            @select="emit('select', $event)"
          />
          <p
            v-else
            class="empty-state"
          >
            当前成果没有摘要。
          </p>
        </section>
        <section
          v-else-if="tab === 'visuals'"
          id="panel-visuals"
          role="tabpanel"
          aria-labelledby="tab-visuals"
        >
          <div
            v-if="visuals.length"
            class="visual-grid"
          >
            <figure
              v-for="artifact in visuals"
              :key="artifact.artifact_id"
            >
              <img
                v-if="fileUrls.get(artifact.artifact_id)"
                :src="fileUrls.get(artifact.artifact_id)"
                :alt="`${artifact.kind}: ${artifact.name}`"
              >
              <figcaption><b>{{ artifact.kind === "figure" ? "Figure" : "Table" }}</b>{{ artifact.name }}</figcaption>
            </figure>
          </div>
          <p
            v-else
            class="empty-state"
          >
            没有提取到 Figure 或 Table 区域。
          </p>
        </section>
        <JobPanel
          v-else-if="tab === 'pipeline'"
          id="pipeline"
          :job="job"
          mode="pipeline"
          :text-content="textContent"
          :file-urls="fileUrls"
          @refresh="emit('refresh')"
        />
        <JobPanel
          v-else
          id="panel-artifacts"
          :job="job"
          mode="artifacts"
          :text-content="textContent"
          :file-urls="fileUrls"
          @refresh="emit('refresh')"
        />
      </div>

      <aside
        class="evidence-panel"
        aria-label="PDF 证据定位"
      >
        <div class="evidence-head">
          <span><i /> Evidence</span>
          <a
            v-if="viewerSrc"
            :href="viewerSrc"
            target="_blank"
            rel="noopener"
          >新窗口打开</a>
        </div>
        <p
          class="evidence-status"
          aria-live="polite"
        >
          {{ status }}
        </p>
        <template v-if="evidence && locator">
          <blockquote>{{ evidence.quote }}</blockquote>
          <dl class="locator-grid">
            <div><dt>页码</dt><dd>{{ locator.page }}</dd></div>
            <div><dt>坐标</dt><dd>{{ locator.bbox.x1 }}, {{ locator.bbox.y1 }} → {{ locator.bbox.x2 }}, {{ locator.bbox.y2 }}</dd></div>
          </dl>
        </template>
        <object
          v-if="viewerSrc"
          :key="viewerSrc"
          :data="viewerSrc"
          type="application/pdf"
          class="pdf-viewer"
        >
          <p>浏览器无法内嵌 PDF，请在新窗口打开。</p>
        </object>
        <p
          v-else
          class="empty-state"
        >
          缺少原始 PDF，无法定位页面。
        </p>
      </aside>
    </div>
  </section>
</template>
