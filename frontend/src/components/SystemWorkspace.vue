<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";
import EventsPanel from "./EventsPanel.vue";

const props = defineProps<{ jobId?: components["schemas"]["JobId"] }>();
const system = ref<components["schemas"]["SystemView"]>();
const runners = ref<components["schemas"]["RunnerView"][]>([]);
const status = ref("正在读取 Home Core 状态…");
const loading = ref(false);
const eventScope = ref<"system" | "job">("system");
let timer: number | undefined;

function bytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}
function time(value: number | null | undefined): string {
  return value === null || value === undefined ? "从未上报" : new Date(value).toLocaleString("zh-CN");
}
function toolName(tool: components["schemas"]["RunnerTool"]): string {
  return tool.replaceAll("_", " ");
}

async function refresh(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  try {
    const [health, runnerList] = await Promise.all([
      apiClient.GET("/api/v1/system"), apiClient.GET("/api/v1/runners"),
    ]);
    if (!health.data) { status.value = apiError(health.error, "无法读取系统状态。"); return; }
    if (!runnerList.data) { status.value = apiError(runnerList.error, "无法读取 Runner。"); return; }
    system.value = health.data;
    runners.value = runnerList.data;
    status.value = health.data.status === "healthy" ? "Home Core 正常。" : "Home Core 已降级，请检查下面的真实指标。";
  } catch { status.value = "无法连接 Home Core。"; }
  finally { loading.value = false; }
}

onMounted(() => { void refresh(); timer = window.setInterval(() => { void refresh(); }, 15_000); });
onUnmounted(() => { if (timer !== undefined) window.clearInterval(timer); });
</script>

<template>
  <section class="system-workspace">
    <header class="system-heading">
      <div>
        <p class="eyebrow">
          System
        </p><h1>系统健康总览</h1>
      </div>
      <button
        class="btn secondary compact"
        type="button"
        :disabled="loading"
        @click="refresh"
      >
        {{ loading ? "刷新中" : "刷新" }}
      </button>
    </header>
    <section
      v-if="system"
      class="health-banner"
      :class="`health-${system.status}`"
      role="status"
    >
      <span /><div><b>{{ system.status === "healthy" ? "Home Core 正常" : "Home Core 已降级" }}</b><p>{{ status }}</p></div>
    </section>

    <div
      v-if="system"
      class="metric-grid"
    >
      <article class="card">
        <small>等待任务</small><b>{{ system.queue_depth }}</b><em>仅汇总；当前 API 不提供队列明细</em>
      </article>
      <article class="card">
        <small>NAS 可用空间</small><b>{{ bytes(system.disk_free_bytes) }}</b>
      </article>
      <article class="card">
        <small>Runner 在线</small><b>{{ system.runners_online }} / {{ system.runners_total }}</b>
      </article>
      <article class="card">
        <small>AI usage</small><b>{{ system.usage_final }} / {{ system.usage_started }}</b><em>final / started</em>
      </article>
    </div>

    <section class="runner-section card">
      <header>
        <div>
          <p class="eyebrow">
            Execution
          </p><h2>Runner</h2>
        </div><span>{{ runners.filter((runner) => runner.online).length }} 在线</span>
      </header>
      <div
        v-if="runners.length"
        class="runner-grid"
      >
        <article
          v-for="runner in runners"
          :key="runner.runner_id"
        >
          <div class="runner-title">
            <span :class="{ online: runner.online }" /><div><b>{{ runner.name }}</b><small>{{ runner.online ? "在线" : "离线" }} · {{ runner.state === "enabled" ? "已启用" : "已禁用" }}</small></div><em>{{ runner.active_attempts }} / {{ runner.max_concurrency }}</em>
          </div>
          <p>
            <span
              v-for="tag in runner.tags"
              :key="tag"
            >{{ tag }}</span>
          </p>
          <dl>
            <div><dt>工具</dt><dd>{{ runner.tools.map((tool) => `${toolName(tool.tool)} ${tool.version}`).join(" · ") || "未上报" }}</dd></div>
            <div><dt>模型</dt><dd>{{ runner.ai_models.map((model) => `${model.model} (${model.efforts.join("/")})`).join(" · ") || "不执行 AI" }}</dd></div>
            <div><dt>默认</dt><dd>{{ runner.default_model ?? "—" }} / {{ runner.default_effort ?? "—" }}</dd></div>
            <div><dt>最近心跳</dt><dd>{{ time(runner.last_seen_at_ms) }}</dd></div>
            <div><dt>配置版本</dt><dd>{{ runner.config_revision }}</dd></div>
          </dl>
        </article>
      </div>
      <p
        v-else
        class="empty-state"
      >
        没有已注册 Runner。
      </p>
    </section>

    <nav
      v-if="jobId"
      class="event-scope"
      aria-label="事件范围"
    >
      <button
        type="button"
        :aria-pressed="eventScope === 'system'"
        @click="eventScope = 'system'"
      >
        系统事件
      </button>
      <button
        type="button"
        :aria-pressed="eventScope === 'job'"
        @click="eventScope = 'job'"
      >
        当前 Job
      </button>
    </nav>
    <EventsPanel
      v-if="eventScope === 'system' || !jobId"
      key="system-events"
    />
    <EventsPanel
      v-else-if="props.jobId"
      :key="props.jobId"
      :job-id="props.jobId"
    />
  </section>
