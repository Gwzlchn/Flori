<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

import KnowledgeSidebar from "./components/KnowledgeSidebar.vue";
import LibraryWorkspace from "./components/LibraryWorkspace.vue";
import DocumentHeader from "./components/DocumentHeader.vue";
import PdfReader from "./components/PdfReader.vue";
import RerunPanel from "./components/RerunPanel.vue";
import SearchPanel from "./components/SearchPanel.vue";
import SystemWorkspace from "./components/SystemWorkspace.vue";
import AboutPanel from "./components/AboutPanel.vue";
import { useKnowledgeNavigation } from "./composables/useKnowledgeNavigation";
import { usePageNavigation } from "./composables/usePageNavigation";
import { usePdfWorkspace } from "./composables/usePdfWorkspace";
import UiIcon from "./components/UiIcon.vue";

const workspace = usePdfWorkspace();
const library = useKnowledgeNavigation();
const navigation = usePageNavigation();
const sidebarCollapsed = ref(localStorage.getItem("flori.sidebar.collapsed") === "true");
const mobileSidebarOpen = ref(false);
const mobileMenu = ref<HTMLButtonElement>();
const {
  activeView, readerTab, selectedTaskKey, selectedArtifactId, selectedVisualId,
  setView, setTab: setReaderTab, setTask, setArtifact, setVisual, clearReader,
} = navigation;
const {
  setup, selectedFile, job, source, busy, notice, evidence, activeEvidenceId, evidenceStatus,
  textContent, fileUrls, pdfUrl, noteText, summaryText, translationText, sourceTitle,
  documentView, documentHtml,
  chooseFile, setUploadContext, submit, selectEvidence, loadArtifact, refreshJob, openJob, closeJob,
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
const pageTitle = computed(() => activeView.value === "system" ? "系统与 Runner"
  : activeView.value === "about" ? "关于 Flori" : job.value ? sourceTitle.value : "投递内容");

function openEvidence(value: string): void { setVisual(""); void selectEvidence(value); }

async function openSidebar(): Promise<void> {
  mobileSidebarOpen.value = true;
  await nextTick();
  document.querySelector<HTMLButtonElement>(".sidebar-close")?.focus();
}
function closeSidebar(restoreFocus = true): void {
  if (!mobileSidebarOpen.value) return;
  mobileSidebarOpen.value = false;
  if (restoreFocus) void nextTick(() => mobileMenu.value?.focus());
}
function trapSidebar(event: KeyboardEvent): void {
  if (!mobileSidebarOpen.value) return;
  const items = [...document.querySelectorAll<HTMLElement>(".sidebar a, .sidebar button:not([disabled])")];
  const first = items[0];
  const last = items.at(-1);
  if (event.shiftKey && document.activeElement === first && last) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last && first) { event.preventDefault(); first.focus(); }
}

function chooseContext(domainId: string, collectionId = ""): void {
  selectContext(domainId, collectionId);
  setUploadContext(domainId, collectionId);
  closeJob();
  setView("library");
  closeSidebar(false);
}

function startSubmission(): void {
  closeJob();
  setView("library");
  closeSidebar(false);
  requestAnimationFrame(() => document.querySelector("#upload")?.scrollIntoView({ behavior: "smooth", block: "start" }));
}

function openContent(jobId: string, evidenceId?: string): void {
  closeSidebar(false);
  clearReader();
  setView("content");
  void openJob(jobId, evidenceId);
}

function navigate(view: "about" | "library" | "system"): void {
  closeSidebar(false);
  if (view === "library") closeJob();
  setView(view);
}

function toggleSidebar(): void {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  localStorage.setItem("flori.sidebar.collapsed", String(sidebarCollapsed.value));
}

watch(source, (current) => {
  if (!current) return;
  selectSource(current);
  setUploadContext(current.domain_id, current.collection_ids[0] ?? "");
  void refreshLibrary();
});
watch(job, (current) => {
  if (!current) return;
  if (selectedTaskKey.value && !current.tasks.some((task) => task.task_key === selectedTaskKey.value)) setTask("");
  if (selectedArtifactId.value
    && !current.artifacts.some((artifact) => artifact.artifact_id === selectedArtifactId.value)) setArtifact("");
});

