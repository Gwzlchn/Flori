<script setup lang="ts">
import { computed, ref } from "vue";
import { apiClient, apiError, type components } from "../api/client";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
}>();
const emit = defineEmits<{ refresh: [] }>();
const lifecycleBusy = ref(false);
const lifecycleStatus = ref("");

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

async function cancelJob(): Promise<void> {
  if (!window.confirm("确定取消这个 Job？正在运行的 Attempt 会立即失效。")) return;
  lifecycleBusy.value = true;
  try {
    const result = await apiClient.POST("/api/v1/jobs/{job_id}/cancel", {
      params: { path: { job_id: props.job.job_id } },
    });
    lifecycleStatus.value = result.response.ok
      ? "Job 已取消。"
      : apiError(result.error, "cancel_failed: 无法取消 Job。");
    if (result.response.ok) emit("refresh");
  } catch { lifecycleStatus.value = "network_temporary: 取消请求未完成。"; }
  finally { lifecycleBusy.value = false; }
}

async function deleteSource(): Promise<void> {
  if (!window.confirm("确定完整删除这个 Source 及全部历史成果？此操作不可撤销。")) return;
  lifecycleBusy.value = true;
  try {
    const result = await apiClient.DELETE("/api/v1/sources/{source_id}", {
      params: { path: { source_id: props.job.source_id } },
    });
    if (result.response.ok) window.location.assign(window.location.pathname);
    else lifecycleStatus.value = apiError(result.error, "delete_failed: 无法删除 Source。");
  } catch { lifecycleStatus.value = "network_temporary: 删除请求未完成。"; }
  finally { lifecycleBusy.value = false; }
}
</script>

<template>
  <section
    class="card"
    aria-labelledby="job-title"
  >
    <div class="section-head">
      <div>
        <p class="eyebrow">
          Job {{ job.job_id }}
        </p><h2 id="job-title">
          解析状态：{{ job.state }}
        </h2>
      </div>
      <div class="actions">
        <button
          type="button"
          class="secondary"
          @click="$emit('refresh')"
        >
          刷新状态
        </button>
        <button
          v-if="job.state === 'queued' || job.state === 'running'"
          type="button"
          :disabled="lifecycleBusy"
          @click="cancelJob"
        >
          取消 Job
        </button>
        <button
          type="button"
          :disabled="lifecycleBusy"
          @click="deleteSource"
        >
          删除 Source
        </button>
      </div>
    </div>
    <p
      v-if="job.error_code"
      class="error"
    >
      {{ job.error_code }}: {{ job.error_message }}
    </p>
    <p
      v-if="lifecycleStatus"
      class="notice"
      aria-live="polite"
    >
      {{ lifecycleStatus }}
    </p>
    <ol class="timeline">
      <li
        v-for="task in orderedTasks"
        :key="task.task_id"
      >
        <div class="task-head">
          <strong>{{ task.task_key }}</strong><span class="badge">{{ task.state }}</span>
        </div>
        <p>{{ task.executor }} · 依赖：{{ task.spec.needs.join(", ") || "无" }}</p>
        <p
          v-if="task.error_code"
          class="error"
        >
          {{ task.error_code }}: {{ task.error_message }}
        </p>
        <ul
          v-if="task.attempts.length"
          class="attempts"
        >
          <li
            v-for="attempt in task.attempts"
            :key="attempt.attempt_id"
          >
            Attempt {{ attempt.attempt_no }} · {{ attempt.state }}<span v-if="attempt.runner_id"> · Runner {{ attempt.runner_id }}</span>
            <span
              v-if="attempt.error_code"
              class="error"
            > · {{ attempt.error_code }}: {{ attempt.error_message }}</span>
          </li>
        </ul>
      </li>
    </ol>
  </section>
  <section
    v-if="job.artifacts.length"
    class="card"
    aria-labelledby="results-title"
  >
    <h2 id="results-title">
      解析成果
    </h2>
    <article
      v-for="artifact in job.artifacts"
      :key="artifact.artifact_id"
      class="artifact"
    >
      <h3>{{ artifact.kind }} <small>{{ artifact.name }} · {{ sizeLabel(artifact.size_bytes) }}</small></h3>
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
    </article>
  </section>
</template>

<style scoped>
.actions { display: flex; flex-wrap: wrap; gap: .5rem; }
</style>
