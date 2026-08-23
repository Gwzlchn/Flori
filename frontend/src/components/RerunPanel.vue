<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

import { apiClient, apiError, type components } from "../api/client";

const props = defineProps<{ job: components["schemas"]["JobView"] }>();
const emit = defineEmits<{ created: [jobId: string]; refresh: [] }>();
const runners = ref<components["schemas"]["RunnerView"][]>([]);
const runnerId = ref("");
const model = ref("");
const effort = ref("");
const pending = ref<components["schemas"]["RerunJobRequest"]>();
const pendingLabel = ref("");
const busy = ref(false);
const lifecycleBusy = ref(false);
const status = ref("选择一种重跑方式；每次都会创建新 Job。");

const aiRunners = computed(() => runners.value.filter((runner) =>
  runner.state === "enabled"
  && runner.ai_models.length > 0
  && runner.tools.some((tool) => tool.tool === "qoder_cli" || tool.tool === "codex_cli"),
));
const selectedRunner = computed(() => aiRunners.value.find((runner) => runner.runner_id === runnerId.value));
const models = computed(() => selectedRunner.value?.ai_models ?? []);
const efforts = computed(() => models.value.find((item) => item.model === model.value)?.efforts ?? []);

watch(aiRunners, (available) => {
  if (!available.some((runner) => runner.runner_id === runnerId.value)) {
    runnerId.value = available[0]?.runner_id ?? "";
  }
}, { immediate: true });
watch(selectedRunner, (runner) => {
  if (!runner?.ai_models.some((item) => item.model === model.value)) {
    model.value = runner?.default_model ?? runner?.ai_models[0]?.model ?? "";
  }
}, { immediate: true });
watch([models, model], () => {
  const runner = selectedRunner.value;
  if (!efforts.value.includes(effort.value)) {
    effort.value = runner?.default_model === model.value
      ? runner.default_effort ?? efforts.value[0] ?? ""
      : efforts.value[0] ?? "";
  }
}, { immediate: true });
watch(() => props.job.job_id, () => { pending.value = undefined; pendingLabel.value = ""; });

function arm(action: "pipeline" | "translate" | "note"): void {
  const runner = selectedRunner.value;
  if (action === "note" && (!runner || !model.value || !effort.value)) {
    status.value = "runner_unavailable: 请选择具备 AI 能力的 Runner、模型和 effort。";
    return;
  }
  pendingLabel.value = action === "pipeline" ? "整条 Pipeline" : action === "translate" ? "从 translate 开始" : "从 note 开始并固定 Runner";
  pending.value = {
    request_key: crypto.randomUUID(),
    mode: action === "pipeline" ? "pipeline" : "from_task",
    from_task_key: action === "pipeline" ? null : action,
    ai_selection: action === "note" && runner ? {
      task_key: "note", runner_id: runner.runner_id, model: model.value,
      effort: effort.value, runner_config_revision: runner.config_revision,
    } : null,
  };
  status.value = `请确认创建“${pendingLabel.value}”新 Job。成功发布前 current 不会改变。`;
}

async function submit(): Promise<void> {
  const request = pending.value;
  if (!request) return;
  busy.value = true;
  status.value = "正在创建重跑 Job…";
  try {
    const response = await apiClient.POST("/api/v1/jobs/{job_id}/rerun", {
      params: { path: { job_id: props.job.job_id } }, body: request,
    });
    if (!response.data) {
      status.value = apiError(response.error, "rerun_failed: 无法创建重跑 Job。");
      return;
    }
    pending.value = undefined;
    status.value = `已创建 Job ${response.data.job_id}。`;
    emit("created", response.data.job_id);
  } catch {
    status.value = "network_temporary: 创建未确认，可用同一 request_key 安全重试。";
  } finally { busy.value = false; }
}

async function cancelJob(): Promise<void> {
  if (!window.confirm("确定取消这个 Job？正在运行的 Attempt 会立即失效。")) return;
  lifecycleBusy.value = true;
  try {
    const result = await apiClient.POST("/api/v1/jobs/{job_id}/cancel", {
      params: { path: { job_id: props.job.job_id } },
    });
    status.value = result.response.ok ? "Job 已取消。" : apiError(result.error, "cancel_failed: 无法取消 Job。");
    if (result.response.ok) emit("refresh");
  } catch { status.value = "network_temporary: 取消请求未完成。"; }
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
    else status.value = apiError(result.error, "delete_failed: 无法删除 Source。");
  } catch { status.value = "network_temporary: 删除请求未完成。"; }
  finally { lifecycleBusy.value = false; }
}

