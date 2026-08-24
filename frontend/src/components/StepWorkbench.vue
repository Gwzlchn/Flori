<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import ArtifactPreview from "./ArtifactPreview.vue";

type ArtifactKind = components["schemas"]["ArtifactKind"];
type ArtifactGroup = "输入与结构" | "阅读成果" | "证据与审计" | "图表与媒体" | "运行记录";

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
const attemptStates: Record<components["schemas"]["AttemptState"], string> = {
  leased: "执行中", succeeded: "完成", failed: "失败", expired: "租约过期", canceled: "取消",
};
const artifactGroups: Record<ArtifactKind, ArtifactGroup> = {
  source_original: "输入与结构", document_structure: "输入与结构", scholarly_html: "输入与结构",
  scholarly_html_snapshot: "输入与结构", scholarly_resource: "图表与媒体",
  smart_note: "阅读成果", summary: "阅读成果", translation: "阅读成果", terms: "阅读成果",
  mechanical_note: "阅读成果", evidence: "证据与审计", ai_audit: "证据与审计",
  figure: "图表与媒体", table_region: "图表与媒体", keyframe: "图表与媒体",
  subtitle: "输入与结构", transcript: "输入与结构", danmaku: "输入与结构",
  parts_manifest: "输入与结构", subscription_manifest: "输入与结构", task_log: "运行记录",
};
const groupOrder: ArtifactGroup[] = ["输入与结构", "阅读成果", "证据与审计", "图表与媒体", "运行记录"];
const currentAttempt = computed(() => props.task.attempts.find((attempt) => attempt.attempt_id === props.task.current_attempt_id)
  ?? props.task.attempts.at(-1));
const actualGroups = computed(() => groupOrder.map((name) => ({
  artifacts: props.artifacts.filter((artifact) => artifactGroups[artifact.kind] === name), name,
})).filter((group) => group.artifacts.length > 0));
const selectedExecution = computed(() => ({
  effort: currentAttempt.value?.effort ?? props.task.selected_effort ?? "—",
  model: currentAttempt.value?.model ?? props.task.selected_model ?? "—",
  revision: currentAttempt.value?.runner_config_revision ?? props.task.runner_config_revision ?? "—",
  runner: currentAttempt.value?.runner_id ?? props.task.pinned_runner_id
    ?? (props.task.executor.startsWith("core.") ? "Home Core" : "未分配"),
}));

