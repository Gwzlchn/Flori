<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../api/client";
import JobPanel from "./JobPanel.vue";
import MarkdownContent from "./MarkdownContent.vue";
import UiIcon from "./UiIcon.vue";
import VisualCatalog from "./VisualCatalog.vue";

type ReaderTab = "artifacts" | "pipeline" | "metadata" | "visuals";
interface VisualLocation { bbox: components["schemas"]["PdfRect"]; label: string; page: number }

const props = defineProps<{
  job: components["schemas"]["JobView"];
  source: components["schemas"]["SourceView"] | undefined;
  domainName: string | undefined;
  collectionNames: string[];
  note: string | undefined;
  summary: string | undefined;
  translation: string | undefined;
  pdfUrl: string | undefined;
  evidence: components["schemas"]["EvidenceView"] | undefined;
  activeEvidenceId: string;
  status: string;
  textContent: ReadonlyMap<string, string>;
  fileUrls: ReadonlyMap<string, string>;
}>();
const emit = defineEmits<{ select: [evidenceId: string]; refresh: [] }>();
const tab = ref<ReaderTab>("artifacts");
const visualLocation = ref<VisualLocation>();

const tabs = [
  { id: "artifacts", label: "产物", icon: "book" },
  { id: "pipeline", label: "流水线", icon: "pipeline" },
  { id: "metadata", label: "元信息", icon: "info" },
  { id: "visuals", label: "图表", icon: "image" },
] as const;
const evidenceLocator = computed(() => props.evidence?.locator.kind === "pdf" ? props.evidence.locator.value : undefined);
const activeLocation = computed(() => visualLocation.value ?? evidenceLocator.value);
const viewerSrc = computed(() => {
  if (!props.pdfUrl) return undefined;
  return activeLocation.value ? `${props.pdfUrl}#page=${activeLocation.value.page}` : props.pdfUrl;
});
const documentText = computed(() => {
  const artifact = props.job.artifacts.find((item) => item.kind === "document_structure");
  return artifact ? props.textContent.get(artifact.artifact_id) : undefined;
});
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

function locateVisual(page: number, bbox: components["schemas"]["PdfRect"], label: string): void {
  visualLocation.value = { page, bbox, label };
}
function coordinate(value: number): string { return value.toFixed(1); }
watch(() => props.activeEvidenceId, () => { visualLocation.value = undefined; });
</script>

