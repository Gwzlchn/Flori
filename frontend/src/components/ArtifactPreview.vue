<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import MarkdownContent from "./MarkdownContent.vue";

type ArtifactKind = components["schemas"]["ArtifactKind"];
type PreviewKind = "markdown" | "json" | "ndjson" | "image" | "media" | "html";

const props = defineProps<{
  artifact: components["schemas"]["ArtifactView"];
  text: string | undefined;
  fileUrl: string | undefined;
}>();
const emit = defineEmits<{ load: [artifact: components["schemas"]["ArtifactView"]] }>();

const previewKinds = {
  source_original: "media", document_structure: "json", figure: "image", table_region: "image",
  translation: "markdown", subtitle: "ndjson", transcript: "json", keyframe: "image", danmaku: "ndjson",
  parts_manifest: "json", subscription_manifest: "json", mechanical_note: "markdown",
  smart_note: "markdown", summary: "markdown", terms: "json", evidence: "json", task_log: "ndjson",
  ai_audit: "json", scholarly_html: "html", scholarly_html_snapshot: "json", scholarly_resource: "image",
} satisfies Record<ArtifactKind, PreviewKind>;

const preview = computed(() => previewKinds[props.artifact.kind]);
const loaded = computed(() => props.text !== undefined || props.fileUrl !== undefined);
const tooLarge = computed(() => preview.value !== "image" && preview.value !== "media"
  && props.artifact.size_bytes > 1024 * 1024);

function open(event: Event): void {
  if (event.currentTarget instanceof HTMLDetailsElement && event.currentTarget.open && !loaded.value && !tooLarge.value) {
    emit("load", props.artifact);
  }
}

function sizeLabel(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.ceil(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
</script>

<template>
  <details
    class="artifact-preview"
    @toggle="open"
  >
    <summary>
      <span><b>{{ artifact.name }}</b><small>{{ artifact.kind }} · {{ sizeLabel(artifact.size_bytes) }}</small></span>
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
      >
      <object
        v-else-if="preview === 'media' && artifact.media_type === 'application/pdf' && fileUrl"
        :data="fileUrl"
        type="application/pdf"
      ><a :href="fileUrl">打开 PDF</a></object>
      <video
        v-else-if="preview === 'media' && artifact.media_type.startsWith('video/') && fileUrl"
        :src="fileUrl"
        controls
      />
      <pre
        v-else-if="text !== undefined"
        :class="{ 'html-source': preview === 'html' }"
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
      产物超过 1 MiB，只保留下载与摘要信息，避免浏览器载入大日志。
    </p>
    <p
      v-else
      class="preview-loading"
    >
      正在读取并校验产物…
    </p>
  </details>
</template>

<style scoped>
.artifact-preview { overflow: hidden; border: 1px solid var(--line); border-radius: 6px; background: white; }
summary { display: flex; align-items: center; justify-content: space-between; padding: 9px 10px; cursor: pointer; }
summary > span:first-child { display: grid; gap: 2px; }small,.preview-loading { color: var(--muted); }
.preview-body { padding: 0 10px 10px; }.preview-body img,.preview-body object,.preview-body video { display: block; width: 100%; max-height: 440px; object-fit: contain; background: var(--surface-soft); }
pre { max-height: 460px; margin: 0; padding: 10px; overflow: auto; border-radius: 5px; color: #e7e6e2; background: #37352f; font-size: 11px; white-space: pre-wrap; }
.html-source { white-space: pre; }.preview-loading { margin: 0; padding: 10px; }
</style>
