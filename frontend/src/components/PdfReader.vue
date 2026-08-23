<script setup lang="ts">
import { computed, ref } from "vue";

import type { components } from "../api/client";
import JobPanel from "./JobPanel.vue";
import MarkdownContent from "./MarkdownContent.vue";

type ReaderTab = "artifacts" | "pipeline" | "metadata" | "visuals";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
  domainName: string | undefined;
  collectionNames: string[];
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
const tab = ref<ReaderTab>("artifacts");

const tabs: { id: ReaderTab; label: string }[] = [
  { id: "artifacts", label: "产物" },
  { id: "pipeline", label: "Pipeline" },
  { id: "metadata", label: "元信息" },
  { id: "visuals", label: "图表" },
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
          v-if="tab === 'artifacts'"
          id="panel-artifacts"
          role="tabpanel"
          aria-labelledby="tab-artifacts"
          class="knowledge-output"
        >
          <header class="output-heading">
            <p class="eyebrow">
              Published output
            </p><h3>智能笔记</h3>
          </header>
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
          <section class="summary-note">
            <p class="eyebrow">
              中文摘要
            </p>
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
          <JobPanel
            :job="job"
            mode="artifacts"
            :text-content="textContent"
            :file-urls="fileUrls"
            @refresh="emit('refresh')"
          />
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
        <section
          v-else
          id="panel-metadata"
          role="tabpanel"
          aria-labelledby="tab-metadata"
          class="metadata-panel"
        >
          <p class="eyebrow">
            Source
          </p><h3>内容元信息</h3>
          <dl class="metadata-grid">
            <div><dt>标题</dt><dd>{{ source?.title ?? source?.canonical_ref ?? "—" }}</dd></div>
            <div><dt>领域</dt><dd>{{ domainName ?? source?.domain_id ?? "—" }}</dd></div>
            <div><dt>分类</dt><dd>{{ collectionNames.join("、") || "未归分类" }}</dd></div>
            <div><dt>来源类型</dt><dd>{{ source?.kind ?? "—" }}</dd></div>
            <div><dt>规范引用</dt><dd>{{ source?.canonical_ref ?? "—" }}</dd></div>
            <div><dt>Job 状态</dt><dd>{{ job.state }} · {{ job.trigger }}</dd></div>
            <div><dt>Pipeline revision</dt><dd>{{ job.pipeline_revision_id }}</dd></div>
            <div><dt>输入</dt><dd>translate={{ job.inputs.translate }}</dd></div>
            <div><dt>Current Job</dt><dd>{{ source?.current_job_id ?? "—" }}</dd></div>
            <div><dt>Previous Job</dt><dd>{{ source?.previous_job_id ?? "—" }}</dd></div>
          </dl>
        </section>
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
