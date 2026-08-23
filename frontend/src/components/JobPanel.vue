<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  mode: "pipeline" | "artifacts";
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
}>();
defineEmits<{ refresh: [] }>();

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

function sizeLabel(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
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
        </p><h3 id="pipeline-title">
          Pipeline 运行状态
        </h3>
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
    <ol class="timeline">
      <li
        v-for="task in orderedTasks"
        :key="task.task_id"
      >
        <span
          class="timeline-dot"
          :class="`state-${task.state}`"
        />
        <div class="task-card">
          <header>
            <strong>{{ task.task_key }}</strong><span
              class="status-pill"
              :class="`state-${task.state}`"
            >{{ task.state }}</span>
          </header>
          <p>{{ task.executor }} · 依赖 {{ task.spec.needs.join(", ") || "无" }}</p>
          <p
            v-if="task.error_code"
            class="error-text"
          >
            {{ task.error_code }}: {{ task.error_message }}
          </p>
          <details v-if="task.attempts.length">
            <summary>{{ task.attempts.length }} 次 Attempt</summary>
            <ul class="attempt-list">
              <li
                v-for="attempt in task.attempts"
                :key="attempt.attempt_id"
              >
                #{{ attempt.attempt_no }} {{ attempt.state }}<span v-if="attempt.runner_id"> · Runner {{ attempt.runner_id.slice(0, 8) }}</span>
                <span
                  v-if="attempt.error_code"
                  class="error-text"
                > · {{ attempt.error_code }}</span>
              </li>
            </ul>
          </details>
        </div>
      </li>
    </ol>
  </section>

  <section
    v-else
    class="panel-section"
    aria-labelledby="artifacts-title"
  >
    <div class="panel-title">
      <div>
        <p class="eyebrow">
          Canonical outputs
        </p><h3 id="artifacts-title">
          原始产物
        </h3>
      </div>
    </div>
    <p class="meta">
      {{ job.artifacts.length }} 项 Artifact，按发布 Job 固化。
    </p>
    <div class="artifact-list">
      <details
        v-for="artifact in job.artifacts"
        :key="artifact.artifact_id"
        class="artifact-row"
      >
        <summary>
          <span><b>{{ artifact.kind }}</b><small>{{ artifact.name }}</small></span>
          <span>{{ sizeLabel(artifact.size_bytes) }}</span>
        </summary>
        <a
          v-if="artifact.kind === 'source_original' && fileUrls.get(artifact.artifact_id)"
          :href="fileUrls.get(artifact.artifact_id)"
          download="source.pdf"
        >下载原始 PDF</a>
        <img
          v-else-if="(artifact.kind === 'figure' || artifact.kind === 'table_region') && fileUrls.get(artifact.artifact_id)"
          :src="fileUrls.get(artifact.artifact_id)"
          :alt="`${artifact.kind}: ${artifact.name}`"
        >
        <pre v-else-if="textContent.has(artifact.artifact_id)">{{ textContent.get(artifact.artifact_id) }}</pre>
        <p
          v-else
          class="meta"
        >
          此产物没有可内嵌预览。
        </p>
      </details>
    </div>
  </section>
</template>
