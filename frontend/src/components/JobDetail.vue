<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  api, duration, failedAt, failure, fileText, fileUrl, isActive, jobStatusText, platformText, seconds, shortTime, stepStatusText,
  stepText, type Schemas,
} from "../client";
import JobMeta from "./JobMeta.vue";
import MarkdownView from "./MarkdownView.vue";

// `tab` is the path segment after /jobs/<id>/: a logical .md file name or "meta"; empty or
// unknown shows the note. App keeps this component mounted while only the tab changes.
const props = defineProps<{ id: number; tab: string; domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();

const docOrder = ["note", "translation", "original", "transcript"];
const META_TAB = "meta";
// The executor opens each attempt's part of job.log with this line (contract 1).
const LOG_HEADER = /^== (\d{2}-[a-z-]+) attempt \d+ ==$/;
const skipText: Record<string, string> = { chinese: "原文是中文,不需要翻译", unknown: "无法判断语言,未翻译", too_long: "篇幅过长,未翻译" };

const callFileText: Record<string, string> = {
  "prompt.md": "发出的 prompt", "call.json": "调用信息", "out.json": "原始输出", "out.txt": "原始输出", "err.txt": "stderr 末尾",
};
const transcriptSource: Record<string, string> = { manual: "人工字幕", ai: "B站 AI 字幕" };
const htmlSource: Record<string, string> = { arxiv: "arXiv 官方 HTML", ar5iv: "ar5iv" };
const translationSource: Record<string, string> = { ai: "AI 翻译", subtitle: "中文人工字幕" };

// A few manifest meta values with Chinese labels; abstract, authors, title and vault_path stay out.
// skipped_reason and the punctuation counts have their own sentences below the step.
function metaChips(meta: Schemas["StepInfo"]["meta"]): string[] {
  const text = (key: string) => { const value = meta[key]; return typeof value === "string" && value ? value : undefined; };
  const number = (key: string) => { const value = meta[key]; return typeof value === "number" ? value : undefined; };
  const chips: string[] = [];
  const ms = number("duration_ms");
  if (ms !== undefined) chips.push(`时长 ${duration(Math.round(ms / 1000))}`);
  if (text("uploader")) chips.push(`上传者 ${text("uploader")}`);
  if (text("published")) chips.push(`发布于 ${text("published")}`);
  const origin = text("origin");
  if (origin === "whisper") chips.push("转写来源 whisper");
  else if (origin === "subtitle") chips.push(`转写来源 ${transcriptSource[text("subtitle_kind") ?? ""] ?? "字幕"}`);
  const language = text("language");
  const probability = number("language_probability");
  if (language) chips.push(`语言 ${language}${probability === undefined ? "" : ` (${(probability * 100).toFixed(1)}%)`}`);
  if (text("subtitle_language")) chips.push(`字幕语言 ${text("subtitle_language")}`);
  if (text("version")) chips.push(`arXiv 版本 ${text("version")}`);
  if (text("primary_category")) chips.push(`分类 ${text("primary_category")}`);
  const html = text("html_source");
  if (html) chips.push(`网页来源 ${htmlSource[html] ?? html}`);
  const translation = text("translation_source");
  if (translation) chips.push(`译文来源 ${translationSource[translation] ?? translation}`);
  const chunks = number("chunks");
  if (chunks !== undefined) chips.push(`${chunks} 块`);
  return chips;
}

function punctuationText({ sections, kept_original: kept, corrected, reverted }: Schemas["Punctuation"]): string {
  if (kept >= sections) return `原文是中文,不翻译;AI 的回复都没能用上,${sections} 节全部保留原转写`;
  let text = "原文是中文,不翻译,由 AI 加了标点";
  if (corrected > 0) text += `,改了 ${corrected} 处同音错字`;
  if (reverted > 0) text += `;AI 的其它 ${reverted} 处改动已还原为原转写`;
  if (kept > 0) text += `;${kept}/${sections} 节未加标点,保留原转写`;
  return text;
}

const detail = ref<Schemas["JobDetail"]>();
const missing = ref(false);
const log = ref("");
const logBox = ref<HTMLPreElement>();
const message = ref("");
const busy = ref(false);
// Job-relative path -> Markdown text; null when loading failed. Finished step files never change.
const texts = ref(new Map<string, string | null>());
// AI call records by job-relative path; null when loading failed. They hold untrusted transcript
// and model text, so they are only ever shown as plain text. Fetched on every open: a running
// call's info is completed when it ends.
const callTexts = ref(new Map<string, string | null>());
// Wall clock of the last poll; running steps count up from their start with it.
const now = ref(Date.now());
let offset = 0;
let polling = false;
let timer: number | undefined;

const job = computed(() => detail.value?.job);
const domainName = computed(() => props.domains.find((item) => item.id === job.value?.domain_id)?.name);
const collectionName = computed(() => props.collections.find((item) => item.id === job.value?.collection_id)?.name);
const place = computed(() => [domainName.value, collectionName.value].filter(Boolean).join(" / "));
const docs = computed(() => (detail.value?.files ?? [])
  .filter((file) => file.path.endsWith(".md"))
  .sort((a, b) => rank(a.name) - rank(b.name)));
// Real links, so back, forward and copying the address work; App navigates them in place.
const tabs = computed(() => [
  { name: "note", label: fileText.note, href: `/jobs/${props.id}` },
  ...docs.value.filter((file) => file.name !== "note")
    .map((file) => ({ name: file.name, label: fileText[file.name] ?? file.name, href: `/jobs/${props.id}/${file.name}` })),
  { name: META_TAB, label: "元信息", href: `/jobs/${props.id}/${META_TAB}` },
]);
const active = computed(() => (tabs.value.some((item) => item.name === props.tab) ? props.tab : "note"));
const current = computed(() => (active.value === META_TAB ? undefined : docs.value.find((file) => file.name === active.value)));
const video = computed(() => detail.value?.files.find((file) => file.name === "video"));
const showVideo = computed(() => active.value === "note" || active.value === "transcript");
// job.log by step directory name (`<nn>-<name>`); all attempts of a step, headers included.
const stepLogs = computed(() => {
  const parts = new Map<string, string[]>();
  let key = "";
  for (const line of log.value.split("\n")) {
    key = LOG_HEADER.exec(line)?.[1] ?? key;
    if (!key) continue;
    if (!parts.has(key)) parts.set(key, []);
    parts.get(key)?.push(line);
  }
  return new Map([...parts].map(([name, lines]) => [name, lines.join("\n").trimEnd()]));
});
const rows = computed(() => (detail.value?.steps ?? []).map((step) => {
  const text = stepLogs.value.get(`${String(step.seq).padStart(2, "0")}-${step.name}`) ?? "";
  const lines = text.split("\n").slice(1).filter((line) => line.trim() && !LOG_HEADER.test(line));
  return {
    step,
    chips: metaChips(step.meta),
    log: text,
    last: lines.at(-1) ?? "",
    // Pending steps have no start; a step between automatic retries stays running and keeps counting.
    seconds: step.started && (step.finished || step.status === "running")
      ? seconds(step.started, step.finished, now.value)
      : undefined,
  };
}));
const total = computed(() => rows.value.reduce((sum, row) => sum + (row.seconds ?? 0), 0));
const failedStep = computed(() => detail.value?.steps.find((step) => step.status === "failed"));

function rank(name: string): number {
  const index = docOrder.indexOf(name);
  return index < 0 ? docOrder.length : index;
}

async function refresh(): Promise<void> {
  const { data, response } = await api.GET("/api/jobs/{id}", { params: { path: { id: props.id } } });
  if (data) detail.value = data;
  else if (response.status === 404) missing.value = true;
}

// Returns whether new text arrived, so a finished job can drain the rest of its log.
async function pullLog(): Promise<boolean> {
  const { data } = await api.GET("/api/jobs/{id}/logs", { params: { path: { id: props.id }, query: { offset } } });
  if (!data) return false;
  offset = data.next_offset;
  if (!data.text) return false;
  const box = logBox.value;
  const following = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  log.value += data.text;
  if (following) {
    await nextTick();
    logBox.value?.scrollTo({ top: logBox.value.scrollHeight });
  }
  return true;
}

async function loop(): Promise<void> {
  try {
    await Promise.all([refresh(), pullLog()]);
    now.value = Date.now();
    if (missing.value || (job.value && !isActive(job.value.status))) {
      polling = false;
      let more = true;
      while (more) more = await pullLog();
    }
  } catch {
    // A dropped request is retried on the next round.
  }
  if (polling) timer = window.setTimeout(() => void loop(), 1500);
}

function watchJob(): void {
  if (polling) return;
  polling = true;
  void loop();
}

async function act(action: "retry" | "cancel"): Promise<void> {
  if (action === "cancel" && !window.confirm("取消这个任务?正在运行的步骤会被终止。")) return;
  busy.value = true;
  message.value = "";
  const options = { params: { path: { id: props.id } } };
  const { error, response } = await (action === "retry"
    ? api.POST("/api/jobs/{id}/retry", options)
    : api.POST("/api/jobs/{id}/cancel", options)).finally(() => { busy.value = false; });
  if (!response.ok) {
    message.value = failure(error, response);
    return;
  }
  await refresh();
  watchJob();
}

async function load(path: string): Promise<void> {
  if (texts.value.has(path) || !detail.value) return;
  const response = await fetch(fileUrl(detail.value.preview_base, path));
  texts.value.set(path, response.ok ? await response.text() : null);
}

// pullLog cannot scroll the folded box, so it would open at the top. Opening jumps to the newest
// lines, which pullLog then keeps in view.
function openLog(event: Event): void {
  const box = logBox.value;
  if (box && (event.target as HTMLDetailsElement).open) box.scrollTo({ top: box.scrollHeight });
}

async function openCall(call: Schemas["AiCall"], event: Event): Promise<void> {
  const base = detail.value?.preview_base;
  if (!base || !(event.target as HTMLDetailsElement).open) return;
  await Promise.all(call.files.map(async (path) => {
    try {
      const response = await fetch(fileUrl(base, path));
      callTexts.value.set(path, response.ok ? await response.text() : null);
    } catch {
      callTexts.value.set(path, null);
    }
  }));
}

function callFileKind(path: string): string {
  const name = path.split("/").pop() ?? path;
  const kind = name.slice(name.indexOf(".") + 1);
  return callFileText[kind] ?? kind;
}

// The job files a call sent as images, from its call.json; only plain job-relative paths.
function sentImages(path: string): string[] {
  const text = callTexts.value.get(path);
  if (!path.endsWith(".call.json") || !text) return [];
  try {
    const images: unknown = (JSON.parse(text) as { images?: unknown }).images;
    if (!Array.isArray(images)) return [];
    return images.filter((image): image is string =>
      typeof image === "string" && !image.startsWith("/") && !image.split("/").includes(".."));
  } catch {
    return [];
  }
}

watch(() => current.value?.path, (path) => { if (path) void load(path).catch(() => texts.value.set(path, null)); });
onMounted(watchJob);
onBeforeUnmount(() => {
  polling = false;
  window.clearTimeout(timer);
});
</script>

<template>
  <p v-if="missing" class="empty">任务不存在。<a href="/">回到任务列表</a></p>
  <p v-else-if="!detail || !job" class="empty">加载中…</p>
  <div v-else class="stack">
    <header class="card head">
      <div class="grow">
        <h1>{{ job.title || job.source_id }}</h1>
        <p class="meta">
          #{{ job.id }} · {{ platformText[job.platform] }} · {{ place }} · {{ shortTime(job.created) }}
          · {{ job.translate === "auto" ? "自动翻译" : "不翻译" }} · <a :href="job.url" target="_blank" rel="noopener">原始链接</a>
        </p>
        <p v-if="total > 0 || failedStep" class="meta">
          <template v-if="total > 0">各步共用时 {{ duration(total) }}</template>
          <template v-if="total > 0 && failedStep"> · </template>
          <span v-if="failedStep" class="danger">{{ failedAt(failedStep.name) }}</span>
        </p>
        <p v-if="detail.vault_path" class="meta">已发布到 {{ detail.vault_path }}</p>
      </div>
      <div class="row">
        <span class="pill" :class="`state-${job.status}`">{{ jobStatusText[job.status] }}</span>
        <a v-if="detail.obsidian_uri" class="btn primary" :href="detail.obsidian_uri">在 Obsidian 中打开</a>
        <button v-if="job.status === 'failed' || job.status === 'canceled'" class="btn" :disabled="busy" @click="act('retry')">重试</button>
        <button v-if="isActive(job.status)" class="btn danger-outline" :disabled="busy" @click="act('cancel')">取消</button>
      </div>
      <p v-if="message" class="error full">{{ message }}</p>
    </header>

    <div class="detail">
      <div class="stack">
        <nav class="tabs">
          <a v-for="item in tabs" :key="item.name" :href="item.href" :aria-current="active === item.name ? 'page' : undefined">{{ item.label }}</a>
        </nav>
        <!-- v-show keeps the playback position when 元信息 is opened and closed. -->
        <video
          v-if="video" v-show="showVideo" class="card video" controls preload="metadata"
          :src="fileUrl(detail.preview_base, video.path)"
        />
        <JobMeta v-if="active === META_TAB" :detail :domain="domainName" :collection="collectionName" :spent="total" />
        <section v-else class="card">
          <p v-if="!current" class="empty">笔记生成后在这里预览。</p>
          <p v-else-if="!texts.has(current.path)" class="empty">加载中…</p>
          <p v-else-if="texts.get(current.path) === null" class="error">文件加载失败。</p>
          <MarkdownView v-else :source="texts.get(current.path) ?? ''" :base="detail.preview_base" :assets="detail.assets" />
        </section>
        <section class="card">
          <details @toggle="openLog">
            <summary class="fold">完整日志</summary>
            <pre ref="logBox" class="log">{{ log || "暂无日志" }}</pre>
          </details>
        </section>
      </div>

      <aside class="stack">
        <section class="card">
          <h2>步骤</h2>
          <ol class="steps">
            <li v-for="{ step, chips, log: stepLog, last, seconds: spent } in rows" :key="step.seq">
              <div class="row">
                <b class="grow">{{ stepText[step.name] }}</b>
                <small v-if="spent !== undefined" class="meta">{{ duration(spent) }}</small>
                <small v-if="step.attempts > 0" class="meta">尝试 {{ step.attempts }} 次</small>
                <span v-if="step.exit_code === 30" class="pill state-failed">凭据失效</span>
                <span class="pill" :class="`state-${step.status}`">{{ stepStatusText[step.status] }}</span>
              </div>
              <p v-if="step.error" class="error">{{ step.error }}</p>
              <p v-if="step.skipped_reason" class="meta">{{ skipText[step.skipped_reason] ?? `已跳过:${step.skipped_reason}` }}</p>
              <p v-if="step.punctuation" class="meta">{{ punctuationText(step.punctuation) }}</p>
              <div v-if="chips.length" class="chips">
                <span v-for="chip in chips" :key="chip" class="chip">{{ chip }}</span>
              </div>
              <div v-if="step.files.length" class="step-files">
                <a v-for="file in step.files" :key="file.path" :href="fileUrl(detail.preview_base, file.path)" target="_blank" rel="noopener">
                  {{ fileText[file.name] ?? file.name }}
                </a>
              </div>
              <p v-if="step.status === 'running' && last" class="meta tail">{{ last }}</p>
              <details v-if="stepLog" class="step-log">
                <summary class="meta">本步日志</summary>
                <pre class="log">{{ stepLog }}</pre>
              </details>
              <details v-if="step.ai_calls.length" class="step-log">
                <summary class="meta">AI 调用 {{ step.ai_calls.length }} 次</summary>
                <details v-for="call in step.ai_calls" :key="call.name" class="ai-call" @toggle="openCall(call, $event)">
                  <summary class="meta">第 {{ call.attempt }} 次尝试 · 第 {{ call.index }} 次调用</summary>
                  <div v-for="path in call.files" :key="path" class="ai-file">
                    <div class="row">
                      <small class="meta grow">{{ callFileKind(path) }}</small>
                      <a class="meta" :href="fileUrl(detail.preview_base, path)" target="_blank" rel="noopener">原文件</a>
                    </div>
                    <p v-if="!callTexts.has(path)" class="meta">加载中…</p>
                    <p v-else-if="callTexts.get(path) === null" class="error">加载失败。</p>
                    <pre v-else class="log">{{ callTexts.get(path) || "(空)" }}</pre>
                    <div v-if="sentImages(path).length" class="thumbs">
                      <a v-for="image in sentImages(path)" :key="image" :href="fileUrl(detail.preview_base, image)" target="_blank" rel="noopener">
                        <img :src="fileUrl(detail.preview_base, image)" :alt="image" loading="lazy">
                      </a>
                    </div>
                  </div>
                </details>
              </details>
            </li>
          </ol>
        </section>
      </aside>
    </div>
  </div>
</template>
