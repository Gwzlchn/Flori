import createClient from "openapi-fetch";
import { onBeforeUnmount } from "vue";

import type { components, paths } from "./openapi";

// Same origin; the browser supplies Basic Auth credentials.
export const api = createClient<paths>({ baseUrl: "" });

export type Schemas = components["schemas"];

export const jobStatusText: Record<Schemas["JobStatus"], string> = {
  queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败", canceled: "已取消",
};
export const stepStatusText: Record<Schemas["StepStatus"], string> = {
  pending: "等待", running: "运行中", succeeded: "完成", failed: "失败", canceled: "已取消",
};
export const stepText: Record<Schemas["StepName"], string> = {
  "video-fetch": "下载视频", "video-transcribe": "转写", "video-frames": "关键帧",
  "arxiv-fetch": "获取论文", "arxiv-convert": "转换正文", note: "AI 笔记", translate: "翻译", publish: "发布",
};
export const platformText: Record<Schemas["Platform"], string> = { bilibili: "B站", youtube: "YouTube", arxiv: "arXiv" };
// Logical file names from the manifests (contract 1); other names show as they are.
export const fileText: Record<string, string> = {
  note: "AI 笔记", translation: "译文", original: "原文", transcript: "转写稿", video: "视频",
  subtitle: "转写字幕", subtitle_zh: "中文字幕", ai_subtitle_zh: "B站 AI 字幕(中文)", ai_subtitle_en: "B站 AI 字幕(英文)",
  manual_subtitle_zh: "人工字幕(中文,待核对)", manual_subtitle_en: "人工字幕(英文,待核对)",
  transcript_json: "转写数据", sections: "分节数据", page: "论文网页", figures: "论文图注数据",
};
export const feedKindText: Record<Schemas["FeedKind"], string> = {
  bili_up: "B站 UP 主投稿", bili_season: "B站合集", bili_series: "B站系列", bili_fav: "B站收藏夹",
  yt_channel: "YouTube 频道", yt_playlist: "YouTube 播放列表",
};

// translate on a Chinese video adds punctuation instead of translating; job.punctuates says so.
export function stepLabel(step: Schemas["StepName"], punctuates: boolean): string {
  return step === "translate" && punctuates ? "加标点" : stepText[step];
}

// A Latin step name such as "AI 笔记" gets a space after the Chinese words before it.
export function failedAt(step: Schemas["StepName"], punctuates: boolean): string {
  const name = stepLabel(step, punctuates);
  return /^[A-Za-z0-9]/.test(name) ? `失败于 ${name}` : `失败于${name}`;
}

// The transcript translate writes for a video it translated holds Chinese beside the original;
// `translate` is that step's meta. Other files go by their logical name.
export function fileLabel(name: string, translate: Schemas["StepInfo"]["meta"] | undefined): string {
  if (name !== "transcript" || translate?.translated !== true) return fileText[name] ?? name;
  return translate.language === "en" ? "中英对照" : "双语对照";
}

export function isActive(status: Schemas["JobStatus"]): boolean {
  return status === "queued" || status === "running";
}

// Non-JSON error bodies (proxy pages, auth prompts) reach here as strings without `message`.
export function failure(error: { message?: string } | undefined, response: Response): string {
  return error?.message || `请求失败(HTTP ${response.status})`;
}

// `preview_base` + a job-relative path; each segment is encoded, the slashes are not.
export function fileUrl(base: string, path: string): string {
  return base + path.split("/").map(encodeURIComponent).join("/");
}

// Whether the open page holds edits it has not saved. App asks before going to another page,
// the browser before reloading or closing the tab.
let unsaved: (() => boolean) | undefined;

// Called from a page's setup; the check is dropped when the page unmounts.
export function guardUnsaved(check: () => boolean): void {
  unsaved = check;
  onBeforeUnmount(() => {
    if (unsaved === check) unsaved = undefined;
  });
}

export function hasUnsaved(): boolean {
  return unsaved?.() ?? false;
}

// True when nothing is unsaved or the reader chooses to drop it.
export function mayLeave(): boolean {
  return !hasUnsaved() || window.confirm("有修改还没保存,离开会丢掉。仍要离开?");
}

// Rows of a textarea holding `text`: its lines and one more to type in, from `least` up to 30.
// Long lines wrap, so this undercounts them; the box still scrolls and resizes.
export function textRows(text: string, least = 6): number {
  return Math.min(30, Math.max(least, text.split("\n").length + 1));
}

// Server times are already +08:00, so the wall-clock prefix is the local time.
export function shortTime(rfc3339: string): string {
  return rfc3339.slice(0, 16).replace("T", " ");
}

// Whole seconds from `started` to `finished`, or to `now` (ms) while unfinished.
export function seconds(started: string, finished: string | null | undefined, now: number): number {
  const end = finished ? Date.parse(finished) : now;
  return Math.max(0, Math.round((end - Date.parse(started)) / 1000));
}

export function duration(total: number): string {
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} 分 ${total % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

export function bytes(size: number): string {
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = size / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}
