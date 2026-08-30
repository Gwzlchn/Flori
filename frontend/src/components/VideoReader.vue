<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import type { components } from "../api/client";
import ArtifactReaderPanel from "./ArtifactReaderPanel.vue";
import JobPanel from "./JobPanel.vue";
import MetadataPanel from "./MetadataPanel.vue";
import UiIcon from "./UiIcon.vue";

type ReaderTab = "artifacts" | "pipeline" | "metadata" | "visuals";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
  domainName: string | undefined;
  collectionNames: string[];
  note: string | undefined;
  summary: string | undefined;
  videoUrl: string | undefined;
  evidence: components["schemas"]["EvidenceView"] | undefined;
  activeEvidenceId: string;
  status: string;
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
  tab: ReaderTab;
  selectedTaskKey: string;
  selectedArtifactId: string;
}>();
const emit = defineEmits<{
  select: [evidenceId: string]; refresh: []; loadArtifact: [artifact: components["schemas"]["ArtifactView"]];
  tabChange: [tab: ReaderTab]; taskChange: [taskKey: string]; artifactChange: [artifactId: string];
}>();
const player = ref<HTMLVideoElement>();
const artifactView = ref<"alternate" | "notes" | "source">("notes");
const locator = computed(() => props.evidence?.locator.kind === "video" ? props.evidence.locator.value : undefined);
const mechanical = computed(() => {
  const artifact = props.job.artifacts.find((item) => item.kind === "mechanical_note");
  return artifact ? props.textContent.get(artifact.artifact_id) : undefined;
});
const keyframes = computed(() => props.job.artifacts.filter((item) => item.kind === "keyframe"));
const showContext = computed(() => props.tab === "visuals" || (props.tab === "artifacts"
  && artifactView.value === "notes" && Boolean(props.activeEvidenceId)));
const tabs = [
  { id: "artifacts", label: "产物", icon: "book" },
  { id: "pipeline", label: "流水线", icon: "pipeline" },
  { id: "metadata", label: "元信息", icon: "info" },
  { id: "visuals", label: "关键帧", icon: "image" },
] as const;

