<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../api/client";
import ArtifactPreview from "./ArtifactPreview.vue";
import PipelineDag from "./PipelineDag.vue";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  mode: "pipeline" | "artifacts";
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
}>();
defineEmits<{
  refresh: [];
  loadArtifact: [artifact: components["schemas"]["ArtifactView"]];
}>();
const selectedTaskKey = ref("");

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
const selectedTask = computed(() => props.job.tasks.find((task) => task.task_key === selectedTaskKey.value)
  ?? orderedTasks.value[0]);
const selectedArtifacts = computed(() => props.job.artifacts.filter((artifact) =>
  artifact.task_id === selectedTask.value?.task_id,
));
const taskLabels: Record<string, string> = {
  acquire: "获取 PDF", extract: "解析文档", note: "生成智能笔记", translate: "全文翻译",
  validate: "校验证据", publish: "发布成果",
};
const stateLabels: Record<components["schemas"]["TaskState"], string> = {
  pending: "等待", ready: "就绪", leased: "执行中", succeeded: "完成", failed: "失败", skipped: "复用", canceled: "取消",
};

watch(() => props.job.job_id, () => { selectedTaskKey.value = orderedTasks.value[0]?.task_key ?? ""; }, { immediate: true });

function taskLabel(key: string): string { return taskLabels[key] ?? key; }
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
      @select="selectedTaskKey = $event"
    />
    <article
      v-if="selectedTask"
      class="step-workbench"
    >
      <header>
        <div>
          <p class="eyebrow">
            Selected step
          </p><h2>{{ taskLabel(selectedTask.task_key) }}</h2>
        </div>
        <span
          class="status-pill"
          :class="`state-${selectedTask.state}`"
        >{{ stateLabels[selectedTask.state] }}</span>
      </header>
      <p class="meta">
        {{ selectedTask.executor }} · 依赖 {{ selectedTask.spec.needs.join(", ") || "无" }} · timeout {{ selectedTask.spec.timeout_ms }}ms
      </p>
      <p
        v-if="selectedTask.error_code"
        class="error-box"
      >
        {{ selectedTask.error_code }}: {{ selectedTask.error_message }}
      </p>
      <div class="step-columns">
        <section>
          <h3>声明产物</h3><ul class="compact-list declaration-list">
            <li
              v-for="artifact in selectedTask.spec.artifacts"
              :key="artifact.name"
            >
              <b>{{ artifact.name }}</b><span>{{ artifact.kind }} · {{ artifact.when }}</span>
            </li>
          </ul>
        </section>
        <section>
          <h3>实际产物</h3><div class="step-artifacts">
            <ArtifactPreview
              v-for="artifact in selectedArtifacts"
              :key="artifact.artifact_id"
              :artifact="artifact"
              :text="textContent.get(artifact.artifact_id)"
              :file-url="fileUrls.get(artifact.artifact_id)"
              @load="$emit('loadArtifact', $event)"
            /><p
              v-if="!selectedArtifacts.length"
              class="meta"
            >
              尚无产物
            </p>
          </div>
        </section>
      </div>
      <details
        v-if="selectedTask.attempts.length"
        open
      >
        <summary>{{ selectedTask.attempts.length }} 次 Attempt</summary>
        <ul class="attempt-list">
          <li
            v-for="attempt in selectedTask.attempts"
            :key="attempt.attempt_id"
          >
            #{{ attempt.attempt_no }} {{ attempt.state }}<span v-if="attempt.runner_id"> · Runner {{ attempt.runner_id.slice(0, 8) }}</span>
            <span v-if="attempt.model"> · {{ attempt.model }} / {{ attempt.effort }}</span>
            <span
              v-if="attempt.error_code"
              class="error-text"
            > · {{ attempt.error_code }}</span>
          </li>
        </ul>
      </details>
    </article>
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
          @load="$emit('loadArtifact', $event)"
        />
      </div>
    </details>
  </section>
</template>
