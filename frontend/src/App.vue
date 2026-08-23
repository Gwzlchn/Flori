<script setup lang="ts">
import { computed, watch } from "vue";

import KnowledgeSidebar from "./components/KnowledgeSidebar.vue";
import LibraryWorkspace from "./components/LibraryWorkspace.vue";
import DocumentHeader from "./components/DocumentHeader.vue";
import PdfReader from "./components/PdfReader.vue";
import RerunPanel from "./components/RerunPanel.vue";
import SearchPanel from "./components/SearchPanel.vue";
import { useKnowledgeNavigation } from "./composables/useKnowledgeNavigation";
import { usePdfWorkspace } from "./composables/usePdfWorkspace";

const workspace = usePdfWorkspace();
const library = useKnowledgeNavigation();
const {
  setup, selectedFile, job, source, busy, notice, evidence, activeEvidenceId, evidenceStatus,
  textContent, fileUrls, pdfUrl, noteText, summaryText, translationText, sourceTitle,
  chooseFile, setUploadContext, submit, selectEvidence, refreshJob, openJob, closeJob,
} = workspace;
const {
  domains, collections, sources, selectedDomainId, selectedCollectionId, selectedDomain,
  selectedCollection, status: libraryStatus, select: selectContext, selectSource, refresh: refreshLibrary,
} = library;

const collectionNames = computed(() => source.value?.collection_ids
  .map((id) => collections.value.find((item) => item.collection_id === id)?.name)
  .filter((name): name is string => name !== undefined) ?? []);
const activeDomainName = computed(() => domains.value.find((item) => item.domain_id === source.value?.domain_id)?.name
  ?? selectedDomain.value?.name ?? "知识库");
const activeCollectionName = computed(() => collectionNames.value[0] ?? selectedCollection.value?.name);
const showNotice = computed(() => !job.value || job.value.state !== "succeeded" || !notice.value.startsWith("Job succeeded"));

function chooseContext(domainId: string, collectionId = ""): void {
  selectContext(domainId, collectionId);
  setUploadContext(domainId, collectionId);
  closeJob();
}

function startSubmission(): void {
  closeJob();
  requestAnimationFrame(() => document.querySelector("#upload")?.scrollIntoView({ behavior: "smooth", block: "start" }));
}

watch(source, (current) => {
  if (!current) return;
  selectSource(current);
  setUploadContext(current.domain_id, current.collection_ids[0] ?? "");
  void refreshLibrary();
});

</script>

<template>
  <div class="app-shell">
    <KnowledgeSidebar
      :domains="domains"
      :collections="collections"
      :sources="sources"
      :active-source-id="source?.source_id"
      :selected-domain-id="selectedDomainId"
      :selected-collection-id="selectedCollectionId"
      :status="libraryStatus"
      @open="openJob"
      @select="chooseContext"
      @submit="startSubmission"
    />

    <div class="app-main">
      <header class="topbar">
        <div class="breadcrumb">
          <span>{{ activeDomainName }}</span><b>/</b>
          <span v-if="activeCollectionName">{{ activeCollectionName }}</span>
          <b v-if="activeCollectionName">/</b><strong>{{ job ? sourceTitle : "投递内容" }}</strong>
        </div>
        <SearchPanel @open="openJob" />
      </header>

      <main
        id="workspace"
        class="page"
      >
        <DocumentHeader
          v-if="job"
          :job="job"
          :source="source"
          :title="sourceTitle"
          :domain-name="activeDomainName"
          :collection-names="collectionNames"
          @refresh="refreshJob"
        />
        <LibraryWorkspace
          v-else
          :domain="selectedDomain"
          :collection="selectedCollection"
          :collections="collections"
          :sources="sources"
          @open="openJob"
          @select="chooseContext"
        />

        <details
          v-if="!job"
          id="upload"
          class="upload-card card"
          :open="!job"
        >
          <summary>
            <span><b>上传并解析 PDF</b><small>投递到 {{ selectedDomain?.name ?? "默认领域" }} / {{ selectedCollection?.name ?? "未归分类" }}</small></span>
            <span class="summary-action">选择文件</span>
          </summary>
          <div class="upload-body">
            <label
              class="file-drop"
              for="pdf-file"
            >
              <span class="file-icon">PDF</span>
              <span><b>{{ selectedFile?.name ?? "选择数字版 PDF" }}</b><small>浏览器先计算 SHA-256，再安全上传</small></span>
            </label>
            <input
              id="pdf-file"
              class="visually-hidden"
              type="file"
              accept="application/pdf,.pdf"
              :disabled="busy"
              @change="chooseFile"
            >
            <button
              type="button"
              class="btn primary"
              :disabled="!selectedFile || !setup || busy"
              @click="submit"
            >
              {{ busy ? "处理中…" : "开始解析" }}
            </button>
          </div>
        </details>

        <p
          v-if="showNotice"
          class="notice-bar"
          aria-live="polite"
        >
          {{ notice }}
        </p>

        <PdfReader
          v-if="job"
          :job="job"
          :source="source"
          :domain-name="domains.find((item) => item.domain_id === source?.domain_id)?.name"
          :collection-names="collectionNames"
          :note="noteText"
          :summary="summaryText"
          :translation="translationText"
          :pdf-url="pdfUrl"
          :evidence="evidence"
          :active-evidence-id="activeEvidenceId"
          :status="evidenceStatus"
          :text-content="textContent"
          :file-urls="fileUrls"
          @select="selectEvidence"
          @refresh="refreshJob"
        />
        <RerunPanel
          v-if="job"
          :job="job"
          @created="openJob"
          @refresh="refreshJob"
        />
      </main>
    </div>
  </div>
</template>