function restoreLocation(): void {
  const params = new URL(window.location.href).searchParams;
  navigation.restore();
  const domain = domains.value.find((item) => item.domain_id === params.get("domain_id"));
  const collection = collections.value.find((item) => item.collection_id === params.get("collection_id")
    && item.domain_id === domain?.domain_id);
  if (domain) selectContext(domain.domain_id, collection?.collection_id ?? "", false);
  const requestedJob = params.get("job_id");
  if (activeView.value === "content" && requestedJob) {
    void openJob(requestedJob, params.get("evidence_id") ?? "", false);
  } else if (job.value) closeJob(false);
}
onMounted(() => window.addEventListener("popstate", restoreLocation));
onUnmounted(() => window.removeEventListener("popstate", restoreLocation));

</script>

<template>
  <div
    class="app-shell"
    :class="{ 'sidebar-collapsed': sidebarCollapsed, 'mobile-sidebar-open': mobileSidebarOpen }"
    @keydown.esc="closeSidebar()"
    @keydown.tab="trapSidebar"
  >
    <KnowledgeSidebar
      :domains="domains"
      :collections="collections"
      :sources="sources"
      :active-source-id="source?.source_id"
      :selected-domain-id="selectedDomainId"
      :selected-collection-id="selectedCollectionId"
      :status="libraryStatus"
      :collapsed="sidebarCollapsed"
      :active-view="activeView"
      @open="openContent"
      @select="chooseContext"
      @submit="startSubmission"
      @close="closeSidebar"
      @toggle="toggleSidebar"
      @navigate="navigate"
    />
    <button
      type="button"
      class="sidebar-scrim"
      aria-label="关闭导航"
      @click="closeSidebar()"
    />

    <div class="app-main">
      <header class="topbar">
        <button
          ref="mobileMenu"
          type="button"
          class="mobile-menu"
          aria-label="打开导航"
          @click="openSidebar"
        >
          <UiIcon name="menu" />
        </button>
        <div class="breadcrumb">
          <span>{{ activeDomainName }}</span><b>/</b>
          <span v-if="activeView === 'content' && activeCollectionName">{{ activeCollectionName }}</span>
          <b v-if="activeView === 'content' && activeCollectionName">/</b><strong>{{ pageTitle }}</strong>
        </div>
        <SearchPanel @open="openContent" />
      </header>

      <main
        id="workspace"
        class="page"
      >
        <SystemWorkspace
          v-if="activeView === 'system' && job"
          :job-id="job.job_id"
        />
        <SystemWorkspace v-else-if="activeView === 'system'" />
        <AboutPanel v-else-if="activeView === 'about'" />
        <template v-else>
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
            @select="openEvidence"
            @load-artifact="loadArtifact"
            @refresh="refreshJob"
            @tab-change="setReaderTab"
            @task-change="setTask"
            @artifact-change="setArtifact"
            @visual-change="setVisual"
          />
          <RerunPanel
            v-if="job"
            :job="job"
            @created="openJob"
            @refresh="refreshJob"
          />
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell { transition: grid-template-columns .16s ease; }
.app-shell.sidebar-collapsed { grid-template-columns: 64px minmax(0, 1fr); }
.sidebar-scrim, .mobile-menu { display: none; }
.mobile-menu { width: 34px; height: 34px; place-items: center; padding: 0; border: 0; border-radius: 5px; color: var(--muted); background: transparent; cursor: pointer; }
.mobile-menu:hover { color: var(--ink); background: var(--line-soft); }
@media (max-width: 980px) {
  .app-shell, .app-shell.sidebar-collapsed { display: block; }
  .app-shell :deep(.sidebar) { position: fixed; z-index: 70; top: 0; bottom: 0; left: 0; width: min(320px, 86vw); height: 100dvh; border-right: 1px solid var(--line); border-bottom: 0; box-shadow: 0 20px 60px rgb(15 15 15 / 24%); transform: translateX(-105%); transition: transform .18s ease; }
  .app-shell.mobile-sidebar-open :deep(.sidebar) { transform: translateX(0); }
  .sidebar-scrim { position: fixed; z-index: 60; inset: 0; width: 100%; height: 100%; border: 0; background: rgb(15 15 15 / 35%); cursor: default; }
  .mobile-sidebar-open .sidebar-scrim, .mobile-menu { display: grid; }
}
@media (prefers-reduced-motion: reduce) {
  .app-shell, .app-shell :deep(.sidebar) { transition: none; }
}
</style>