function duration(start: number, finish?: number | null): string {
  const milliseconds = Math.max(0, (finish ?? Date.now()) - start);
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
}
function time(value?: number | null): string { return value === undefined || value === null ? "—" : new Date(value).toLocaleString("zh-CN"); }
function usageLabel(usage: components["schemas"]["AiUsageView"]): string {
  const values: string[] = [];
  if (usage.credits_micros !== undefined && usage.credits_micros !== null) values.push(`${(usage.credits_micros / 1_000_000).toFixed(3)} credits`);
  if (usage.input_tokens !== undefined && usage.input_tokens !== null) values.push(`${usage.input_tokens.toLocaleString()} in / ${(usage.output_tokens ?? 0).toLocaleString()} out`);
  if (usage.cost_micros !== undefined && usage.cost_micros !== null) values.push(`${(usage.cost_micros / 1_000_000).toFixed(4)} cost`);
  return values.join(" · ") || (usage.state === "started" ? "调用尚未结束" : "计量不可用");
}
function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}
function originLabel(origin?: components["schemas"]["UsageOrigin"] | null): string {
  if (origin === "observed") return "实测";
  if (origin === "estimated") return "估算";
  if (origin === "unavailable") return "不可用";
  return "待上报";
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
      <span><b>Tags</b>{{ task.spec.tags.join(", ") || "无" }}</span><span><b>Attempts</b>{{ task.attempts.length }} 次</span>
    </div>
    <p
      v-if="task.error_code"
      class="error-box"
    >
      {{ task.error_code }}: {{ task.error_message }}
    </p>
    <div class="run-summary">
      <div><small>开始</small><b>{{ currentAttempt ? time(currentAttempt.started_at_ms) : "尚未运行" }}</b></div>
      <div><small>结束 / 耗时</small><b>{{ currentAttempt ? `${time(currentAttempt.finished_at_ms)} · ${duration(currentAttempt.started_at_ms, currentAttempt.finished_at_ms)}` : "—" }}</b></div>
      <div><small>Runner</small><b>{{ selectedExecution.runner }}</b></div>
      <div><small>模型配置</small><b>{{ selectedExecution.model }} / {{ selectedExecution.effort }} / rev {{ selectedExecution.revision }}</b></div>
    </div>
    <div class="step-columns">
      <section>
        <h3>声明产物</h3><ul class="compact-list declaration-list">
          <li
            v-for="artifact in task.spec.artifacts"
            :key="artifact.name"
          >
            <b>{{ artifact.name }}</b>
            <span>{{ artifact.kind }} · {{ artifact.required ? "必需" : "可选" }} · {{ artifact.when }} · ≤ {{ sizeLabel(artifact.max_bytes) }}</span>
            <code>{{ artifact.path }}<template v-if="artifact.max_files"> · 最多 {{ artifact.max_files }} 个</template></code>
          </li>
        </ul>
      </section>
      <section>
        <h3>实际产物 <small>{{ artifacts.length }} 项</small></h3>
        <div
          v-for="group in actualGroups"
          :key="group.name"
          class="artifact-group"
        >
          <h4>{{ group.name }} <small>{{ group.artifacts.length }}</small></h4>
          <div class="step-artifacts">
            <ArtifactPreview
              v-for="artifact in group.artifacts"
              :key="artifact.artifact_id"
              :artifact="artifact"
              :text="textContent.get(artifact.artifact_id)"
              :file-url="fileUrls.get(artifact.artifact_id)"
              @load="emit('loadArtifact', $event)"
            />
          </div>
        </div>
        <p
          v-if="!actualGroups.length"
          class="meta"
        >
          尚无产物
        </p>
      </section>
    </div>
    <details
      v-for="attempt in task.attempts"
      :key="attempt.attempt_id"
      class="attempt-card"
      :open="attempt.attempt_id === task.current_attempt_id"
    >
      <summary><b>Attempt #{{ attempt.attempt_no }}</b><span>{{ attemptStates[attempt.state] }} · {{ duration(attempt.started_at_ms, attempt.finished_at_ms) }}</span></summary>
      <div class="attempt-facts">
        <span><b>Runner</b>{{ attempt.runner_id ?? "Home Core" }}</span><span><b>模型</b>{{ attempt.model ?? "—" }} / {{ attempt.effort ?? "—" }}</span>
        <span><b>开始 / 结束</b>{{ time(attempt.started_at_ms) }} / {{ time(attempt.finished_at_ms) }}</span>
        <span><b>租约 / 日志</b>{{ time(attempt.lease_expires_at_ms) }} / #{{ attempt.last_log_sequence }}</span>
      </div>
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
          <strong>{{ usageLabel(usage) }}</strong><small>{{ usage.state }} · {{ originLabel(usage.origin) }}</small>
          <time>{{ time(usage.created_at_ms) }} → {{ time(usage.finalized_at_ms) }}</time>
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
.declaration-list li { display: grid; gap: 2px; }.declaration-list code { color: var(--muted); font-size: 10px; word-break: break-all; }
.artifact-group + .artifact-group { margin-top: 12px; }.artifact-group h4 { display: flex; gap: 6px; margin: 7px 0; color: var(--muted); font-size: 11px; }
.artifact-group h4 small,h3 small { font-weight: 400; }
.attempt-card { margin-top: 9px; border: 1px solid var(--line); border-radius: 6px; }.attempt-card summary { display: flex; justify-content: space-between; padding: 9px; cursor: pointer; }.attempt-card > :not(summary) { margin: 8px 10px; }
.attempt-facts { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 5px; }.attempt-facts span { display: grid; color: var(--muted); font-size: 10px; }.attempt-facts b { color: var(--ink); }
.usage-list { display: grid; gap: 5px; padding: 0; list-style: none; }.usage-list li { display: grid; grid-template-columns: minmax(70px,.5fr) minmax(160px,1.5fr) minmax(110px,.7fr) auto minmax(160px,1fr); gap: 8px; align-items: center; }.usage-list span,.usage-list small,.usage-list time { color: var(--muted); font-size: 10px; }
@media (max-width: 720px) { .usage-list li { grid-template-columns: 1fr; gap: 2px; } }
</style>
