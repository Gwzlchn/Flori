<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import ArtifactPreview from "./ArtifactPreview.vue";

const props = defineProps<{
  task: components["schemas"]["TaskView"];
  artifacts: components["schemas"]["ArtifactView"][];
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
}>();
const emit = defineEmits<{ loadArtifact: [artifact: components["schemas"]["ArtifactView"]] }>();

const labels: Record<string, string> = {
  acquire: "获取原文", extract: "解析文档", note: "生成智能笔记", translate: "全文翻译",
  validate: "校验证据", publish: "发布成果",
};
const states: Record<components["schemas"]["TaskState"], string> = {
  pending: "等待", ready: "就绪", leased: "执行中", succeeded: "完成", failed: "失败", skipped: "复用", canceled: "取消",
};
const currentAttempt = computed(() => props.task.attempts.find((attempt) => attempt.attempt_id === props.task.current_attempt_id)
  ?? props.task.attempts.at(-1));

function duration(start: number, finish?: number | null): string {
  const milliseconds = Math.max(0, (finish ?? Date.now()) - start);
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
}
function time(value?: number | null): string { return value === undefined || value === null ? "—" : new Date(value).toLocaleString("zh-CN"); }
function usageLabel(usage: components["schemas"]["AiUsageView"]): string {
  if (usage.credits_micros !== undefined && usage.credits_micros !== null) return `${(usage.credits_micros / 1_000_000).toFixed(3)} credits`;
  if (usage.input_tokens !== undefined && usage.input_tokens !== null) return `${usage.input_tokens} in / ${usage.output_tokens ?? 0} out tokens`;
  if (usage.cost_micros !== undefined && usage.cost_micros !== null) return `${(usage.cost_micros / 1_000_000).toFixed(4)} cost`;
  return usage.state === "started" ? "调用尚未结束" : "计量不可用";
}
</script>

<template>
  <article class="step-workbench">
    <header>
      <div>
        <p class="eyebrow">
          Selected step
        </p><h2>{{ labels[task.task_key] ?? task.task_key }}</h2>
      </div>
      <span
        class="status-pill"
        :class="`state-${task.state}`"
      >{{ states[task.state] }}</span>
    </header>
    <div class="facts">
      <span><b>Executor</b>{{ task.executor }}</span><span><b>依赖</b>{{ task.spec.needs.join(", ") || "无" }}</span>
      <span><b>超时</b>{{ task.spec.timeout_ms }} ms</span><span><b>重试</b>{{ task.spec.retry }} 次</span>
      <span><b>Tags</b>{{ task.spec.tags.join(", ") || "无" }}</span>
    </div>
    <p
      v-if="task.error_code"
      class="error-box"
    >
      {{ task.error_code }}: {{ task.error_message }}
    </p>
    <div
      v-if="currentAttempt"
      class="run-summary"
    >
      <div><small>开始</small><b>{{ time(currentAttempt.started_at_ms) }}</b></div>
      <div><small>结束 / 耗时</small><b>{{ time(currentAttempt.finished_at_ms) }} · {{ duration(currentAttempt.started_at_ms, currentAttempt.finished_at_ms) }}</b></div>
      <div><small>Runner</small><b>{{ currentAttempt.runner_id ?? "Home Core" }}</b></div>
      <div><small>模型配置</small><b>{{ currentAttempt.model ?? "—" }} / {{ currentAttempt.effort ?? "—" }} / rev {{ currentAttempt.runner_config_revision ?? "—" }}</b></div>
    </div>
    <div class="step-columns">
      <section>
        <h3>声明产物</h3><ul class="compact-list declaration-list">
          <li
            v-for="artifact in task.spec.artifacts"
            :key="artifact.name"
          >
            <b>{{ artifact.name }}</b><span>{{ artifact.kind }} · {{ artifact.required ? "必需" : "可选" }} · {{ artifact.when }} · ≤ {{ artifact.max_bytes }} B</span>
          </li>
        </ul>
      </section>
      <section>
        <h3>实际产物</h3><div class="step-artifacts">
          <ArtifactPreview
            v-for="artifact in artifacts"
            :key="artifact.artifact_id"
            :artifact="artifact"
            :text="textContent.get(artifact.artifact_id)"
            :file-url="fileUrls.get(artifact.artifact_id)"
            @load="emit('loadArtifact', $event)"
          /><p
            v-if="!artifacts.length"
            class="meta"
          >
            尚无产物
          </p>
        </div>
      </section>
    </div>
    <details
      v-for="attempt in task.attempts"
      :key="attempt.attempt_id"
      class="attempt-card"
      :open="attempt.attempt_id === task.current_attempt_id"
    >
      <summary><b>Attempt #{{ attempt.attempt_no }}</b><span>{{ attempt.state }} · {{ duration(attempt.started_at_ms, attempt.finished_at_ms) }}</span></summary>
      <p class="meta">
        {{ attempt.attempt_id }} · 日志游标 {{ attempt.last_log_sequence }} · lease {{ time(attempt.lease_expires_at_ms) }}
      </p>
      <p
        v-if="attempt.error_code"
        class="error-text"
      >
        {{ attempt.error_code }}: {{ attempt.error_message }}
      </p>
      <ul
        v-if="attempt.usage.length"
        class="usage-list"
      >
        <li
          v-for="usage in attempt.usage"
          :key="usage.usage_id"
        >
          <b>{{ usage.invocation_key }}</b><span>{{ usage.tool }} · {{ usage.model }} / {{ usage.effort }}</span>
          <strong>{{ usageLabel(usage) }}</strong><small>{{ usage.origin ?? "pending" }}</small>
        </li>
      </ul><p
        v-else
        class="meta"
      >
        无 AI 调用
      </p>
    </details>
  </article>
</template>

<style scoped>
.facts,.run-summary { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 7px; margin: 10px 0 15px; }
.facts span,.run-summary div { display: grid; gap: 2px; padding: 8px; border-radius: 5px; background: var(--surface-soft); color: var(--muted); font-size: 11px; }
.facts b,.run-summary b { overflow-wrap: break-word; color: var(--ink); font-weight: 600; }.run-summary small { color: var(--muted); }
.attempt-card { margin-top: 9px; border: 1px solid var(--line); border-radius: 6px; }.attempt-card summary { display: flex; justify-content: space-between; padding: 9px; cursor: pointer; }.attempt-card > :not(summary) { margin: 8px 10px; }
.usage-list { display: grid; gap: 5px; padding: 0; list-style: none; }.usage-list li { display: grid; grid-template-columns: minmax(70px,.5fr) minmax(160px,1.5fr) minmax(110px,.7fr) auto; gap: 8px; align-items: center; }.usage-list span,.usage-list small { color: var(--muted); }
@media (max-width: 720px) { .usage-list li { grid-template-columns: 1fr; gap: 2px; } }
</style>
