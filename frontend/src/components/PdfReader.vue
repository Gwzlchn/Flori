<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../api/client";
import ArtifactReaderPanel from "./ArtifactReaderPanel.vue";
import JobPanel from "./JobPanel.vue";
import MetadataPanel from "./MetadataPanel.vue";
import ScholarlyDocument from "./ScholarlyDocument.vue";
import UiIcon from "./UiIcon.vue";
import VisualCatalog from "./VisualCatalog.vue";

type ReaderTab = "artifacts" | "pipeline" | "metadata" | "visuals";
interface VisualLocation { bbox: components["schemas"]["PdfRect"]; label: string; page: number }

const props = defineProps<{
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
  domainName: string | undefined;
  collectionNames: string[];
  note: string | undefined;
  summary: string | undefined;
  translation: string | undefined;
  pdfUrl: string | undefined;
  documentView: components["schemas"]["DocumentRepresentationView"] | undefined;
  documentHtml: string | undefined;
  evidence: components["schemas"]["EvidenceView"] | undefined;
  activeEvidenceId: string;
  status: string;
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
  tab: ReaderTab;
  selectedTaskKey: string;
  selectedArtifactId: string;
  selectedVisualId: string;
}>();
const emit = defineEmits<{
  select: [evidenceId: string];
  refresh: [];
  loadArtifact: [artifact: components["schemas"]["ArtifactView"]];
  tabChange: [tab: ReaderTab];
  taskChange: [taskKey: string];
  artifactChange: [artifactId: string];
  visualChange: [visualId: string];
}>();
const visualLocation = ref<VisualLocation>();
const visualHtml = ref<{ anchor: string; label: string }>();
const artifactView = ref<"alternate" | "notes" | "source">("notes");
const forcePdf = ref(false);

const tabs = [
  { id: "artifacts", label: "产物", icon: "book" },
  { id: "pipeline", label: "流水线", icon: "pipeline" },
  { id: "metadata", label: "元信息", icon: "info" },
  { id: "visuals", label: "图表", icon: "image" },
] as const;
const evidenceLocator = computed(() => props.evidence?.locator.kind === "pdf" ? props.evidence.locator.value : undefined);
const activeLocation = computed(() => visualLocation.value ?? evidenceLocator.value);
const viewerSrc = computed(() => {
  if (!props.pdfUrl) return undefined;
  return activeLocation.value ? `${props.pdfUrl}#page=${activeLocation.value.page}` : props.pdfUrl;
});
const htmlView = computed(() => props.documentView?.representation === "scholarly_html" ? props.documentView : undefined);
const htmlAnchor = computed(() => visualHtml.value?.anchor ?? (htmlView.value?.crosswalk?.status === "verified"
  ? htmlView.value.crosswalk.html_anchor ?? undefined : undefined));
const showHtml = computed(() => Boolean(!forcePdf.value && props.documentHtml && htmlView.value && !visualLocation.value
  && (visualHtml.value || !props.activeEvidenceId || htmlAnchor.value)));
const showContext = computed(() => props.tab === "visuals" || (props.tab === "artifacts"
  && artifactView.value === "notes" && Boolean(props.activeEvidenceId)));
const representationLabel = computed(() => showHtml.value
  ? htmlView.value?.provider === "arxiv" ? "arXiv HTML" : "ar5iv HTML"
  : "PDF canonical evidence");

function locateVisual(id: string, page: number, bbox: components["schemas"]["PdfRect"], label: string, notify = true): void {
  visualHtml.value = undefined;
  visualLocation.value = { page, bbox, label };
  if (notify) emit("visualChange", id);
}
function locateHtml(id: string, anchor: string, label: string, notify = true): void {
  visualLocation.value = undefined;
  visualHtml.value = { anchor, label };
  if (notify) emit("visualChange", id);
}
function selectTab(id: ReaderTab): void {
  emit("tabChange", id);
  if (id !== "visuals") return;
  for (const artifact of props.job.artifacts) {
    if (artifact.kind === "figure" || artifact.kind === "table_region") emit("loadArtifact", artifact);
  }
}
function coordinate(value: number): string { return value.toFixed(1); }
watch(() => props.activeEvidenceId, () => { visualLocation.value = undefined; visualHtml.value = undefined; });
</script>

