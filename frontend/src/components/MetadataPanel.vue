<script setup lang="ts">
import type { components } from "../api/client";

defineProps<{
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
  domainName: string | undefined;
  collectionNames: string[];
}>();

const sourceLabels: Record<components["schemas"]["SourceKind"], string> = {
  pdf_upload: "本地上传 PDF", pdf_url: "PDF 直链", arxiv: "arXiv",
  local_video: "本地视频", bilibili_video: "Bilibili 视频", bilibili_channel: "Bilibili 频道",
  youtube_video: "YouTube 视频", youtube_channel: "YouTube 频道",
};
const triggerLabels: Record<components["schemas"]["JobTrigger"], string> = {
  initial: "首次处理", pipeline_rerun: "整条 Pipeline 重跑", task_rerun: "从步骤重跑", subscription: "订阅投递",
};
const stateLabels: Record<components["schemas"]["JobState"], string> = {
  queued: "等待处理", running: "处理中", succeeded: "已发布", failed: "失败", canceled: "已取消",
};
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
      <section>
        <h3>内容归属</h3><dl>
          <div><dt>标题</dt><dd>{{ source?.title ?? source?.canonical_ref ?? "—" }}</dd></div>
          <div><dt>领域</dt><dd>{{ domainName ?? "未设置" }}</dd></div>
          <div><dt>分类</dt><dd>{{ collectionNames.join("、") || "未归分类" }}</dd></div>
          <div><dt>来源</dt><dd>{{ source ? sourceLabels[source.kind] : "—" }}</dd></div>
          <div><dt>规范引用</dt><dd>{{ source?.canonical_ref ?? "—" }}</dd></div>
        </dl>
      </section>
      <section>
        <h3>成果状态</h3><dl>
          <div><dt>当前状态</dt><dd>{{ stateLabels[job.state] }}</dd></div>
          <div><dt>触发方式</dt><dd>{{ triggerLabels[job.trigger] }}</dd></div>
          <div><dt>全文翻译</dt><dd>{{ job.inputs.translate ? "已请求" : "未请求" }}</dd></div>
          <div><dt>Pipeline</dt><dd>{{ job.tasks.length }} 个步骤，{{ job.artifacts.length }} 项产物</dd></div>
          <div><dt>历史成果</dt><dd>{{ source?.previous_job_id ? "保留上一版" : "暂无上一版" }}</dd></div>
        </dl>
      </section>
    </div>
    <details class="technical-details">
      <summary>查看内部标识</summary><dl>
        <div><dt>Source ID</dt><dd>{{ source?.source_id ?? "—" }}</dd></div>
        <div><dt>Current Job</dt><dd>{{ source?.current_job_id ?? "—" }}</dd></div>
        <div><dt>Previous Job</dt><dd>{{ source?.previous_job_id ?? "—" }}</dd></div>
        <div><dt>Pipeline revision</dt><dd>{{ job.pipeline_revision_id }}</dd></div>
      </dl>
    </details>
  </section>
</template>
