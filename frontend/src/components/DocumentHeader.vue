<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import UiIcon from "./UiIcon.vue";

const props = defineProps<{
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
  title: string;
  domainName: string;
  collectionNames: string[];
}>();
defineEmits<{ refresh: [] }>();

const sourceLabels: Record<components["schemas"]["SourceKind"], string> = {
  pdf_upload: "本地 PDF", pdf_url: "PDF 链接", arxiv: "arXiv 论文",
  local_video: "本地视频", bilibili_video: "Bilibili 视频", bilibili_channel: "Bilibili 频道",
  youtube_video: "YouTube 视频", youtube_channel: "YouTube 频道",
};
const triggerLabels: Record<components["schemas"]["JobTrigger"], string> = {
  initial: "首次处理", pipeline_rerun: "整链重跑", task_rerun: "步骤重跑", subscription: "订阅投递",
};
const stateLabels: Record<components["schemas"]["JobState"], string> = {
  queued: "等待处理", running: "处理中", succeeded: "已发布", failed: "处理失败", canceled: "已取消",
};
const settledTasks = computed(() => props.job.tasks.filter((task) => task.state === "succeeded" || task.state === "skipped").length);
</script>

<template>
  <section class="content-identity card">
    <div class="content-type-icon">
      <UiIcon
        name="file"
        :size="22"
      />
    </div>
    <div class="content-identity-main">
      <div class="identity-badges">
        <span class="type-badge">{{ source ? sourceLabels[source.kind] : "PDF" }}</span>
        <span
          class="status-pill"
          :class="`state-${job.state}`"
        >{{ stateLabels[job.state] }}</span>
      </div>
      <h1>{{ title }}</h1>
      <div class="identity-context">
        <span>{{ domainName }}</span><i />
        <span>{{ collectionNames.join("、") || "未归分类" }}</span><i />
        <span>{{ triggerLabels[job.trigger] }}</span><i />
        <span>{{ settledTasks }}/{{ job.tasks.length }} 步收敛</span>
      </div>
      <a
        v-if="source?.canonical_ref.startsWith('http')"
        class="source-link"
        :href="source.canonical_ref"
        target="_blank"
        rel="noopener"
      >查看原始来源 <UiIcon
        name="external"
        :size="13"
      /></a>
    </div>
    <div class="identity-actions">
      <button
        type="button"
        class="btn secondary compact"
        @click="$emit('refresh')"
      >
        <UiIcon
          name="refresh"
          :size="14"
        />刷新
      </button>
      <a
        href="#operations"
        class="btn secondary compact"
      ><UiIcon
        name="settings"
        :size="14"
      />内容操作</a>
    </div>
    <details class="identity-technical">
      <summary>内部信息</summary>
      <dl>
        <div><dt>Job</dt><dd>{{ job.job_id }}</dd></div>
        <div><dt>Pipeline revision</dt><dd>{{ job.pipeline_revision_id }}</dd></div>
        <div><dt>Source</dt><dd>{{ job.source_id }}</dd></div>
      </dl>
    </details>
  </section>
</template>
