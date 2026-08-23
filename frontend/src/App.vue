<script setup lang="ts">
import PdfReader from "./components/PdfReader.vue";
import RerunPanel from "./components/RerunPanel.vue";
import SearchPanel from "./components/SearchPanel.vue";
import { usePdfWorkspace } from "./composables/usePdfWorkspace";

const workspace = usePdfWorkspace();
const {
  setup, selectedFile, job, source, busy, notice, evidence, activeEvidenceId, evidenceStatus,
  textContent, fileUrls, pdfUrl, noteText, summaryText, translationText, sourceTitle,
  chooseFile, submit, selectEvidence, refreshJob, openJob,
} = workspace;

function shortId(value: string): string { return `${value.slice(0, 8)}…${value.slice(-4)}`; }
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a
        class="brand"
        href="#workspace"
        aria-label="Flori PDF 工作台"
      >
        <span class="brand-mark">✦</span>
        <span><strong>Flori</strong><small>PDF knowledge</small></span>
      </a>
      <nav
        class="side-nav"
        aria-label="工作台导航"
      >
        <a
          class="is-active"
          href="#workspace"
        ><span>▣</span>阅读工作台</a>
        <a href="#upload"><span>↑</span>上传 PDF</a>
        <a
          v-if="job"
          href="#pipeline"
        ><span>⌁</span>运行状态</a>
        <a
          v-if="job"
          href="#operations"
        ><span>···</span>更多操作</a>
      </nav>
      <div
        v-if="job"
        class="side-current"
      >
        <p class="eyebrow">
          当前文献
        </p>
        <strong>{{ sourceTitle }}</strong>
        <span
          class="status-pill"
          :class="`state-${job.state}`"
        >{{ job.state }}</span>
        <small>Job {{ shortId(job.job_id) }}</small>
      </div>
      <footer class="side-footer">
        <span class="health-dot" /> vNext preview
      </footer>
    </aside>

    <div class="app-main">
      <header class="topbar">
        <div class="breadcrumb">
          <span>知识库</span><b>/</b><strong>{{ job ? sourceTitle : "PDF 工作台" }}</strong>
        </div>
        <SearchPanel @open="openJob" />
      </header>

      <main
        id="workspace"
        class="page"
      >
        <section
          v-if="job"
          class="document-head card"
        >
          <div class="document-icon">
            ▤
          </div>
          <div class="document-copy">
            <p class="eyebrow">
              {{ source?.kind ?? "PDF" }} · {{ job.trigger }}
            </p>
            <h1>{{ sourceTitle }}</h1>
            <p class="meta">
              Job {{ job.job_id }} · revision {{ shortId(job.pipeline_revision_id) }}
            </p>
          </div>
          <span
            class="status-pill"
            :class="`state-${job.state}`"
          >{{ job.state }}</span>
          <button
            type="button"
            class="btn secondary"
            @click="refreshJob"
          >
            刷新
          </button>
        </section>
        <section
          v-else
          class="welcome"
        >
          <p class="eyebrow">
            Flori research workspace
          </p>
          <h1>把论文变成可检索、可回到原文的研究笔记</h1>
          <p>上传数字版 PDF，Flori 会提取结构与图表、生成中文笔记，并由 Rust 校验每条证据。</p>
        </section>

        <details
          id="upload"
          class="upload-card card"
          :open="!job"
        >
          <summary>
            <span><b>上传并解析 PDF</b><small>首次流程先发布笔记，不自动翻译全文</small></span>
            <span class="summary-action">选择文件</span>
          </summary>
          <div class="upload-body">
            <label
              class="file-drop"
              for="pdf-file"
            >
              <span class="file-icon">PDF</span>
              <span><b>{{ selectedFile?.name ?? "选择数字版 PDF" }}</b><small>浏览器先计算 SHA-256，再安全上传</small></span>
            </label>
            <input
              id="pdf-file"
              class="visually-hidden"
              type="file"
              accept="application/pdf,.pdf"
              :disabled="busy"
              @change="chooseFile"
            >
            <button
              type="button"
              class="btn primary"
              :disabled="!selectedFile || !setup || busy"
              @click="submit"
            >
              {{ busy ? "处理中…" : "开始解析" }}
            </button>
          </div>
        </details>

        <p
          class="notice-bar"
          aria-live="polite"
        >
          {{ notice }}
        </p>

        <PdfReader
          v-if="job"
          :job="job"
          :note="noteText"
          :summary="summaryText"
          :translation="translationText"
          :pdf-url="pdfUrl"
          :evidence="evidence"
          :active-evidence-id="activeEvidenceId"
          :status="evidenceStatus"
          :text-content="textContent"
          :file-urls="fileUrls"
          @select="selectEvidence"
          @refresh="refreshJob"
        />
        <RerunPanel
          v-if="job"
          :job="job"
          @created="openJob"
          @refresh="refreshJob"
        />
      </main>
    </div>
  </div>
</template>
