<script setup lang="ts">
import { computed, ref } from "vue";

import { apiClient, type components } from "../api/client";
import MarkdownContent from "./MarkdownContent.vue";

type ArtifactKind = components["schemas"]["ArtifactKind"];
type PreviewKind = "markdown" | "json" | "ndjson" | "image" | "media" | "html";

const props = defineProps<{
  artifact: components["schemas"]["ArtifactView"];
  text: string | undefined;
  fileUrl: string | undefined;
  active: boolean;
}>();
const emit = defineEmits<{
  load: [artifact: components["schemas"]["ArtifactView"]];
  select: [artifactId: string];
}>();

const previewKinds = {
  source_original: "media", document_structure: "json", figure: "image", table_region: "image",
  translation: "markdown", subtitle: "ndjson", transcript: "json", keyframe: "image", danmaku: "ndjson",
  parts_manifest: "json", subscription_manifest: "json", mechanical_note: "markdown",
  smart_note: "markdown", summary: "markdown", terms: "json", evidence: "json", task_log: "ndjson",
  ai_audit: "json", scholarly_html: "html", scholarly_html_snapshot: "json", scholarly_resource: "image",
} satisfies Record<ArtifactKind, PreviewKind>;
const artifactLabels = {
  source_original: "原始输入", document_structure: "文档结构", figure: "Figure", table_region: "Table 区域",
  translation: "全文翻译", subtitle: "字幕", transcript: "Transcript", keyframe: "关键帧", danmaku: "弹幕",
  parts_manifest: "分片清单", subscription_manifest: "订阅清单", mechanical_note: "机械笔记",
  smart_note: "智能笔记", summary: "中文摘要", terms: "关键术语", evidence: "Evidence",
  task_log: "任务日志", ai_audit: "AI 审计", scholarly_html: "学术 HTML",
  scholarly_html_snapshot: "HTML 快照清单", scholarly_resource: "HTML 资源",
} satisfies Record<ArtifactKind, string>;

const preview = computed(() => previewKinds[props.artifact.kind]);
const loaded = computed(() => props.text !== undefined || props.fileUrl !== undefined);
const tooLarge = computed(() => preview.value !== "image" && preview.value !== "media"
  && props.artifact.size_bytes > 1024 * 1024);
const downloading = ref(false);
const downloadError = ref("");

function open(event: Event): void {
  if (!(event.currentTarget instanceof HTMLDetailsElement)) return;
  emit("select", event.currentTarget.open ? props.artifact.artifact_id : "");
  if (event.currentTarget.open && !loaded.value && !tooLarge.value) {
    emit("load", props.artifact);
  }
}

function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

async function download(): Promise<void> {
  downloading.value = true;
  downloadError.value = "";
  try {
    const result = await apiClient.GET("/api/v1/artifacts/{artifact_id}/content", {
      params: { path: { artifact_id: props.artifact.artifact_id } }, parseAs: "blob",
    });
    if (!result.data) {
      downloadError.value = result.error?.error.message ?? "下载失败";
      return;
    }
    const url = URL.createObjectURL(result.data);
    const link = document.createElement("a");
    link.href = url;
    link.download = props.artifact.name;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch {
    downloadError.value = "网络请求失败";
  } finally {
    downloading.value = false;
  }
}
</script>

<template>
  <details
    class="artifact-preview"
    :open="active"
    @toggle="open"
  >
    <summary>
      <span><b>{{ artifact.name }}</b><small>{{ artifactLabels[artifact.kind] }} · {{ artifact.kind }} · {{ sizeLabel(artifact.size_bytes) }}</small></span>
      <span>查看</span>
    </summary>
    <div
      v-if="loaded"
      class="preview-body"
    >
      <MarkdownContent
        v-if="preview === 'markdown' && text !== undefined"
        :content="text"
        active-evidence-id=""
      />
      <img
        v-else-if="preview === 'image' && fileUrl"
        :src="fileUrl"
        :alt="artifact.name"
        loading="lazy"
      >
      <div
        v-else-if="preview === 'media' && artifact.media_type === 'application/pdf' && fileUrl"
        class="pdf-preview"
      >
        <object
          :data="fileUrl"
          type="application/pdf"
        ><a :href="fileUrl">打开 PDF</a></object>
        <a
          :href="fileUrl"
          target="_blank"
          rel="noopener"
        >在新标签打开 PDF</a>
      </div>
      <video
        v-else-if="preview === 'media' && artifact.media_type.startsWith('video/') && fileUrl"
        :src="fileUrl"
        controls
        preload="metadata"
      />
      <pre
        v-else-if="text !== undefined"
        :class="{ 'html-source': preview === 'html' }"
        :aria-label="`${artifactLabels[artifact.kind]}内容`"
      >{{ text }}</pre>
      <a
        v-else-if="fileUrl"
        :href="fileUrl"
        :download="artifact.name"
      >下载此产物</a>
    </div>
    <p
      v-else-if="tooLarge"
      class="preview-loading"
    >
      产物超过 1 MiB，不在页面载入以避免阻塞浏览器。
      <button
        type="button"
        :disabled="downloading"
        @click="download"
      >
        {{ downloading ? "下载中…" : "单独下载" }}
      </button>
      <span v-if="downloadError">{{ downloadError }}</span>
    </p>
    <p
      v-else
      class="preview-loading"
    >
      正在读取并校验产物…
    </p>
    <dl class="artifact-meta">
      <div><dt>Media type</dt><dd>{{ artifact.media_type }}</dd></div>
      <div>
        <dt>SHA-256</dt><dd :title="artifact.sha256">
          {{ artifact.sha256.slice(0, 16) }}…
        </dd>
      </div>
      <div><dt>Artifact ID</dt><dd>{{ artifact.artifact_id }}</dd></div>
    </dl>
  </details>
</template>

<style scoped>
.artifact-preview { overflow: hidden; border: 1px solid var(--line); border-radius: 6px; background: white; }
summary { display: flex; align-items: center; justify-content: space-between; padding: 9px 10px; cursor: pointer; }
summary > span:first-child { display: grid; gap: 2px; }small,.preview-loading { color: var(--muted); }
.preview-body { padding: 0 10px 10px; }.preview-body img,.preview-body object,.preview-body video { display: block; width: 100%; max-height: 440px; object-fit: contain; background: var(--surface-soft); }
.pdf-preview { display: grid; gap: 7px; }.pdf-preview a { justify-self: start; font-size: 11px; }
pre { max-height: 460px; margin: 0; padding: 10px; overflow: auto; border-radius: 5px; color: #e7e6e2; background: #37352f; font-size: 11px; white-space: pre-wrap; }
.html-source { white-space: pre; }.preview-loading { margin: 0; padding: 10px; }.preview-loading button,.preview-loading span { margin-left: 5px; }
.artifact-meta { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 6px; margin: 0; padding: 9px 10px; border-top: 1px solid var(--line); background: var(--surface-soft); }
.artifact-meta div { min-width: 0; }.artifact-meta dt { color: var(--muted); font-size: 9px; text-transform: uppercase; }.artifact-meta dd { overflow: hidden; margin: 2px 0 0; color: var(--ink); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
</style>
