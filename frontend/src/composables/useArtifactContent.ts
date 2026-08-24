import { reactive, type Ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";

const TEXT_KINDS = new Set<components["schemas"]["ArtifactKind"]>([
  "document_structure", "translation", "smart_note", "summary", "terms", "evidence", "task_log", "ai_audit",
  "subtitle", "transcript", "danmaku", "parts_manifest", "subscription_manifest", "mechanical_note",
  "scholarly_html", "scholarly_html_snapshot",
]);
const FILE_KINDS = new Set<components["schemas"]["ArtifactKind"]>([
  "source_original", "figure", "table_region", "keyframe", "scholarly_resource",
]);

export function useArtifactContent(activeJobId: Readonly<Ref<string>>, report: (message: string) => void) {
  const textContent = reactive(new Map<string, string>());
  const fileUrls = reactive(new Map<string, string>());

  function clear(): void {
    for (const url of fileUrls.values()) URL.revokeObjectURL(url);
    fileUrls.clear();
    textContent.clear();
  }

  async function load(artifact: components["schemas"]["ArtifactView"]): Promise<void> {
    const expectedJobId = activeJobId.value;
    if (!expectedJobId || artifact.job_id !== expectedJobId) return;
    try {
      if (TEXT_KINDS.has(artifact.kind) && !textContent.has(artifact.artifact_id)) {
        const result = await apiClient.GET("/api/v1/artifacts/{artifact_id}/content", {
          params: { path: { artifact_id: artifact.artifact_id } }, parseAs: "text",
        });
        if (activeJobId.value !== expectedJobId) return;
        if (result.data !== undefined) textContent.set(artifact.artifact_id, result.data);
        else report(apiError(result.error, "artifact_read_failed: 无法读取成果。"));
      }
      if (FILE_KINDS.has(artifact.kind) && !fileUrls.has(artifact.artifact_id)) {
        const result = await apiClient.GET("/api/v1/artifacts/{artifact_id}/content", {
          params: { path: { artifact_id: artifact.artifact_id } }, parseAs: "blob",
        });
        if (activeJobId.value !== expectedJobId) return;
        if (result.data !== undefined) fileUrls.set(artifact.artifact_id, URL.createObjectURL(result.data));
        else report(apiError(result.error, "artifact_read_failed: 无法读取成果。"));
      }
    } catch {
      if (activeJobId.value === expectedJobId) report("network_temporary: 无法读取成果，请稍后重试。");
    }
  }

  return { textContent, fileUrls, clear, load };
}
