<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";

import type { components } from "../api/client";

const props = defineProps<{
  html: string;
  resources: components["schemas"]["ArtifactView"][];
  fileUrls: ReadonlyMap<string, string>;
  anchor: string | undefined;
}>();
const source = ref("");
let blobUrl = "";

function release(): void {
  if (blobUrl) URL.revokeObjectURL(blobUrl);
  blobUrl = "";
}
function rebuild(): void {
  release();
  const document = new DOMParser().parseFromString(props.html, "text/html");
  const resources = new Map(props.resources.map((artifact) => [artifact.name, props.fileUrls.get(artifact.artifact_id)]));
  for (const image of document.querySelectorAll<HTMLImageElement>("img[data-flori-resource]")) {
    const name = image.dataset.floriResource;
    const url = name ? resources.get(name) : undefined;
    if (url) image.src = url;
    else image.remove();
  }
  blobUrl = URL.createObjectURL(new Blob([`<!doctype html>${document.documentElement.outerHTML}`], { type: "text/html" }));
  source.value = props.anchor ? `${blobUrl}#${encodeURIComponent(props.anchor)}` : blobUrl;
}

watch(() => [props.html, props.resources, props.fileUrls, props.anchor] as const, rebuild, { immediate: true, deep: true });
onUnmounted(release);
</script>

<template>
  <div>
    <a
      class="html-window-link"
      :href="source"
      target="_blank"
      rel="noopener"
    >新窗口打开 HTML</a>
    <iframe
      :key="source"
      class="scholarly-viewer"
      :src="source"
      title="学术原文"
      sandbox=""
      referrerpolicy="no-referrer"
    />
  </div>
</template>

<style scoped>
.html-window-link { display: block; margin: 0 0 7px auto; width: max-content; font-size: 12px; }
.scholarly-viewer { width: 100%; height: min(72vh, 760px); border: 1px solid var(--line); border-radius: 5px; background: white; }
</style>
