<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";

const props = defineProps<{ jobId?: components["schemas"]["JobId"] }>();

interface EventLine {
  cursor: number;
  kind: string;
  data: string;
  createdAt: number;
}

const events = ref<EventLine[]>([]);
const filter = ref("");
const status = ref("正在连接事件流…");
let cursor = 0;
let controller: AbortController | undefined;
const kinds = computed(() => [...new Set(events.value.map((event) => event.kind))].sort());
const visibleEvents = computed(() => filter.value ? events.value.filter((event) => event.kind === filter.value) : events.value);

function label(kind: string): string {
  switch (kind) {
    case "source_changed": return "内容变化";
    case "job_state": return "Job 状态";
    case "task_state": return "Task 状态";
    case "artifact_committed": return "Artifact 提交";
    case "log_cursor": return "日志推进";
    case "runner_changed": return "Runner 变化";
    case "system_health": return "系统健康";
    default: return kind || "未命名事件";
  }
}

function accept(frame: string): void {
  if (!frame || frame.startsWith(":")) return;
  const lines = frame.split("\n");
  const id = lines.find((line) => line.startsWith("id: "))?.slice(4);
  const event = lines.find((line) => line.startsWith("event: "))?.slice(7) ?? "";
  const createdAt = lines.find((line) => line.startsWith("event-time-ms: "))?.slice(15);
  const data = lines.filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
  if (!id || !/^\d+$/u.test(id) || !createdAt || !/^\d+$/u.test(createdAt) || !event || !data) return;
  const next = Number(id);
  const timestamp = Number(createdAt);
  if (!Number.isSafeInteger(next) || next <= cursor || !Number.isSafeInteger(timestamp)) return;
  cursor = next;
  events.value = [{ cursor, kind: event, data, createdAt: timestamp }, ...events.value].slice(0, 80);
}

async function stream(signal: AbortSignal): Promise<"ended" | "expired" | "failed"> {
  try {
    const response = props.jobId
      ? await apiClient.GET("/api/v1/jobs/{job_id}/events", {
          params: { path: { job_id: props.jobId }, query: { after: cursor } }, parseAs: "stream", signal,
        })
      : await apiClient.GET("/api/v1/events", {
          params: { query: { after: cursor } }, parseAs: "stream", signal,
        });
    if (response.error?.error.code === "event_cursor_expired") return "expired";
    if (!response.data || !response.response.headers.get("content-type")?.startsWith("text/event-stream")) {
      status.value = apiError(response.error, "事件流响应无效。");
      return "failed";
    }
    status.value = props.jobId ? "正在接收当前 Job 事件。" : "正在接收全局事件。";
    const reader = response.data.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) return "ended";
      buffer += chunk.value;
      if (buffer.length > 64 * 1024) return "failed";
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        accept(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
    return "ended";
  } catch {
    return signal.aborted ? "ended" : "failed";
  }
}

async function watch(): Promise<void> {
  controller = new AbortController();
  while (!controller.signal.aborted) {
    const result = await stream(controller.signal);
    if (result === "expired") {
      cursor = 0;
      events.value = [];
      status.value = "事件游标已过期，已从当前保留窗口重新读取。";
    } else if (result === "failed") status.value = "事件流已断开，正在重连…";
    if (!controller.signal.aborted) await new Promise((resolve) => window.setTimeout(resolve, 1500));
  }
}

onMounted(() => { void watch(); });
onUnmounted(() => controller?.abort());
</script>

<template>
  <section
    class="event-panel card"
    aria-labelledby="events-title"
  >
    <header>
      <div>
        <p class="eyebrow">
          Live events
        </p><h2 id="events-title">
          {{ jobId ? "当前 Job 事件" : "系统事件" }}
        </h2>
      </div><label>筛选<select v-model="filter">
        <option value="">全部事件</option>
        <option
          v-for="kind in kinds"
          :key="kind"
          :value="kind"
        >{{ label(kind) }}</option>
      </select></label><span>{{ visibleEvents.length }} / {{ events.length }}</span>
    </header>
    <p
      class="event-status"
      aria-live="polite"
    >
      {{ status }}
    </p>
    <ol v-if="visibleEvents.length">
      <li
        v-for="item in visibleEvents"
        :key="item.cursor"
      >
        <span
          class="event-dot"
          aria-hidden="true"
        />
        <div><b>{{ label(item.kind) }}</b><small>游标 {{ item.cursor }} · {{ new Date(item.createdAt).toLocaleString("zh-CN") }}</small><code>{{ item.data }}</code></div>
      </li>
    </ol>
    <p
      v-else
      class="empty-state"
    >
      等待新的事件。
    </p>
  </section>
</template>

<style scoped>
.event-panel { overflow: hidden; }
header { display: flex; align-items: end; justify-content: space-between; padding: 14px 16px 10px; border-bottom: 1px solid var(--line); }
header p, header h2 { margin-bottom: 0; }
header > span, header label, small { color: var(--muted); font-size: 11px; }
header label { display: flex; gap: 5px; align-items: center; margin-left: auto; }header select { max-width: 150px; border: 1px solid var(--line); border-radius: 5px; background: white; }
.event-status { margin: 0; padding: 8px 16px; color: var(--brand-dark); background: var(--brand-soft); font-size: 12px; }
ol { display: grid; max-height: 430px; margin: 0; padding: 0; overflow: auto; list-style: none; }
li { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; padding: 10px 16px; border-top: 1px solid var(--line-soft); }
.event-dot { width: 7px; height: 7px; margin-top: 7px; border-radius: 50%; background: var(--brand); }
li div { display: grid; gap: 3px; min-width: 0; }
code { overflow: hidden; color: #4a4843; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
</style>