</template>

<style scoped>
.system-workspace { display: grid; gap: 14px; }
.system-heading, .runner-section > header { display: flex; align-items: end; justify-content: space-between; }
.system-heading p { margin-bottom: 0; color: var(--muted); }
.health-banner { display: flex; gap: 12px; align-items: center; padding: 14px 16px; border: 1px solid var(--line); border-left: 4px solid #3f8f48; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow); }.health-banner.health-degraded { border-left-color: #cb7b1f; }
.health-banner > span { width: 10px; height: 10px; border-radius: 50%; background: #3f8f48; }.health-degraded > span { background: #cb7b1f; }.health-banner p { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 9px; }
.metric-grid article { display: grid; align-content: start; gap: 4px; min-height: 92px; padding: 13px; }
.metric-grid small, .metric-grid em, .runner-section header > span { color: var(--muted); font-size: 11px; font-style: normal; }
.metric-grid b { color: var(--ink); font-size: 19px; }
.runner-title > span { width: 9px; height: 9px; border-radius: 50%; background: var(--danger); }
.runner-title > span.online { background: #3f8f48; }
.runner-section { overflow: hidden; }
.runner-section > header { padding: 13px 16px; border-bottom: 1px solid var(--line); }
.runner-section header p, .runner-section header h2 { margin-bottom: 0; }
.runner-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.runner-grid article { min-width: 0; padding: 14px 16px; border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft); }
.runner-title { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 8px; align-items: center; }
.runner-title div { display: grid; }
.runner-title small, .runner-title em { color: var(--muted); font-size: 11px; font-style: normal; }
.runner-grid p { display: flex; flex-wrap: wrap; gap: 4px; }
.runner-grid p span, .event-scope button { padding: 3px 7px; border: 1px solid var(--line); border-radius: 5px; color: var(--muted); background: var(--surface-soft); font-size: 11px; }
dl { display: grid; gap: 5px; margin: 0; font-size: 11px; }
dl div { display: grid; grid-template-columns: 72px minmax(0, 1fr); }
dt { color: var(--muted); } dd { margin: 0; color: #4a4843; word-break: break-word; }
.event-scope { display: flex; gap: 5px; }
.event-scope button { cursor: pointer; }
.event-scope button[aria-pressed="true"] { color: white; border-color: var(--brand); background: var(--brand); }
@media (max-width: 900px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 540px) { .metric-grid { grid-template-columns: 1fr; } .system-heading { align-items: start; } }
</style>
