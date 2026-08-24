import { ref, type Ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";

const HINT = "点击笔记中的证据引用可定位原文。";

export function isVideoSource(kind: components["schemas"]["SourceKind"]): boolean {
  return kind === "local_video" || kind === "bilibili_video" || kind === "youtube_video";
}

export function useEvidenceNavigation(
  job: Ref<components["schemas"]["JobView"] | undefined>,
  source: Ref<components["schemas"]["SourceView"] | undefined>,
  original: Ref<components["schemas"]["ArtifactView"] | undefined>,
  remember: (evidenceId: string) => void,
  afterPdf: (evidenceId: string) => Promise<void>,
) {
  const evidence = ref<components["schemas"]["EvidenceView"]>();
  const activeEvidenceId = ref("");
  const evidenceStatus = ref(HINT);

  function reset(persist = true): void {
    evidence.value = undefined;
    activeEvidenceId.value = "";
    evidenceStatus.value = HINT;
    if (persist) remember("");
  }

  async function select(id: string): Promise<void> {
    const current = job.value;
    const currentSource = source.value;
    const sourceArtifact = original.value;
    activeEvidenceId.value = id;
    remember(id);
    if (!current || !currentSource) return;
    if (!sourceArtifact) {
      evidenceStatus.value = "artifact_missing: 缺少原始内容，无法定位证据。";
      return;
    }
    evidenceStatus.value = "正在读取证据…";
    try {
      const result = await apiClient.GET("/api/v1/evidence/{evidence_id}", {
        params: { path: { evidence_id: id } },
      });
      if (activeEvidenceId.value !== id || job.value?.job_id !== current.job_id) return;
      if (!result.data) {
        evidenceStatus.value = apiError(result.error, "evidence_read_failed: 无法读取证据。");
        return;
      }
      const view = result.data;
      const locatorMatches = isVideoSource(currentSource.kind)
        ? view.locator.kind === "video" && sourceArtifact.media_type.startsWith("video/")
        : view.locator.kind === "pdf" && sourceArtifact.media_type === "application/pdf";
      if (view.job_id !== current.job_id || view.source_id !== current.source_id
        || view.source_artifact_id !== sourceArtifact.artifact_id || !locatorMatches) {
        evidenceStatus.value = "evidence_mismatch: 该证据不属于当前内容，已拒绝跳转。";
        return;
      }
      evidence.value = view;
      evidenceStatus.value = view.locator.kind === "video"
        ? `已定位 ${formatTime(view.locator.value.start_ms)}。`
        : `已定位第 ${view.locator.value.page} 页。`;
      if (view.locator.kind === "pdf") await afterPdf(id);
    } catch {
      if (activeEvidenceId.value === id && job.value?.job_id === current.job_id) {
        evidenceStatus.value = "network_temporary: 无法读取证据，请稍后重试。";
      }
    }
  }

  return { evidence, activeEvidenceId, evidenceStatus, reset, select };
}

function formatTime(value: number): string {
  const seconds = Math.floor(value / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
