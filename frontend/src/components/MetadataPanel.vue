<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";

interface MetadataGroup { label: string; rows: { label: string; value: string }[] }
const props = defineProps<{
  collectionNames: string[];
  documentView: components["schemas"]["DocumentRepresentationView"] | undefined;
  domainName: string | undefined;
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
}>();
const sourceLabels: Record<components["schemas"]["SourceKind"], string> = {
  arxiv: "arXiv", bilibili_channel: "Bilibili 频道", bilibili_video: "Bilibili 视频",
  local_video: "本地视频", pdf_upload: "本地上传 PDF", pdf_url: "PDF 直链",
  youtube_channel: "YouTube 频道", youtube_video: "YouTube 视频",
};
const triggerLabels: Record<components["schemas"]["JobTrigger"], string> = {
  initial: "首次处理", pipeline_rerun: "整条 Pipeline 重跑", subscription: "订阅投递", task_rerun: "从步骤重跑",
};
const stateLabels: Record<components["schemas"]["JobState"], string> = {
  canceled: "已取消", failed: "失败", queued: "等待处理", running: "处理中", succeeded: "已发布",
};
const usages = computed(() => props.job.tasks.flatMap((task) => task.attempts.flatMap((attempt) => attempt.usage)));
const audit = computed(() => props.job.artifacts.find((artifact) => artifact.kind === "ai_audit"));
function date(value?: number | null): string { return value === undefined || value === null ? "未提供" : new Date(value).toLocaleString("zh-CN"); }
function duration(start?: number | null, finish?: number | null): string {
  if (start === undefined || start === null) return "—";
  const seconds = Math.floor(Math.max(0, (finish ?? Date.now()) - start) / 1000);
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
function usageLabel(value: components["schemas"]["AiUsageView"]): string {
  if (value.credits_micros !== null && value.credits_micros !== undefined) return `${(value.credits_micros / 1_000_000).toFixed(3)} credits`;
  if (value.input_tokens !== null && value.input_tokens !== undefined) return `${value.input_tokens} in / ${value.output_tokens ?? 0} out tokens`;
  return value.state === "started" ? "调用尚未结束" : "计量不可用";
}
const groups = computed<MetadataGroup[]>(() => {
  const document = props.documentView;
  const result: MetadataGroup[] = [
    { label: "内容归属", rows: [
      { label: "标题", value: props.source?.title ?? props.source?.canonical_ref ?? "—" },
      { label: "领域", value: props.domainName ?? "未设置" },
      { label: "分类", value: props.collectionNames.join("、") || "未归分类" },
      { label: "来源", value: props.source ? sourceLabels[props.source.kind] : "—" },
      { label: "规范引用", value: props.source?.canonical_ref ?? "—" },
    ] },
    { label: "成果状态", rows: [
      { label: "状态 / 触发", value: `${stateLabels[props.job.state]} · ${triggerLabels[props.job.trigger]}` },
      { label: "Pipeline", value: `${props.job.tasks.length} 步 · ${props.job.artifacts.length} 项产物` },
      { label: "翻译", value: props.job.inputs.translate ? "已请求" : "未请求" },
      { label: "历史成果", value: props.source?.previous_job_id ? "保留上一版" : "暂无上一版" },
      { label: "创建 / 开始", value: `${date(props.job.created_at_ms)} / ${date(props.job.started_at_ms)}` },
      { label: "结束 / 耗时", value: `${date(props.job.finished_at_ms)} / ${duration(props.job.started_at_ms, props.job.finished_at_ms)}` },
    ] },
  ];
  if (document) result.push({ label: "论文与原文", rows: [
    { label: "作者", value: document.metadata.authors.join("、") || "未提供" },
    { label: "arXiv", value: document.metadata.arxiv_id ? `${document.metadata.arxiv_id} v${document.metadata.arxiv_version ?? "?"}` : "非 arXiv" },
    { label: "发布日期", value: date(document.metadata.published_at_ms) },
    { label: "语言 / 页数", value: `${document.metadata.language} / ${document.metadata.page_count}` },
    { label: "阅读表示", value: document.representation === "scholarly_html" ? `${document.provider} HTML` : "PDF" },
    { label: "原文摘要", value: document.metadata.abstract_text ?? "未提供" },
  ] });
  if (usages.value.length || audit.value) result.push({ label: "AI 与审计", rows: [
    { label: "计量", value: usages.value.map(usageLabel).join("；") || "无 AI usage" },
    { label: "AI audit", value: audit.value ? `${audit.value.name} · ${audit.value.sha256.slice(0, 16)}…` : "无" },
    { label: "Prompt digest", value: props.job.prompt_snapshot_sha256 },
  ] });
  return result;
});
const technical = computed(() => [
  ["Source ID", props.source?.source_id ?? "—"], ["Current Job", props.source?.current_job_id ?? "—"],
  ["Previous Job", props.source?.previous_job_id ?? "—"], ["Pipeline revision", props.job.pipeline_revision_id],
  ["Prompt snapshot", props.job.prompt_snapshot_sha256],
]);
</script>

<template>
  <section
    role="tabpanel"
    aria-labelledby="tab-metadata"
    class="metadata-panel"
  >
    <header class="panel-intro">
      <p class="eyebrow">
        Content information
      </p><h2>内容元信息</h2>
    </header>
    <div class="metadata-cards">
      <section
        v-for="group in groups"
        :key="group.label"
      >
        <h3>{{ group.label }}</h3>
        <dl>
          <div
            v-for="row in group.rows"
            :key="row.label"
          >
            <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
          </div>
        </dl>
      </section>
    </div>
    <details class="technical-details">
      <summary>查看内部标识</summary>
      <dl>
        <div
          v-for="row in technical"
          :key="row[0]"
        >
          <dt>{{ row[0] }}</dt><dd>{{ row[1] }}</dd>
        </div>
      </dl>
    </details>
  </section>
</template>
