<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import { isVideoSource } from "../composables/useEvidenceNavigation";
import DocumentHeader from "./DocumentHeader.vue";
import PdfReader from "./PdfReader.vue";
import RerunPanel from "./RerunPanel.vue";
import VideoReader from "./VideoReader.vue";

type ReaderTab = "artifacts" | "metadata" | "pipeline" | "visuals";
const props = defineProps<{
  activeEvidenceId: string;
  collectionNames: string[];
  documentHtml: string | undefined;
  documentView: components["schemas"]["DocumentRepresentationView"] | undefined;
  domainName: string;
  evidence: components["schemas"]["EvidenceView"] | undefined;
  evidenceStatus: string;
  fileUrls: ReadonlyMap<string, string>;
  job: components["schemas"]["JobView"];
  noteText: string | undefined;
  notice: string;
  pdfUrl: string | undefined;
  readerTab: ReaderTab;
  selectedArtifactId: string;
  selectedTaskKey: string;
  selectedVisualId: string;
  source: components["schemas"]["SourceView"] | undefined;
  sourceTitle: string;
  summaryText: string | undefined;
  textContent: ReadonlyMap<string, string>;
  translationText: string | undefined;
  videoUrl: string | undefined;
}>();
const emit = defineEmits<{
  artifactChange: [artifactId: string];
  created: [jobId: string];
  loadArtifact: [artifact: components["schemas"]["ArtifactView"]];
  refresh: [];
  select: [evidenceId: string];
  tabChange: [tab: ReaderTab];
  taskChange: [taskKey: string];
  visualChange: [visualId: string];
}>();
const showNotice = computed(() => props.job.state !== "succeeded" || !props.notice.startsWith("Job succeeded"));
</script>

<template>
  <DocumentHeader
    :job="job"
    :source="source"
    :title="sourceTitle"
    :domain-name="domainName"
    :collection-names="collectionNames"
    @refresh="emit('refresh')"
  />
  <p
    v-if="showNotice"
    class="notice-bar"
    aria-live="polite"
  >
    {{ notice }}
  </p>
  <PdfReader
    v-if="source && !isVideoSource(source.kind)"
    :job="job"
    :source="source"
    :domain-name="domainName"
    :collection-names="collectionNames"
    :note="noteText"
    :summary="summaryText"
    :translation="translationText"
    :pdf-url="pdfUrl"
    :document-view="documentView"
    :document-html="documentHtml"
    :evidence="evidence"
    :active-evidence-id="activeEvidenceId"
    :status="evidenceStatus"
    :text-content="textContent"
    :file-urls="fileUrls"
    :tab="readerTab"
    :selected-task-key="selectedTaskKey"
    :selected-artifact-id="selectedArtifactId"
    :selected-visual-id="selectedVisualId"
    @select="emit('select', $event)"
    @load-artifact="emit('loadArtifact', $event)"
    @refresh="emit('refresh')"
    @tab-change="emit('tabChange', $event)"
    @task-change="emit('taskChange', $event)"
    @artifact-change="emit('artifactChange', $event)"
    @visual-change="emit('visualChange', $event)"
  />
  <VideoReader
    v-else-if="source"
    :job="job"
    :source="source"
    :domain-name="domainName"
    :collection-names="collectionNames"
    :note="noteText"
    :summary="summaryText"
    :video-url="videoUrl"
    :evidence="evidence"
    :active-evidence-id="activeEvidenceId"
    :status="evidenceStatus"
    :text-content="textContent"
    :file-urls="fileUrls"
    :tab="readerTab"
    :selected-task-key="selectedTaskKey"
    :selected-artifact-id="selectedArtifactId"
    @select="emit('select', $event)"
    @load-artifact="emit('loadArtifact', $event)"
    @refresh="emit('refresh')"
    @tab-change="emit('tabChange', $event)"
    @task-change="emit('taskChange', $event)"
    @artifact-change="emit('artifactChange', $event)"
  />
  <RerunPanel
    :job="job"
    @created="emit('created', $event)"
    @refresh="emit('refresh')"
  />
</template>