<template>
  <section
    class="reader-card card"
    aria-label="内容工作台"
  >
    <div class="reader-heading">
      <div
        class="reader-tabs"
        role="tablist"
        aria-label="内容工作台"
      >
        <button
          v-for="item in tabs"
          :id="`tab-${item.id}`"
          :key="item.id"
          type="button"
          role="tab"
          :aria-selected="props.tab === item.id"
          :aria-controls="`panel-${item.id}`"
          @click="selectTab(item.id)"
        >
          <UiIcon
            :name="item.icon"
            :size="15"
          />{{ item.label }}
        </button>
      </div>
    </div>

    <div
      class="reader-layout"
      :class="{ 'has-context': showContext }"
    >
      <div class="reader-content">
        <section
          v-if="props.tab === 'artifacts'"
          id="panel-artifacts"
          role="tabpanel"
          aria-labelledby="tab-artifacts"
          class="knowledge-output"
        >
          <ArtifactReaderPanel
            :view="artifactView"
            source-label="原文"
            alternate-label="译文"
            :alternate-content="translation"
            evidence-intro="引用标记仅会定位到经 Rust 校验的原文证据。"
            :job="job"
            :note="note"
            :summary="summary"
            :active-evidence-id="activeEvidenceId"
            :text-content="textContent"
            :file-urls="fileUrls"
            :selected-task-key="selectedTaskKey"
            :selected-artifact-id="selectedArtifactId"
            @view-change="artifactView = $event"
            @select="emit('select', $event)"
            @load-artifact="emit('loadArtifact', $event)"
            @refresh="emit('refresh')"
            @task-change="emit('taskChange', $event)"
            @artifact-change="emit('artifactChange', $event)"
          >
            <template #source>
              <section class="source-reader">
                <div
                  v-if="documentHtml && htmlView && pdfUrl"
                  class="representation-switch"
                >
                  <button
                    type="button"
                    :aria-pressed="showHtml"
                    @click="forcePdf = false"
                  >
                    原文
                  </button>
                  <button
                    type="button"
                    :aria-pressed="!showHtml"
                    @click="forcePdf = true"
                  >
                    原文 PDF
                  </button>
                </div>
                <ScholarlyDocument
                  v-if="showHtml && documentHtml && htmlView"
                  :html="documentHtml"
                  :resources="htmlView.resources"
                  :file-urls="fileUrls"
                  :anchor="htmlAnchor"
                />
                <object
                  v-else-if="viewerSrc"
                  :data="viewerSrc"
                  type="application/pdf"
                  class="pdf-viewer source-pdf"
                >
                  <p>浏览器无法内嵌 PDF，请在新窗口打开。</p>
                </object>
                <p
                  v-else
                  class="empty-state"
                >
                  缺少可验证的学术 HTML 与原始 PDF。
                </p>
              </section>
            </template>
          </ArtifactReaderPanel>
        </section>

        <section
          v-else-if="props.tab === 'visuals'"
          id="panel-visuals"
          role="tabpanel"
          aria-labelledby="tab-visuals"
        >
          <header class="panel-intro">
            <p class="eyebrow">
              Figures &amp; tables
            </p><h2>图表与原文区域</h2>
            <p class="meta">
              从 DocumentStructure 读取 caption、页码与 bbox；点击即可在右侧核对原文。
            </p>
          </header>
          <VisualCatalog
            :artifacts="job.artifacts"
            :structure="documentView?.structure"
            :projections="htmlView?.visuals"
            :file-urls="fileUrls"
            :active-visual-id="selectedVisualId"
            @locate="locateVisual"
            @locate-html="locateHtml"
          />
        </section>

        <JobPanel
          v-else-if="props.tab === 'pipeline'"
          id="panel-pipeline"
          :job="job"
          mode="pipeline"
          :text-content="textContent"
          :file-urls="fileUrls"
          :selected-task-key="selectedTaskKey"
          :selected-artifact-id="selectedArtifactId"
          @load-artifact="emit('loadArtifact', $event)"
          @refresh="emit('refresh')"
          @task-change="emit('taskChange', $event)"
          @artifact-change="emit('artifactChange', $event)"
        />

        <MetadataPanel
          v-else
          id="panel-metadata"
          :job="job"
          :source="source"
          :domain-name="domainName"
          :collection-names="collectionNames"
          :document-view="documentView"
        />
      </div>

      <aside
        v-if="showContext"
        class="evidence-panel"
        aria-label="PDF 证据定位"
      >
        <div class="evidence-head">
          <span><i /> {{ representationLabel }}</span>
          <a
            v-if="!showHtml && viewerSrc"
            :href="viewerSrc"
            target="_blank"
            rel="noopener"
          >新窗口打开</a>
        </div>
        <p
          class="evidence-status"
          aria-live="polite"
        >
          {{ visualHtml ? `已定位 HTML 图表：${visualHtml.label}` : visualLocation ? `已定位图表：${visualLocation.label}` : status }}
        </p>
        <blockquote v-if="evidence && evidenceLocator && !visualLocation">
          {{ evidence.quote }}
        </blockquote>
        <dl
          v-if="activeLocation"
          class="locator-grid"
        >
          <div><dt>页码</dt><dd>{{ activeLocation.page }}</dd></div>
          <div><dt>坐标</dt><dd>{{ coordinate(activeLocation.bbox.x1) }}, {{ coordinate(activeLocation.bbox.y1) }} → {{ coordinate(activeLocation.bbox.x2) }}, {{ coordinate(activeLocation.bbox.y2) }}</dd></div>
        </dl>
        <ScholarlyDocument
          v-if="showHtml && documentHtml && htmlView"
          :html="documentHtml"
          :resources="htmlView.resources"
          :file-urls="fileUrls"
          :anchor="htmlAnchor"
        />
        <object
          v-else-if="viewerSrc"
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
          缺少可验证的学术 HTML 与原始 PDF，无法定位原文。
        </p>
      </aside>
    </div>
  </section>
</template>
