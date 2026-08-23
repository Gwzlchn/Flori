<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";

const props = defineProps<{
  tasks: components["schemas"]["TaskView"][];
  selectedTaskId: string | undefined;
}>();
const emit = defineEmits<{ select: [taskKey: string] }>();

const labels: Record<string, string> = {
  acquire: "获取原文", extract: "解析文档", note: "智能笔记", translate: "全文翻译",
  validate: "校验证据", publish: "发布成果",
};
const states: Record<components["schemas"]["TaskState"], string> = {
  pending: "等待", ready: "就绪", leased: "执行中", succeeded: "完成", failed: "失败", skipped: "复用", canceled: "取消",
};

const graph = computed(() => {
  const pending = [...props.tasks];
  const depth = new Map<string, number>();
  const ordered: components["schemas"]["TaskView"][] = [];
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
    return { task, x: 24 + level * 204, y: 34 + row * 86 };
  });
  const byKey = new Map(nodes.map((node) => [node.task.task_key, node]));
  const edges = nodes.flatMap((target) => target.task.spec.needs.flatMap((key) => {
    const source = byKey.get(key);
    return source ? [{ key: `${key}-${target.task.task_key}`, source, target }] : [];
  }));
  return {
    ordered,
    nodes,
    edges,
    width: Math.max(420, 188 + Math.max(0, ...nodes.map((node) => node.x))),
    height: Math.max(156, 116 + Math.max(0, ...nodes.map((node) => node.y))),
  };
});

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
          <defs><marker
            id="dag-arrow"
            markerWidth="6"
            markerHeight="6"
            refX="5"
            refY="3"
            orient="auto"
          ><path d="M0,0 L6,3 L0,6 Z" /></marker></defs>
          <path
            v-for="edge in graph.edges"
            :key="edge.key"
            :d="`M ${edge.source.x + 152} ${edge.source.y + 29} C ${edge.source.x + 178} ${edge.source.y + 29}, ${edge.target.x - 26} ${edge.target.y + 29}, ${edge.target.x} ${edge.target.y + 29}`"
            marker-end="url(#dag-arrow)"
          />
        </svg>
        <button
          v-for="node in graph.nodes"
          :key="node.task.task_id"
          type="button"
          role="option"
          class="dag-node"
          :class="[{ 'is-active': selectedTaskId === node.task.task_id }, `state-${node.task.state}`]"
          :aria-selected="selectedTaskId === node.task.task_id"
          :style="{ left: `${node.x}px`, top: `${node.y}px` }"
          @click="emit('select', node.task.task_key)"
        >
          <b>{{ labels[node.task.task_key] ?? node.task.task_key }}</b>
          <small>{{ node.task.executor }}</small><span>{{ states[node.task.state] }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dag-frame { overflow: hidden; border: 1px solid var(--line); border-radius: 7px; background: linear-gradient(180deg, #fbfbfa, #f7f7f5); }
.dag-legend { display: flex; flex-wrap: wrap; gap: 13px; padding: 9px 13px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 11px; }
.dag-legend span { display: flex; align-items: center; gap: 5px; }
.dag-legend i { width: 7px; height: 7px; border-radius: 50%; background: #bcbbb5; }
.dag-legend .state-succeeded { background: #3f8f48; }.dag-legend .state-leased { background: var(--brand); }
.dag-legend .state-ready { background: #d3972d; }.dag-legend .state-failed { background: var(--danger); }
.dag-scroll { overflow-x: auto; }.dag-canvas { position: relative; min-width: 100%; outline: none; }
svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
svg path { fill: none; stroke: #bbb9b2; stroke-width: 1.4; }svg marker path { fill: #aaa9a3; stroke: none; }
.dag-node { position: absolute; display: grid; width: 152px; min-height: 58px; gap: 2px; padding: 9px 10px; border: 1px solid var(--line); border-left: 3px solid #bcbbb5; border-radius: 6px; color: var(--ink); text-align: left; background: white; cursor: pointer; box-shadow: var(--shadow); }
.dag-node small,.dag-node span { overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.dag-node.is-active { border-color: var(--brand); box-shadow: 0 0 0 2px rgb(35 131 226 / 10%); }
.dag-node.state-succeeded { border-left-color: #3f8f48; }.dag-node.state-leased { border-left-color: var(--brand); }
.dag-node.state-ready { border-left-color: #d3972d; }.dag-node.state-failed,.dag-node.state-canceled { border-left-color: var(--danger); }
</style>
