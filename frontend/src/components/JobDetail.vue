<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { api, failure, fileUrl, isActive, jobStatusText, platformText, shortTime, stepStatusText, stepText, type Schemas } from "../client";
import MarkdownView from "./MarkdownView.vue";

const props = defineProps<{ id: number; domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();

const docOrder = ["note", "translation", "original", "transcript"];
const fileText: Record<string, string> = {
  note: "AI 笔记", translation: "译文", original: "原文", transcript: "转写", video: "视频",
  subtitle: "转写字幕", subtitle_zh: "中文字幕", transcript_json: "转写数据", sections: "分节数据", page: "论文网页",
};
const skipText: Record<string, string> = { chinese: "原文是中文,不需要翻译", unknown: "无法判断语言,未翻译", too_long: "篇幅过长,未翻译" };

const detail = ref<Schemas["JobDetail"]>();
const missing = ref(false);
const log = ref("");
const logBox = ref<HTMLPreElement>();
const message = ref("");
const busy = ref(false);
const tab = ref("");
// Job-relative path -> Markdown text; null when loading failed. Finished step files never change.
const texts = ref(new Map<string, string | null>());
let offset = 0;
let polling = false;
let timer: number | undefined;

const job = computed(() => detail.value?.job);
const place = computed(() => {
  const domain = props.domains.find((item) => item.id === job.value?.domain_id)?.name;
  const collection = props.collections.find((item) => item.id === job.value?.collection_id)?.name;
  return [domain, collection].filter(Boolean).join(" / ");
});
const docs = computed(() => (detail.value?.files ?? [])
  .filter((file) => file.path.endsWith(".md"))
  .sort((a, b) => rank(a.name) - rank(b.name)));
const current = computed(() => docs.value.find((file) => file.name === tab.value) ?? docs.value[0]);
const video = computed(() => detail.value?.files.find((file) => file.name === "video"));

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
        <video v-if="video" class="card video" controls preload="metadata" :src="fileUrl(detail.preview_base, video.path)" />
        <section class="card">
          <nav v-if="docs.length > 1" class="tabs">
            <button v-for="file in docs" :key="file.name" type="button" :aria-pressed="current?.name === file.name" @click="tab = file.name">
              {{ fileText[file.name] ?? file.name }}
            </button>
          </nav>
          <p v-if="!current" class="empty">笔记生成后在这里预览。</p>
          <p v-else-if="!texts.has(current.path)" class="empty">加载中…</p>
          <p v-else-if="texts.get(current.path) === null" class="error">文件加载失败。</p>
          <MarkdownView v-else :source="texts.get(current.path) ?? ''" :base="detail.preview_base" :assets="detail.assets" />
        </section>
        <section class="card">
          <h2>日志</h2>
          <pre ref="logBox" class="log">{{ log || "暂无日志" }}</pre>
        </section>
      </div>

      <aside class="stack">
        <section class="card">
          <h2>步骤</h2>
          <ol class="steps">
            <li v-for="step in detail.steps" :key="step.seq">
              <div class="row">
                <b class="grow">{{ stepText[step.name] }}</b>
                <small v-if="step.attempts > 0" class="meta">尝试 {{ step.attempts }} 次</small>
                <span v-if="step.exit_code === 30" class="pill state-failed">凭据失效</span>
                <span class="pill" :class="`state-${step.status}`">{{ stepStatusText[step.status] }}</span>
              </div>
              <p v-if="step.error" class="error">{{ step.error }}</p>
              <p v-if="step.skipped_reason" class="meta">{{ skipText[step.skipped_reason] ?? `已跳过:${step.skipped_reason}` }}</p>
            </li>
          </ol>
        </section>
        <section v-if="detail.files.length" class="card">
          <h2>文件</h2>
          <ul class="files">
            <li v-for="file in detail.files" :key="file.name">
              <a :href="fileUrl(detail.preview_base, file.path)" target="_blank" rel="noopener">{{ fileText[file.name] ?? file.name }}</a>
              <small class="meta">{{ file.path.split("/").pop() }}</small>
            </li>
          </ul>
        </section>
      </aside>
    </div>
  </div>
</template>
