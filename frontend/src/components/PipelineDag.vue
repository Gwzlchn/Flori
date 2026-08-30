<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";

type TaskView = components["schemas"]["TaskView"];

const props = defineProps<{
  tasks: components["schemas"]["TaskView"][];
  selectedTaskId: string | undefined;
}>();
const emit = defineEmits<{ select: [taskKey: string] }>();

const labels: Record<string, string> = {
  acquire: "获取原文", extract: "解析文档", note: "智能笔记", translate: "全文翻译",
  transcribe: "标准化字幕", frames: "提取关键帧", mechanical_note: "机械笔记",
  validate: "校验证据", publish: "发布成果", subscription: "同步订阅",
};
const states: Record<components["schemas"]["TaskState"], string> = {
  pending: "等待", ready: "就绪", leased: "执行中", succeeded: "完成", failed: "失败", skipped: "复用", canceled: "取消",
};

function currentAttempt(task: TaskView): components["schemas"]["AttemptView"] | undefined {
  return task.attempts.find((attempt) => attempt.attempt_id === task.current_attempt_id) ?? task.attempts.at(-1);
}

function duration(task: TaskView): string {
  const attempt = currentAttempt(task);
  if (!attempt) return "尚未运行";
  const milliseconds = Math.max(0, (attempt.finished_at_ms ?? Date.now()) - attempt.started_at_ms);
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
}

function executorClass(task: TaskView): "core" | "ai" | "media" {
  if (task.executor.startsWith("core.")) return "core";
  if (task.executor.startsWith("ai.")) return "ai";
  return "media";
}

const selectedKeys = computed(() => {
  const selected = props.tasks.find((task) => task.task_id === props.selectedTaskId);
  const byKey = new Map(props.tasks.map((task) => [task.task_key, task]));
  const keys = new Set<string>();
  const visit = (key: string): void => {
    if (keys.has(key)) return;
    keys.add(key);
    for (const need of byKey.get(key)?.spec.needs ?? []) visit(need);
  };
  if (selected) visit(selected.task_key);
  return keys;
});

const graph = computed(() => {
  const pending = [...props.tasks];
  const depth = new Map<string, number>();
  const ordered: TaskView[] = [];
  while (pending.length) {
    const ready = pending.findIndex((task) => task.spec.needs.every((key) => depth.has(key)));
    const task = pending.splice(ready < 0 ? 0 : ready, 1)[0];
    if (!task) break;
    depth.set(task.task_key, task.spec.needs.reduce((value, key) => Math.max(value, (depth.get(key) ?? -1) + 1), 0));
    ordered.push(task);
  }
  const levelCounts = new Map<number, number>();
  const nodes = ordered.map((task) => {
    const level = depth.get(task.task_key) ?? 0;
    const row = levelCounts.get(level) ?? 0;
    levelCounts.set(level, row + 1);
    return { task, x: 18 + level * 188, y: 30 + row * 86 };
  });
  const byKey = new Map(nodes.map((node) => [node.task.task_key, node]));
  const edges = nodes.flatMap((target) => target.task.spec.needs.flatMap((key) => {
    const source = byKey.get(key);
    return source ? [{
      active: selectedKeys.value.has(key) && selectedKeys.value.has(target.task.task_key),
      key: `${key}-${target.task.task_key}`, source, target,
    }] : [];
  }));
  return {
    ordered,
    nodes,
    edges,
    width: Math.max(420, 166 + Math.max(0, ...nodes.map((node) => node.x))),
    height: Math.max(150, 108 + Math.max(0, ...nodes.map((node) => node.y))),
  };
});

function nodeDetail(task: TaskView): string {
  const attempt = currentAttempt(task);
  const execution = attempt
    ? `${attempt.runner_id ?? "Home Core"} · ${attempt.model ?? task.selected_model ?? "无模型"} · ${duration(task)}`
    : `${task.pinned_runner_id ?? "未分配 Runner"} · ${task.selected_model ?? "无模型"}`;
  const error = task.error_code ? ` · ${task.error_code}: ${task.error_message ?? ""}` : "";
  return `${task.executor} · needs ${task.spec.needs.join(", ") || "none"} · timeout ${task.spec.timeout_ms} ms · ${execution}${error}`;
}

function selectOffset(offset: number): void {
  const tasks = graph.value.ordered;
  const index = Math.max(0, tasks.findIndex((task) => task.task_id === props.selectedTaskId));
  const target = tasks[(index + offset + tasks.length) % tasks.length];
  if (target) emit("select", target.task_key);
}
</script>

