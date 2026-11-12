<script setup lang="ts">
import { computed } from "vue";

import { bytes, duration, fileText, fileUrl, jobStatusText, platformText, shortTime, type Schemas } from "../client";

const props = defineProps<{
  detail: Schemas["JobDetail"]; domain: string | undefined; collection: string | undefined; spent: number;
}>();

// Every value is shown as plain text; href only takes the links the server built.
type Row = { label: string; text: string; href?: string; newTab?: boolean; pill?: string };

const transcriptSource: Record<string, string> = { manual: "人工字幕", ai: "B站 AI 字幕" };
const htmlSource: Record<string, string> = { arxiv: "arXiv 官方 HTML", ar5iv: "ar5iv" };
const translationSource: Record<string, string> = { ai: "AI 翻译", subtitle: "中文人工字幕" };
const languageText: Record<string, string> = {
  zh: "中文", en: "英语", ja: "日语", ko: "韩语", fr: "法语", de: "德语", es: "西班牙语", ru: "俄语", other: "其它",
};

type Meta = Schemas["StepInfo"]["meta"];

function stepMeta(name: Schemas["StepName"]): Meta {
  return props.detail.steps.find((step) => step.name === name)?.meta ?? {};
}

function text(meta: Meta, key: string): string | undefined {
  const value = meta[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function number(meta: Meta, key: string): number | undefined {
  const value = meta[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function list(meta: Meta, key: string): string[] {
  const value = meta[key];
  return Array.isArray(value) ? value.filter((item) => item.trim()) : [];
}

function language(code: string | undefined, probability?: number): string | undefined {
  if (!code || code === "unknown") return undefined;
  const name = languageText[code] ?? code;
  return probability === undefined ? name : `${name} (${(probability * 100).toFixed(1)}%)`;
}

// Rows without a value are left out.
function rows(entries: [string, string | undefined, Partial<Row>?][]): Row[] {
  return entries.flatMap(([label, value, extra]) => (value ? [{ label, text: value, ...extra }] : []));
}

const content = computed((): Row[] => {
  const { job } = props.detail;
  const video = job.pipeline === "video";
  const fetched = stepMeta(video ? "video-fetch" : "arxiv-fetch");
  const transcribed = stepMeta("video-transcribe");
  const translated = stepMeta("translate");
  // Trimmed like the meta values, so edge spaces alone never make an 原标题 row.
  const title = job.title?.trim() || job.source_id;
  const original = text(fetched, "title");
  const ms = number(fetched, "duration_ms");
  const [width, height] = [number(fetched, "width"), number(fetched, "height")];
  const fps = number(fetched, "fps");
  const size = number(fetched, "filesize_bytes");
  const origin = text(transcribed, "origin");
  const kind = text(transcribed, "subtitle_kind");
  const source = origin === "whisper" ? "whisper" : origin === "subtitle" ? transcriptSource[kind ?? ""] ?? "字幕" : undefined;
  const subtitle = text(fetched, "subtitle_language") ?? (origin === "subtitle" ? text(transcribed, "language") : undefined);
  const spoken = video
    ? language(text(transcribed, "language"), number(transcribed, "language_probability"))
    : language(text(translated, "language"));
  const categories = list(fetched, "categories");
  const html = text(fetched, "html_source");
  const translation = text(translated, "translation_source");
  return rows([
    ["标题", title],
    ["原标题", original !== title ? original : undefined],
    ["平台", platformText[job.platform]],
    ["来源 ID", job.source_id],
    [video ? "UP 主" : "作者", video ? text(fetched, "uploader") : list(fetched, "authors").join("、")],
    ["发布时间", text(fetched, "published")],
    ["时长", ms === undefined ? undefined : duration(Math.round(ms / 1000))],
    ["分辨率", width && height ? `${width}×${height}` : undefined],
    ["帧率", fps === undefined ? undefined : `${Number(fps.toFixed(2))} fps`],
    ["视频编码", text(fetched, "vcodec")],
    ["音频编码", text(fetched, "acodec")],
    ["文件大小", size === undefined ? undefined : bytes(size)],
    ["标签", list(fetched, "tags").join("、")],
    ["语言", spoken],
    ["转写来源", source],
    ["字幕语言", source && origin === "subtitle" ? language(subtitle) : undefined],
    ["中文译文来源", translation ? translationSource[translation] ?? translation : undefined],
    ["arXiv 版本", text(fetched, "version")],
    ["分类", categories.length ? categories.join("、") : text(fetched, "primary_category")],
    ["期刊/会议", text(fetched, "journal_ref")],
    ["备注", text(fetched, "comment")],
    ["网页来源", html ? htmlSource[html] ?? html : undefined],
    ["摘要", text(fetched, "abstract")],
    ["原始链接", job.url, { href: job.url, newTab: true }],
  ]);
});

const processing = computed((): Row[] => {
  const { job, ai, steps } = props.detail;
  const calls = steps.reduce((sum, step) => sum + step.ai_calls.length, 0);
  const models = ai && [
    ai.tool, `笔记 ${ai.model_note}`, job.translate === "auto" ? `翻译 ${ai.model_translate}` : undefined,
  ].filter(Boolean).join(" · ");
  return rows([
    ["任务序号", `#${job.id}`],
    ["任务 ID", job.job_key],
    ["状态", jobStatusText[job.status], { pill: `state-${job.status}` }],
    ["领域", props.domain],
    ["集合", props.collection],
    ["翻译设置", job.translate === "auto" ? "自动翻译" : "不翻译"],
    ["创建于", shortTime(job.created)],
    ["更新于", shortTime(job.updated)],
    ["各步共用时", props.spent > 0 ? duration(props.spent) : undefined],
    ["AI 工具与模型", models || undefined],
    ["CLI 版本", ai?.cli_version],
    ["AI 调用次数", calls > 0 ? `${calls} 次` : undefined],
    ["vault 路径", props.detail.vault_path ?? undefined],
    ["Obsidian 链接", props.detail.obsidian_uri ?? undefined, { href: props.detail.obsidian_uri ?? "" }],
  ]);
});

const cards = computed(() => [
  { heading: "内容信息", rows: content.value, files: false },
  { heading: "处理信息", rows: processing.value, files: true },
]);
</script>

<template>
  <section v-for="card in cards" :key="card.heading" class="card">
    <h2>{{ card.heading }}</h2>
    <table class="kv">
      <tbody>
        <tr v-for="row in card.rows" :key="row.label">
          <th scope="row">{{ row.label }}</th>
          <td>
            <a v-if="row.href && row.newTab" :href="row.href" target="_blank" rel="noopener">{{ row.text }}</a>
            <a v-else-if="row.href" :href="row.href">{{ row.text }}</a>
            <span v-else-if="row.pill" class="pill" :class="row.pill">{{ row.text }}</span>
            <template v-else>{{ row.text }}</template>
          </td>
        </tr>
      </tbody>
    </table>
    <details v-if="card.files && detail.files.length" class="artifacts">
      <summary class="meta">产物文件 {{ detail.files.length }} 个</summary>
      <ul class="files">
        <li v-for="file in detail.files" :key="file.name">
          <a :href="fileUrl(detail.preview_base, file.path)" target="_blank" rel="noopener">{{ fileText[file.name] ?? file.name }}</a>
          <small class="meta">{{ file.path }}</small>
        </li>
      </ul>
    </details>
  </section>
</template>
