<script setup lang="ts">
import type { components } from "../api/client";
import JobPanel from "./JobPanel.vue";
import MarkdownContent from "./MarkdownContent.vue";

type ArtifactView = "alternate" | "notes" | "source";
defineProps<{
  activeEvidenceId: string;
  alternateContent: string | undefined;
  alternateLabel: string;
  evidenceIntro: string;
  fileUrls: ReadonlyMap<string, string>;
  job: components["schemas"]["JobView"];
  note: string | undefined;
  selectedArtifactId: string;
  selectedTaskKey: string;
  sourceLabel: string;
  summary: string | undefined;
  textContent: ReadonlyMap<string, string>;
  view: ArtifactView;
}>();
const emit = defineEmits<{
  artifactChange: [artifactId: string];
  loadArtifact: [artifact: components["schemas"]["ArtifactView"]];
  refresh: [];
  select: [evidenceId: string];
  taskChange: [taskKey: string];
  viewChange: [view: ArtifactView];
}>();
</script>

<template>
  <nav
    class="artifact-toolbar"
    aria-label="产物阅读方式"
  >
    <button
      type="button"
      :aria-pressed="view === 'notes'"
      @click="emit('viewChange', 'notes')"
    >
      智能版
    </button>
    <button
      type="button"
      :aria-pressed="view === 'source'"
      @click="emit('viewChange', 'source')"
    >
      {{ sourceLabel }}
    </button>
    <button
      v-if="alternateContent"
      type="button"
      :aria-pressed="view === 'alternate'"
      @click="emit('viewChange', 'alternate')"
    >
      {{ alternateLabel }}
    </button>
  </nav>
  <template v-if="view === 'notes'">
    <header class="output-heading">
      <p class="eyebrow">
        Knowledge result
      </p><h2>智能笔记</h2>
      <p class="meta">
        {{ evidenceIntro }}
      </p>
    </header>
    <MarkdownContent
      v-if="note"
      :content="note"
      :active-evidence-id="activeEvidenceId"
      @select="emit('select', $event)"
    />
    <p
      v-else
      class="empty-state"
    >
      智能笔记尚未生成。Pipeline 完成后会在这里显示。
    </p>
    <section class="summary-note">
      <p class="eyebrow">
        中文摘要
      </p>
      <MarkdownContent
        v-if="summary"
        :content="summary"
        :active-evidence-id="activeEvidenceId"
        @select="emit('select', $event)"
      />
      <p
        v-else
        class="empty-state"
      >
        当前成果没有摘要。
      </p>
    </section>
    <JobPanel
      :job="job"
      mode="artifacts"
      :text-content="textContent"
      :file-urls="fileUrls"
      :selected-task-key="selectedTaskKey"
      :selected-artifact-id="selectedArtifactId"
      @load-artifact="emit('loadArtifact', $event)"
      @refresh="emit('refresh')"
      @task-change="emit('taskChange', $event)"
      @artifact-change="emit('artifactChange', $event)"
    />
  </template>
  <section
    v-else-if="view === 'alternate' && alternateContent"
    class="translation-view"
  >
    <p class="eyebrow">
      Additional result
    </p><h2>{{ alternateLabel }}</h2>
    <MarkdownContent
      :content="alternateContent"
      :active-evidence-id="activeEvidenceId"
      @select="emit('select', $event)"
    />
  </section>
  <slot
    v-else
    name="source"
  />
</template>
