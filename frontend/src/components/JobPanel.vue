<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import ArtifactPreview from "./ArtifactPreview.vue";
import PipelineDag from "./PipelineDag.vue";
import StepWorkbench from "./StepWorkbench.vue";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  mode: "pipeline" | "artifacts";
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
  selectedTaskKey: string;
  selectedArtifactId: string;
}>();
const emit = defineEmits<{
  refresh: [];
  loadArtifact: [artifact: components["schemas"]["ArtifactView"]];
  taskChange: [taskKey: string];
  artifactChange: [artifactId: string];
}>();

const orderedTasks = computed(() => {
  const remaining = [...props.job.tasks];
  const done = new Set<string>();
  const ordered: components["schemas"]["TaskView"][] = [];
  while (remaining.length > 0) {
    const ready = remaining.findIndex((task) => task.spec.needs.every((key) => done.has(key)));
    const task = remaining.splice(ready < 0 ? 0 : ready, 1)[0];
    if (!task) break;
    ordered.push(task);
    done.add(task.task_key);
  }
  return ordered;
});
const selectedTask = computed(() => props.job.tasks.find((task) => task.task_key === props.selectedTaskKey)
  ?? orderedTasks.value[0]);
const selectedArtifacts = computed(() => props.job.artifacts.filter((artifact) =>
  artifact.task_id === selectedTask.value?.task_id,
));
</script>

<template>
  <section
    v-if="mode === 'pipeline'"
    class="panel-section"
    aria-labelledby="pipeline-title"
  >
    <header class="panel-title">
      <div>
        <p class="eyebrow">
          Execution graph
        </p><h2 id="pipeline-title">
          Pipeline 流程
        </h2>
        <p class="meta">
          点击任一步骤，查看执行器、声明产物、实际产物和 Attempt。
        </p>
      </div>
      <button
        type="button"
        class="btn secondary compact"
        @click="$emit('refresh')"
      >
        刷新
      </button>
    </header>
    <p
      v-if="job.error_code"
      class="error-box"
    >
      {{ job.error_code }}: {{ job.error_message }}
    </p>
    <PipelineDag
      :tasks="job.tasks"
      :selected-task-id="selectedTask?.task_id"
      @select="emit('taskChange', $event)"
    />
    <StepWorkbench
      v-if="selectedTask"
      :task="selectedTask"
      :artifacts="selectedArtifacts"
      :text-content="textContent"
      :file-urls="fileUrls"
      :selected-artifact-id="selectedArtifactId"
      @load-artifact="$emit('loadArtifact', $event)"
      @artifact-change="emit('artifactChange', $event)"
    />
  </section>

  <section
    v-else
    class="panel-section"
    aria-labelledby="artifacts-title"
  >
    <details class="raw-artifacts">
      <summary id="artifacts-title">
        <span><b>原始产物</b><small>{{ job.artifacts.length }} 项 Artifact，按发布 Job 固化</small></span>
        <span class="summary-action">展开清单</span>
      </summary>
      <div class="artifact-list">
        <ArtifactPreview
          v-for="artifact in job.artifacts"
          :key="artifact.artifact_id"
          :artifact="artifact"
          :text="textContent.get(artifact.artifact_id)"
          :file-url="fileUrls.get(artifact.artifact_id)"
          :active="selectedArtifactId === artifact.artifact_id"
          @load="$emit('loadArtifact', $event)"
          @select="emit('artifactChange', $event)"
        />
      </div>
    </details>
  </section>
</template>