onMounted(async () => {
  try {
    const response = await apiClient.GET("/api/v1/runners");
    runners.value = response.data ?? [];
    if (!response.data) status.value = apiError(response.error, "runner_read_failed: 无法读取 Runner。");
  } catch { status.value = "network_temporary: 无法读取 Runner 清单。"; }
});
</script>

<template>
  <details
    id="operations"
    class="operations card"
  >
    <summary>
      <span><b>重跑、翻译与危险操作</b><small>这些操作不会占用阅读主区域</small></span>
      <span class="summary-action">展开操作区</span>
    </summary>
    <div class="operations-body">
      <section aria-labelledby="rerun-title">
        <p class="eyebrow">
          Create a new Job
        </p>
        <h2 id="rerun-title">
          重跑与翻译
        </h2>
        <p class="meta">
          重跑总是创建新 Job；失败不会替换已发布的 current。
        </p>
        <div class="operation-actions">
          <button
            type="button"
            class="btn secondary"
            :disabled="busy"
            @click="arm('pipeline')"
          >
            重跑整条 Pipeline
          </button>
          <button
            type="button"
            class="btn secondary"
            :disabled="busy"
            @click="arm('translate')"
          >
            生成全文翻译
          </button>
        </div>
        <fieldset class="runner-fields">
          <legend>指定 AI Runner 重跑智能笔记</legend>
          <label for="runner">Runner<select
            id="runner"
            v-model="runnerId"
            :disabled="busy"
          >
            <option value="">无可用 Runner</option>
            <option
              v-for="runner in aiRunners"
              :key="runner.runner_id"
              :value="runner.runner_id"
            >
              {{ runner.name }} · {{ runner.online ? "在线" : "离线等待" }} · rev {{ runner.config_revision }}
            </option>
          </select></label>
          <label for="model">模型<select
            id="model"
            v-model="model"
            :disabled="busy || !runnerId"
          >
            <option
              v-for="item in models"
              :key="item.model"
              :value="item.model"
            >{{ item.model }}</option>
          </select></label>
          <label for="effort">Effort<select
            id="effort"
            v-model="effort"
            :disabled="busy || !model"
          >
            <option
              v-for="item in efforts"
              :key="item"
              :value="item"
            >{{ item }}</option>
          </select></label>
          <button
            type="button"
            class="btn secondary"
            :disabled="busy || !runnerId"
            @click="arm('note')"
          >
            准备指定重跑
          </button>
        </fieldset>
        <div
          v-if="pending"
          class="confirm-box"
        >
          <strong>待确认：{{ pendingLabel }}</strong>
          <div>
            <button
              type="button"
              class="btn primary"
              :disabled="busy"
              @click="submit"
            >
              {{ busy ? "创建中…" : "确认创建新 Job" }}
            </button>
            <button
              type="button"
              class="btn secondary"
              :disabled="busy"
              @click="pending = undefined"
            >
              返回
            </button>
          </div>
        </div>
      </section>
      <section
        class="danger-zone"
        aria-labelledby="danger-title"
      >
        <p class="eyebrow">
          Danger zone
        </p>
        <h2 id="danger-title">
          取消与删除
        </h2>
        <p>取消会 fence 当前 Attempt；删除 Source 会移除其全部历史成果，且不可撤销。</p>
        <button
          v-if="job.state === 'queued' || job.state === 'running'"
          type="button"
          class="btn danger-outline"
          :disabled="lifecycleBusy"
          @click="cancelJob"
        >
          取消当前 Job
        </button>
        <button
          type="button"
          class="btn danger"
          :disabled="lifecycleBusy"
          @click="deleteSource"
        >
          删除整个 Source
        </button>
      </section>
    </div>
    <p
      class="notice-bar"
      aria-live="polite"
    >
      {{ status }}
    </p>
  </details>
</template>