<template>
  <section
    class="reader-card card"
    aria-labelledby="reader-title"
  >
    <div class="reader-heading">
      <div>
        <p class="eyebrow">
          Knowledge result
        </p>
        <h2 id="reader-title">
          内容工作台
        </h2>
      </div>
      <div
        class="reader-tabs"
        role="tablist"
        aria-label="内容工作台"
      >
        <button
          v-for="item in tabs"
          :id="`tab-${item.id}`"
          :key="item.id"
          type="button"
          role="tab"
          :aria-selected="tab === item.id"
          :aria-controls="`panel-${item.id}`"
          @click="tab = item.id"
        >
          <UiIcon
            :name="item.icon"
            :size="15"
          />{{ item.label }}
        </button>
      </div>
    </div>

    <div class="reader-layout">
      <div class="reader-content">
        <section
          v-if="tab === 'artifacts'"
          id="panel-artifacts"
          role="tabpanel"
          aria-labelledby="tab-artifacts"
          class="knowledge-output"
        >
          <header class="output-heading">
            <p class="eyebrow">
              Research notes
            </p><h2>智能笔记</h2>
            <p class="meta">
              引用标记可定位到右侧 PDF 的原文页与坐标。
            </p>
          </header>
          <MarkdownContent
            v-if="note"
            :content="note"
            :active-evidence-id="activeEvidenceId"
            @select="emit('select', $event)"
          />
          <p
            v-else
            class="empty-state"
          >
            智能笔记尚未生成。Pipeline 完成后会在这里显示。
          </p>
          <section class="summary-note">
            <p class="eyebrow">
              中文摘要
            </p>
            <MarkdownContent
              v-if="summary"
              :content="summary"
              :active-evidence-id="activeEvidenceId"
              @select="emit('select', $event)"
            />
            <p
              v-else
              class="empty-state"
            >
              当前成果没有摘要。
            </p>
          </section>
          <details
            v-if="translation"
            class="translation-block"
          >
            <summary>查看全文翻译</summary>
            <MarkdownContent
              :content="translation"
              :active-evidence-id="activeEvidenceId"
              @select="emit('select', $event)"
            />
          </details>
          <JobPanel
            :job="job"
            mode="artifacts"
            :text-content="textContent"
            :file-urls="fileUrls"
            @refresh="emit('refresh')"
          />
        </section>

        <section
          v-else-if="tab === 'visuals'"
          id="panel-visuals"
          role="tabpanel"
          aria-labelledby="tab-visuals"
        >
          <header class="panel-intro">
            <p class="eyebrow">
              Figures &amp; tables
            </p><h2>图表与原文区域</h2>
            <p class="meta">
              从 DocumentStructure 读取 caption、页码与 bbox；点击即可在右侧核对原文。
            </p>
          </header>
          <VisualCatalog
            :artifacts="job.artifacts"
            :document-text="documentText"
            :file-urls="fileUrls"
            @locate="locateVisual"
          />
        </section>

        <JobPanel
          v-else-if="tab === 'pipeline'"
          id="panel-pipeline"
          :job="job"
          mode="pipeline"
          :text-content="textContent"
          :file-urls="fileUrls"
          @refresh="emit('refresh')"
        />

        <section
          v-else
          id="panel-metadata"
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
              <h3>内容归属</h3>
              <dl>
                <div><dt>标题</dt><dd>{{ source?.title ?? source?.canonical_ref ?? "—" }}</dd></div>
                <div><dt>领域</dt><dd>{{ domainName ?? "未设置" }}</dd></div>
                <div><dt>分类</dt><dd>{{ collectionNames.join("、") || "未归分类" }}</dd></div>
                <div><dt>来源</dt><dd>{{ source ? sourceLabels[source.kind] : "—" }}</dd></div>
                <div><dt>规范引用</dt><dd>{{ source?.canonical_ref ?? "—" }}</dd></div>
              </dl>
            </section>
            <section>
              <h3>成果状态</h3>
              <dl>
                <div><dt>当前状态</dt><dd>{{ stateLabels[job.state] }}</dd></div>
                <div><dt>触发方式</dt><dd>{{ triggerLabels[job.trigger] }}</dd></div>
                <div><dt>全文翻译</dt><dd>{{ job.inputs.translate ? "已请求" : "未请求" }}</dd></div>
                <div><dt>Pipeline</dt><dd>{{ job.tasks.length }} 个步骤，{{ job.artifacts.length }} 项产物</dd></div>
                <div><dt>历史成果</dt><dd>{{ source?.previous_job_id ? "保留上一版" : "暂无上一版" }}</dd></div>
              </dl>
            </section>
          </div>
          <details class="technical-details">
            <summary>查看内部标识</summary>
            <dl>
              <div><dt>Source ID</dt><dd>{{ source?.source_id ?? "—" }}</dd></div>
              <div><dt>Current Job</dt><dd>{{ source?.current_job_id ?? "—" }}</dd></div>
              <div><dt>Previous Job</dt><dd>{{ source?.previous_job_id ?? "—" }}</dd></div>
              <div><dt>Pipeline revision</dt><dd>{{ job.pipeline_revision_id }}</dd></div>
            </dl>
          </details>
        </section>
      </div>

      <aside
        class="evidence-panel"
        aria-label="PDF 证据定位"
      >
        <div class="evidence-head">
          <span><i /> 原文与 Evidence</span>
          <a
            v-if="viewerSrc"
            :href="viewerSrc"
            target="_blank"
            rel="noopener"
          >新窗口打开</a>
        </div>
        <p
          class="evidence-status"
          aria-live="polite"
        >
          {{ visualLocation ? `已定位图表：${visualLocation.label}` : status }}
        </p>
        <blockquote v-if="evidence && evidenceLocator && !visualLocation">
          {{ evidence.quote }}
        </blockquote>
        <dl
          v-if="activeLocation"
          class="locator-grid"
        >
          <div><dt>页码</dt><dd>{{ activeLocation.page }}</dd></div>
          <div><dt>坐标</dt><dd>{{ coordinate(activeLocation.bbox.x1) }}, {{ coordinate(activeLocation.bbox.y1) }} → {{ coordinate(activeLocation.bbox.x2) }}, {{ coordinate(activeLocation.bbox.y2) }}</dd></div>
        </dl>
        <object
          v-if="viewerSrc"
          :key="viewerSrc"
          :data="viewerSrc"
          type="application/pdf"
          class="pdf-viewer"
        >
          <p>浏览器无法内嵌 PDF，请在新窗口打开。</p>
        </object>
        <p
          v-else
          class="empty-state"
        >
          缺少原始 PDF，无法定位页面。
        </p>
      </aside>
    </div>
  </section>
</template>
