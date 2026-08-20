<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from "vue";

import { apiClient, apiError, type components } from "./api/client";
import JobPanel from "./components/JobPanel.vue";

const setup = ref<components["schemas"]["PdfSetupView"]>();
const selectedFile = ref<File>();
const job = ref<components["schemas"]["JobView"]>();
const jobId = ref("");
const uploadKey = ref("");
const jobKey = ref("");
const busy = ref(false);
const notice = ref("正在读取 PDF 配置…");
const textContent = reactive(new Map<string, string>());
const fileUrls = reactive(new Map<string, string>());
const textKinds = new Set<components["schemas"]["ArtifactKind"]>([
  "document_structure", "translation", "smart_note", "summary", "terms", "evidence", "task_log", "ai_audit",
]);
const fileKinds = new Set<components["schemas"]["ArtifactKind"]>([
  "source_original", "figure", "table_region",
]);
let pollTimer: number | undefined;

function chooseFile(event: Event): void {
  if (event.currentTarget instanceof HTMLInputElement) {
    selectedFile.value = event.currentTarget.files?.item(0) ?? undefined;
    uploadKey.value = selectedFile.value ? crypto.randomUUID() : "";
    jobKey.value = selectedFile.value ? crypto.randomUUID() : "";
  }
}

function rememberJob(id: string): void {
  jobId.value = id;
  const url = new URL(window.location.href);
  url.searchParams.set("job_id", id);
  history.replaceState(null, "", url);
}

function clearArtifacts(): void {
  for (const url of fileUrls.values()) URL.revokeObjectURL(url);
  fileUrls.clear();
  textContent.clear();
}

async function loadArtifacts(artifacts: components["schemas"]["ArtifactView"][]): Promise<void> {
  for (const artifact of artifacts) {
    if (textKinds.has(artifact.kind) && !textContent.has(artifact.artifact_id)) {
      const result = await apiClient.GET("/api/v1/artifacts/{artifact_id}/content", {
        params: { path: { artifact_id: artifact.artifact_id } }, parseAs: "text",
      });
      if (result.data !== undefined) textContent.set(artifact.artifact_id, result.data);
      else notice.value = apiError(result.error, "artifact_read_failed: 无法读取成果。");
    }
    if (fileKinds.has(artifact.kind) && !fileUrls.has(artifact.artifact_id)) {
      const result = await apiClient.GET("/api/v1/artifacts/{artifact_id}/content", {
        params: { path: { artifact_id: artifact.artifact_id } }, parseAs: "blob",
      });
      if (result.data !== undefined) fileUrls.set(artifact.artifact_id, URL.createObjectURL(result.data));
      else notice.value = apiError(result.error, "artifact_read_failed: 无法读取成果。");
    }
  }
}

async function refreshJob(): Promise<void> {
  window.clearTimeout(pollTimer);
  if (!jobId.value) return;
  try {
    const result = await apiClient.GET("/api/v1/jobs/{job_id}", {
      params: { path: { job_id: jobId.value } },
    });
    if (!result.data) {
      notice.value = apiError(result.error, "job_read_failed: 无法读取任务。");
      return;
    }
    job.value = result.data;
    notice.value = `Job ${result.data.state}`;
    await loadArtifacts(result.data.artifacts);
    if (result.data.state === "queued" || result.data.state === "running") {
      pollTimer = window.setTimeout(() => void refreshJob(), 2000);
    }
  } catch {
    notice.value = "network_temporary: 无法连接 Flori，请手动刷新状态。";
  }
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
      request_key: uploadKey.value, kind: "pdf_upload", domain_id: currentSetup.domain_id,
      collection_ids: [], file_sha256: digest, title: file.name,
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
    clearArtifacts();
    rememberJob(created.data.job_id);
    await refreshJob();
  } catch {
    notice.value = "network_temporary: 请求未完成，请检查网络后重试。";
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  try {
    const result = await apiClient.GET("/api/v1/pdf/setup");
    setup.value = result.data;
    notice.value = result.data ? "请选择一个数字版 PDF。" : apiError(result.error, "setup_failed: PDF 尚未配置。");
  } catch { notice.value = "network_temporary: 无法读取 PDF 配置。"; }
  const saved = new URL(window.location.href).searchParams.get("job_id");
  if (saved) { rememberJob(saved); await refreshJob(); }
});
onUnmounted(() => { window.clearTimeout(pollTimer); clearArtifacts(); });
</script>

<template>
  <main class="shell">
    <header>
      <h1>PDF 解析</h1><p>上传数字版 PDF，生成可追溯的智能笔记与证据。</p>
    </header>
    <section
      class="card"
      aria-labelledby="upload-title"
    >
      <h2 id="upload-title">
        开始解析
      </h2>
      <label for="pdf-file">PDF 文件</label>
      <input
        id="pdf-file"
        type="file"
        accept="application/pdf,.pdf"
        :disabled="busy"
        @change="chooseFile"
      >
      <p class="hint">
        首次流程固定不翻译全文；文件摘要在浏览器内计算。
      </p>
      <button
        type="button"
        :disabled="!selectedFile || !setup || busy"
        @click="submit"
      >
        {{ busy ? "处理中…" : "上传并解析" }}
      </button>
      <p
        class="notice"
        aria-live="polite"
      >
        {{ notice }}
      </p>
    </section>
    <JobPanel
      v-if="job"
      :job="job"
      :text-content="textContent"
      :file-urls="fileUrls"
      @refresh="refreshJob"
    />
  </main>
</template>
