<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";

import KnowledgeSidebar from "./components/KnowledgeSidebar.vue";
import LibraryWorkspace from "./components/LibraryWorkspace.vue";
import ContentWorkspace from "./components/ContentWorkspace.vue";
import UploadPanel from "./components/UploadPanel.vue";
import SystemWorkspace from "./components/SystemWorkspace.vue";
import TopBar from "./components/TopBar.vue";
import AboutPanel from "./components/AboutPanel.vue";
import { useKnowledgeNavigation } from "./composables/useKnowledgeNavigation";
import { usePageNavigation } from "./composables/usePageNavigation";
import { usePdfWorkspace } from "./composables/usePdfWorkspace";

const workspace = usePdfWorkspace();
const library = useKnowledgeNavigation();
const navigation = usePageNavigation();
const sidebarCollapsed = ref(localStorage.getItem("flori.sidebar.collapsed") === "true");
const mobileSidebarOpen = ref(false);
const {
  activeView, readerTab, selectedTaskKey, selectedArtifactId, selectedVisualId,
  setView, setTab: setReaderTab, setTask, setArtifact, setVisual, clearReader,
} = navigation;
const {
  setup, selectedFile, job, source, busy, notice, evidence, activeEvidenceId, evidenceStatus,
  textContent, fileUrls, pdfUrl, noteText, summaryText, translationText, sourceTitle,
  documentView, documentHtml, videoUrl,
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
const activeCollectionId = computed(() => source.value?.collection_ids.includes(selectedCollectionId.value)
  ? selectedCollectionId.value
  : source.value?.collection_ids[0] ?? "");
const activeCollectionName = computed(() => collections.value
  .find((item) => item.collection_id === activeCollectionId.value)?.name);
const pageTitle = computed(() => activeView.value === "system" ? "系统与 Runner"
  : activeView.value === "about" ? "关于 Flori" : job.value ? sourceTitle.value : "投递内容");
const canGoBack = computed(() => activeView.value !== "library" || Boolean(job.value));

function openEvidence(value: string): void { setVisual(""); void selectEvidence(value); }

async function openSidebar(): Promise<void> {
  mobileSidebarOpen.value = true;
  await nextTick();
  document.querySelector<HTMLButtonElement>(".sidebar-close")?.focus();
}
function closeSidebar(restoreFocus = true): void {
  if (!mobileSidebarOpen.value) return;
  mobileSidebarOpen.value = false;
  if (restoreFocus) void nextTick(() => document.querySelector<HTMLButtonElement>(".mobile-menu")?.focus());
}
function trapSidebar(event: KeyboardEvent): void {
  if (!mobileSidebarOpen.value) return;
  const items = [...document.querySelectorAll<HTMLElement>(".sidebar a, .sidebar button:not([disabled])")]
    .filter((item) => item.getClientRects().length > 0 && getComputedStyle(item).visibility !== "hidden");
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

function goBack(): void {
  closeJob();
  setView("library");
}

function toggleSidebar(): void {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  localStorage.setItem("flori.sidebar.collapsed", String(sidebarCollapsed.value));
}

watch(source, (current) => {
  if (!current) return;
  selectSource(current);
  setUploadContext(current.domain_id, selectedCollectionId.value);
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
      tabindex="-1"
      aria-label="关闭导航"
      @click="closeSidebar()"
    />

    <div class="app-main">
      <TopBar
        :active-view="activeView"
        :can-go-back="canGoBack"
        :collection-name="activeCollectionName"
        :domain-name="activeDomainName"
        :title="pageTitle"
        @back="goBack"
        @collection="chooseContext(source?.domain_id ?? selectedDomainId, activeCollectionId)"
        @domain="chooseContext(source?.domain_id ?? selectedDomainId)"
        @library="navigate('library')"
        @menu="openSidebar"
        @open="openContent"
      />

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
        <ContentWorkspace
          v-else-if="job"
          :active-evidence-id="activeEvidenceId"
          :collection-names="collectionNames"
          :document-html="documentHtml"
          :document-view="documentView"
          :domain-name="activeDomainName"
          :evidence="evidence"
          :evidence-status="evidenceStatus"
          :file-urls="fileUrls"
          :job="job"
          :note-text="noteText"
          :notice="notice"
          :pdf-url="pdfUrl"
          :reader-tab="readerTab"
          :selected-artifact-id="selectedArtifactId"
          :selected-task-key="selectedTaskKey"
          :selected-visual-id="selectedVisualId"
          :source="source"
          :source-title="sourceTitle"
          :summary-text="summaryText"
          :text-content="textContent"
          :translation-text="translationText"
          :video-url="videoUrl"
          @artifact-change="setArtifact"
          @created="openJob"
          @load-artifact="loadArtifact"
          @refresh="refreshJob"
          @select="openEvidence"
          @tab-change="setReaderTab"
          @task-change="setTask"
          @visual-change="setVisual"
        />
        <template v-else>
          <LibraryWorkspace
            :domain="selectedDomain"
            :collection="selectedCollection"
            :collections="collections"
            :sources="sources"
            @open="openJob"
            @select="chooseContext"
          />

          <UploadPanel
            :busy="busy"
            :domain-name="selectedDomain?.name ?? '默认领域'"
            :collection-name="selectedCollection?.name"
            :file="selectedFile"
            :ready="Boolean(setup)"
            @choose="chooseFile"
            @submit="submit"
          />

          <p
            class="notice-bar"
            aria-live="polite"
          >
            {{ notice }}
          </p>
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell { transition: grid-template-columns .16s ease; }
.app-shell.sidebar-collapsed { grid-template-columns: 64px minmax(0, 1fr); }
.sidebar-scrim { display: none; }
@media (max-width: 980px) {
  .app-shell, .app-shell.sidebar-collapsed { display: block; }
  .app-shell :deep(.sidebar) { position: fixed; z-index: 70; top: 0; bottom: 0; left: 0; width: min(320px, 86vw); height: 100dvh; border-right: 1px solid var(--line); border-bottom: 0; box-shadow: 0 20px 60px rgb(15 15 15 / 24%); transform: translateX(-105%); transition: transform .18s ease; }
  .app-shell.mobile-sidebar-open :deep(.sidebar) { transform: translateX(0); }
  .sidebar-scrim { position: fixed; z-index: 60; inset: 0; width: 100%; height: 100%; border: 0; background: rgb(15 15 15 / 35%); cursor: default; }
  .mobile-sidebar-open .sidebar-scrim { display: grid; }
}
@media (prefers-reduced-motion: reduce) {
  .app-shell, .app-shell :deep(.sidebar) { transition: none; }
}
</style>