function seek(milliseconds: number): void {
  if (!player.value) return;
  player.value.currentTime = milliseconds / 1000;
  player.value.scrollIntoView({ behavior: "smooth", block: "center" });
}
function timestamp(name: string): number | undefined {
  const match = /^frames\/(\d{13})\.jpg$/.exec(name);
  return match?.[1] ? Number(match[1]) : undefined;
}
function formatTime(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function selectTab(tab: ReaderTab): void {
  emit("tabChange", tab);
  if (tab === "visuals") for (const frame of keyframes.value) emit("loadArtifact", frame);
}
watch(locator, async (value) => {
  if (!value) return;
  if (value.keyframe) {
    const frame = keyframes.value.find((item) => item.artifact_id === value.keyframe?.artifact_id);
    if (frame) emit("loadArtifact", frame);
  }
  await nextTick();
  seek(value.start_ms);
});
</script>

<template>
  <section
    class="reader-card card"
    aria-label="视频阅读工作台"
  >
    <div class="reader-heading">
      <div
        class="reader-tabs"
        role="tablist"
        aria-label="视频阅读工作台"
      >
        <button
          v-for="item in tabs"
          :key="item.id"
          type="button"
          role="tab"
          :aria-selected="tab === item.id"
          @click="selectTab(item.id)"
        >
          <UiIcon
            :name="item.icon"
            :size="15"
          />{{ item.label }}
        </button>
      </div>
    </div>
    <div
      class="reader-layout"
      :class="{ 'has-context': showContext }"
    >
      <div class="reader-content">
        <section
          v-if="tab === 'artifacts'"
          class="knowledge-output"
        >
          <ArtifactReaderPanel
            :view="artifactView"
            source-label="视频原文"
            alternate-label="机械版"
            :alternate-content="mechanical"
            evidence-intro="引用标记仅会定位到经 Rust 校验的时间证据。"
            :job="job"
            :note="note"
            :summary="summary"
            :active-evidence-id="activeEvidenceId"
            :text-content="textContent"
            :file-urls="fileUrls"
            :selected-task-key="selectedTaskKey"
            :selected-artifact-id="selectedArtifactId"
            @view-change="artifactView = $event"
            @select="emit('select', $event)"
            @load-artifact="emit('loadArtifact', $event)"
            @refresh="emit('refresh')"
            @task-change="emit('taskChange', $event)"
            @artifact-change="emit('artifactChange', $event)"
          >
            <template #source>
              <section class="source-reader">
                <p class="source-reader-hint">
                  视频原件按实际比例显示，字幕与 Evidence 使用毫秒定位。
                </p>
                <video
                  v-if="videoUrl"
                  ref="player"
                  class="video-player source-video"
                  :src="videoUrl"
                  controls
                  preload="metadata"
                />
                <p
                  v-else
                  class="empty-state"
                >
                  缺少已验证的视频原件。
                </p>
              </section>
            </template>
          </ArtifactReaderPanel>
        </section>
        <section
          v-else-if="tab === 'visuals'"
          class="frame-panel"
        >
          <header class="panel-intro">
            <p class="eyebrow">
              Keyframes
            </p><h2>关键帧时间轴</h2>
            <p class="meta">
              图像保持原始宽高比；点击后跳到对应视频时间。
            </p>
          </header>
          <div class="frame-grid">
            <button
              v-for="frame in keyframes"
              :key="frame.artifact_id"
              type="button"
              @click="timestamp(frame.name) !== undefined && seek(timestamp(frame.name) ?? 0)"
            >
              <img
                v-if="fileUrls.get(frame.artifact_id)"
                :src="fileUrls.get(frame.artifact_id)"
                :alt="frame.name"
              >
              <span>{{ timestamp(frame.name) === undefined ? frame.name : formatTime(timestamp(frame.name) ?? 0) }}</span>
            </button>
          </div>
        </section>
        <JobPanel
          v-else-if="tab === 'pipeline'"
          :job="job"
          mode="pipeline"
          :text-content="textContent"
          :file-urls="fileUrls"
          :selected-task-key="selectedTaskKey"
          :selected-artifact-id="selectedArtifactId"
          @load-artifact="emit('loadArtifact', $event)"
          @refresh="emit('refresh')"
          @task-change="emit('taskChange', $event)"
          @artifact-change="emit('artifactChange', $event)"
        />
        <MetadataPanel
          v-else
          :job="job"
          :source="source"
          :domain-name="domainName"
          :collection-names="collectionNames"
          :document-view="undefined"
        />
      </div>
      <aside
        v-if="showContext"
        class="evidence-panel"
        aria-label="视频证据定位"
      >
        <div class="evidence-head">
          <span><i /> 视频时间证据</span>
        </div>
        <p
          class="evidence-status"
          aria-live="polite"
        >
          {{ status }}
        </p>
        <blockquote v-if="evidence && locator">
          {{ evidence.quote }}
        </blockquote>
        <dl
          v-if="locator"
          class="locator-grid"
        >
          <div><dt>开始</dt><dd>{{ formatTime(locator.start_ms) }}</dd></div>
          <div><dt>结束</dt><dd>{{ formatTime(locator.end_ms) }}</dd></div>
        </dl>
        <video
          v-if="videoUrl"
          ref="player"
          class="video-player"
          :src="videoUrl"
          controls
          preload="metadata"
        />
        <p
          v-else
          class="empty-state"
        >
          缺少已验证的视频原件。
        </p>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.frame-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 12px; }
.frame-grid button { display: grid; gap: 7px; padding: 8px; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--surface); color: var(--muted); text-align: left; cursor: pointer; }
.frame-grid button:hover,.frame-grid button:focus-visible { border-color: var(--brand); box-shadow: var(--focus-ring); }
.frame-grid img { width: 100%; aspect-ratio: 16/9; object-fit: contain; background: var(--surface-soft); }
.video-player { display: block; width: 100%; max-height: calc(100vh - 250px); background: #111; }
</style>