<template>
  <div class="dag-frame">
    <div
      class="dag-legend"
      aria-label="Pipeline 状态图例"
    >
      <span><i class="state-succeeded" />完成</span><span><i class="state-leased" />执行中</span>
      <span><i class="state-ready" />就绪</span><span><i class="state-failed" />失败 / 取消</span>
      <span class="legend-spacer" /><span><i class="executor-core" />Core</span><span><i class="executor-ai" />AI</span>
      <span><i class="executor-media" />Media</span>
    </div>
    <div class="dag-scroll">
      <div
        class="dag-canvas"
        :style="{ width: `${graph.width}px`, height: `${graph.height}px` }"
        role="listbox"
        aria-label="可点击 Pipeline DAG"
        tabindex="0"
        @keydown.left.prevent="selectOffset(-1)"
        @keydown.up.prevent="selectOffset(-1)"
        @keydown.right.prevent="selectOffset(1)"
        @keydown.down.prevent="selectOffset(1)"
      >
        <svg
          :viewBox="`0 0 ${graph.width} ${graph.height}`"
          aria-hidden="true"
        >
          <defs>
            <marker
              id="dag-arrow"
              markerWidth="6"
              markerHeight="6"
              refX="5"
              refY="3"
              orient="auto"
            ><path d="M0,0 L6,3 L0,6 Z" /></marker>
            <marker
              id="dag-arrow-active"
              markerWidth="6"
              markerHeight="6"
              refX="5"
              refY="3"
              orient="auto"
            ><path d="M0,0 L6,3 L0,6 Z" /></marker>
          </defs>
          <path
            v-for="edge in graph.edges"
            :key="edge.key"
            :class="{ active: edge.active }"
            :d="`M ${edge.source.x + 152} ${edge.source.y + 30} C ${edge.source.x + 170} ${edge.source.y + 30}, ${edge.target.x - 18} ${edge.target.y + 30}, ${edge.target.x} ${edge.target.y + 30}`"
            :marker-end="edge.active ? 'url(#dag-arrow-active)' : 'url(#dag-arrow)'"
          />
        </svg>
        <button
          v-for="node in graph.nodes"
          :key="node.task.task_id"
          type="button"
          role="option"
          class="dag-node"
          :class="[{ 'is-active': selectedTaskId === node.task.task_id }, `state-${node.task.state}`, `executor-${executorClass(node.task)}`]"
          :aria-selected="selectedTaskId === node.task.task_id"
          :aria-label="`${labels[node.task.task_key] ?? node.task.task_key}，${states[node.task.state]}`"
          :style="{ left: `${node.x}px`, top: `${node.y}px` }"
          :title="nodeDetail(node.task)"
          @click="emit('select', node.task.task_key)"
        >
          <b>{{ labels[node.task.task_key] ?? node.task.task_key }}</b>
          <span>{{ states[node.task.state] }} · {{ duration(node.task) }} · {{ node.task.attempts.length }} attempt</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dag-frame { overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-soft); }
.dag-legend { display: flex; flex-wrap: wrap; gap: 13px; padding: 9px 13px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 11px; }
.dag-legend span { display: flex; align-items: center; gap: 5px; }
.dag-legend .legend-spacer { flex: 1; }
.dag-legend i { width: 7px; height: 7px; border-radius: 50%; background: #bcbbb5; }
.dag-legend .state-succeeded { background: #3f8f48; }.dag-legend .state-leased { background: var(--brand); }
.dag-legend .state-ready { background: #d3972d; }.dag-legend .state-failed { background: var(--danger); }
.dag-legend .executor-core { background: #77746c; }.dag-legend .executor-ai { background: var(--brand); }
.dag-legend .executor-media { background: #a06e2b; }
.dag-scroll { overflow-x: auto; }.dag-canvas { position: relative; min-width: 100%; outline: none; }
svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
svg path { fill: none; stroke: #bbb9b2; stroke-width: 1.4; }svg marker path { fill: #aaa9a3; stroke: none; }
svg path.active { stroke: var(--brand); stroke-width: 2.3; }
svg #dag-arrow-active path { fill: var(--brand); }
.dag-node { position: absolute; display: grid; width: 152px; min-height: 60px; gap: 3px; padding: 8px 9px; border: 1px solid var(--line); border-left: 5px solid #bcbbb5; border-radius: 5px; color: var(--ink); text-align: left; background: white; cursor: pointer; box-shadow: var(--shadow); }
.dag-node span { overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.dag-node.is-active { border-color: var(--brand); box-shadow: 0 0 0 2px rgb(35 131 226 / 10%); }
.dag-node.executor-core { border-left-color: #77746c; }.dag-node.executor-ai { border-left-color: var(--brand); }
.dag-node.executor-media { border-left-color: #a06e2b; }
.dag-node.state-pending::after,.dag-node.state-skipped::after,.dag-node.state-succeeded::after,.dag-node.state-leased::after,.dag-node.state-ready::after,.dag-node.state-failed::after,.dag-node.state-canceled::after { position: absolute; top: 8px; right: 8px; width: 7px; height: 7px; border-radius: 50%; background: #bcbbb5; content: ""; }
.dag-node.state-succeeded::after { background: #3f8f48; }.dag-node.state-leased::after { background: var(--brand); }
.dag-node.state-ready::after { background: #d3972d; }.dag-node.state-failed::after,.dag-node.state-canceled::after { background: var(--danger); }
@media (max-width: 720px) { .dag-legend .legend-spacer { display: none; } }
</style>
