import { computed, onMounted, onUnmounted, ref } from "vue";

import { apiClient, apiError, type components, watchJobEvents } from "../api/client";
import { useArtifactContent } from "./useArtifactContent";
import { isVideoSource, useEvidenceNavigation } from "./useEvidenceNavigation";

const EAGER_TEXT_KINDS = new Set<components["schemas"]["ArtifactKind"]>([
  "translation", "smart_note", "summary", "mechanical_note",
]);

export function usePdfWorkspace() {
  const setup = ref<components["schemas"]["PdfSetupView"]>();
  const selectedFile = ref<File>();
  const job = ref<components["schemas"]["JobView"]>();
  const source = ref<components["schemas"]["SourceView"]>();
  const jobId = ref("");
  const uploadDomainId = ref("");
  const uploadCollectionId = ref("");
  const uploadKey = ref("");
  const jobKey = ref("");
  const busy = ref(false);
  const notice = ref("正在读取 PDF 配置…");
  const documentView = ref<components["schemas"]["DocumentRepresentationView"]>();
  const documentHtml = ref<string>();
  const content = useArtifactContent(jobId, (message) => { notice.value = message; });
  const { textContent, fileUrls } = content;
  let eventController: AbortController | undefined;
  let eventCursor = 0;
  let eventRefreshTimer: number | undefined;
  let generation = 0;
  let refreshSequence = 0;
  let documentSequence = 0;

  function artifactOf(kind: components["schemas"]["ArtifactKind"]): components["schemas"]["ArtifactView"] | undefined {
    return job.value?.artifacts.find((artifact) => artifact.kind === kind);
  }
  function textOf(kind: components["schemas"]["ArtifactKind"]): string | undefined {
    const artifact = artifactOf(kind);
    return artifact ? textContent.get(artifact.artifact_id) : undefined;
  }

  const sourceOriginal = computed(() => artifactOf("source_original"));
  const pdfUrl = computed(() => {
    const artifact = sourceOriginal.value;
    return artifact?.media_type === "application/pdf" ? fileUrls.get(artifact.artifact_id) : undefined;
  });
  const videoUrl = computed(() => {
    const artifact = sourceOriginal.value;
    return artifact?.media_type.startsWith("video/") ? fileUrls.get(artifact.artifact_id) : undefined;
  });
  const noteText = computed(() => textOf("smart_note"));
  const summaryText = computed(() => textOf("summary"));
  const translationText = computed(() => textOf("translation"));
  const sourceTitle = computed(() => source.value?.title ?? selectedFile.value?.name ?? "PDF 阅读工作台");

  function chooseFile(event: Event): void {
    if (!(event.currentTarget instanceof HTMLInputElement)) return;
    selectedFile.value = event.currentTarget.files?.item(0) ?? undefined;
    uploadKey.value = selectedFile.value ? crypto.randomUUID() : "";
    jobKey.value = selectedFile.value ? crypto.randomUUID() : "";
  }

  function setUploadContext(domainId: string, collectionId = ""): void {
    uploadDomainId.value = domainId;
    uploadCollectionId.value = collectionId;
  }

  function remember(values: Partial<Record<"evidence_id" | "job_id" | "source_id" | "view", string>>): void {
    const url = new URL(window.location.href);
    for (const [name, value] of Object.entries(values)) {
      if (value) url.searchParams.set(name, value);
      else url.searchParams.delete(name);
    }
    history.replaceState(null, "", url);
  }

  const evidenceNavigation = useEvidenceNavigation(
    job,
    source,
    sourceOriginal,
    (id) => remember({ evidence_id: id }),
    async (id) => loadDocument(id, generation),
  );
  const { evidence, activeEvidenceId, evidenceStatus, reset: resetEvidence, select: selectEvidence } = evidenceNavigation;

  function clearArtifacts(): void {
    content.clear();
    documentView.value = undefined;
    documentHtml.value = undefined;
  }

  function stopEvents(): void {
    eventController?.abort();
    eventController = undefined;
    window.clearTimeout(eventRefreshTimer);
  }

  function startEvents(id: string): void {
    stopEvents();
    eventCursor = 0;
    eventController = new AbortController();
    const controller = eventController;
    void (async () => {
      while (!controller.signal.aborted && jobId.value === id) {
        const result = await watchJobEvents(id, eventCursor, controller.signal, (cursor) => {
          eventCursor = cursor;
          window.clearTimeout(eventRefreshTimer);
          eventRefreshTimer = window.setTimeout(() => void refreshJob(), 80);
        });
        if (result === "expired") {
          eventCursor = 0;
          await refreshJob();
        }
        if (controller.signal.aborted || job.value?.state === "succeeded"
          || job.value?.state === "failed" || job.value?.state === "canceled") break;
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
    })();
  }

  async function loadSource(sourceId: string, expected: number): Promise<void> {
    const result = await apiClient.GET("/api/v1/sources/{source_id}", { params: { path: { source_id: sourceId } } });
    if (generation !== expected) return;
    if (result.data) {
      source.value = result.data;
      remember({ source_id: result.data.source_id, view: "content" });
    }
    else notice.value = apiError(result.error, "source_read_failed: 无法读取来源。");
  }

  async function loadDocument(evidenceId = "", expected = generation): Promise<void> {
    const current = job.value;
    const currentSource = source.value;
    if (!current || !currentSource || current.state !== "succeeded") return;
    if (isVideoSource(currentSource.kind)) {
      documentView.value = undefined;
      documentHtml.value = undefined;
      return;
    }
    const sequence = ++documentSequence;
    const result = await apiClient.GET("/api/v1/sources/{source_id}/document", {
      params: {
        path: { source_id: currentSource.source_id },
        query: evidenceId ? { evidence_id: evidenceId } : {},
      },
    });
    if (generation !== expected || sequence !== documentSequence
      || !result.data || result.data.job_id !== current.job_id) return;
    documentView.value = result.data;
    documentHtml.value = undefined;
    if (result.data.representation === "scholarly_html") {
      await Promise.all(result.data.resources.map(content.load));
      const html = await apiClient.GET("/api/v1/sources/{source_id}/document/content", {
        params: { path: { source_id: currentSource.source_id } }, parseAs: "text",
      });
      if (generation === expected && sequence === documentSequence
        && html.data !== undefined && job.value?.job_id === current.job_id) documentHtml.value = html.data;
    }
  }

  async function refreshJob(expected = generation): Promise<components["schemas"]["JobView"] | undefined> {
    const id = jobId.value;
    if (!id) return;
    const sequence = ++refreshSequence;
    try {
      const result = await apiClient.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: id } } });
      if (generation !== expected || sequence !== refreshSequence || jobId.value !== id) return;
      if (!result.data) {
        notice.value = apiError(result.error, "job_read_failed: 无法读取任务。");
        return;
      }
      job.value = result.data;
      notice.value = `Job ${result.data.state}`;
      const eagerArtifacts = result.data.artifacts
        .filter((artifact) => EAGER_TEXT_KINDS.has(artifact.kind) || artifact.kind === "source_original");
      await Promise.all([loadSource(result.data.source_id, expected), ...eagerArtifacts.map(content.load)]);
      await loadDocument("", expected);
      if (result.data.state !== "queued" && result.data.state !== "running") stopEvents();
      return result.data;
    } catch { notice.value = "network_temporary: 无法连接 Flori，请手动刷新状态。"; return undefined; }
  }

  async function openJob(id: string, evidenceId = "", persist = true): Promise<void> {
    const expected = ++generation;
    stopEvents();
    clearArtifacts();
    resetEvidence(persist);
    jobId.value = id;
    if (persist) remember({ job_id: id, source_id: "", view: "content" });
    job.value = undefined;
    source.value = undefined;
    const current = await refreshJob(expected);
    if (generation !== expected || jobId.value !== id) return;
    if (current?.state === "queued" || current?.state === "running") startEvents(id);
    if (evidenceId) await selectEvidence(evidenceId);
  }

  function closeJob(persist = true): void {
    generation += 1;
    stopEvents();
    clearArtifacts();
    resetEvidence(persist);
    job.value = undefined;
    source.value = undefined;
    jobId.value = "";
    if (persist) remember({ job_id: "", source_id: "", view: "library" });
  }

  async function submit(): Promise<void> {
    const currentSetup = setup.value;
    const file = selectedFile.value;
    if (!currentSetup || !file) return;
    busy.value = true;
    try {
      notice.value = "正在计算 SHA-256…";
      const bytes = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      const digest = Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
      const metadata: components["schemas"]["CreateUploadSource"] = {
        request_key: uploadKey.value, kind: "pdf_upload",
        domain_id: uploadDomainId.value || currentSetup.domain_id,
        collection_ids: uploadCollectionId.value ? [uploadCollectionId.value] : [],
        file_sha256: digest, title: file.name,
      };
      notice.value = "正在上传 PDF…";
      const uploaded = await apiClient.POST("/api/v1/sources/uploads", {
        body: { metadata, file: file.name },
        bodySerializer: (body) => {
          const form = new FormData();
          form.append("metadata", new Blob([JSON.stringify(body.metadata)], { type: "application/json" }));
          form.append("file", new Blob([file], { type: "application/pdf" }), body.file);
          return form;
        },
      });
      if (!uploaded.data) {
        notice.value = apiError(uploaded.error, "upload_failed: PDF 上传失败。");
        return;
      }
      notice.value = "正在创建解析 Job…";
      const created = await apiClient.POST("/api/v1/sources/{source_id}/jobs", {
        params: { path: { source_id: uploaded.data.source_id } },
        body: { request_key: jobKey.value, pipeline_id: currentSetup.pipeline_id, inputs: { translate: false } },
      });
      if (!created.data) {
        notice.value = apiError(created.error, "job_create_failed: 无法创建 Job。");
        return;
      }
      await openJob(created.data.job_id);
    } catch { notice.value = "network_temporary: 请求未完成，请检查网络后重试。"; }
    finally { busy.value = false; }
  }

  onMounted(async () => {
    try {
      const result = await apiClient.GET("/api/v1/pdf/setup");
      setup.value = result.data;
      if (result.data && !uploadDomainId.value) uploadDomainId.value = result.data.domain_id;
      notice.value = result.data ? "请选择一个数字版 PDF。" : apiError(result.error, "setup_failed: PDF 尚未配置。");
    } catch { notice.value = "network_temporary: 无法读取 PDF 配置。"; }
    const params = new URL(window.location.href).searchParams;
    const savedJob = params.get("job_id");
    if (savedJob && params.get("view") === "content") await openJob(savedJob, params.get("evidence_id") ?? "");
  });
  onUnmounted(() => { stopEvents(); clearArtifacts(); });

  return {
    setup, selectedFile, job, source, busy, notice, evidence, activeEvidenceId, evidenceStatus,
    textContent, fileUrls, pdfUrl, videoUrl, noteText, summaryText, translationText, sourceTitle,
    documentView, documentHtml,
    chooseFile, setUploadContext, submit, selectEvidence, loadArtifact: content.load, refreshJob, openJob, closeJob,
  };
}
