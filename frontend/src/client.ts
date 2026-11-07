import createClient from "openapi-fetch";

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
export const feedKindText: Record<Schemas["FeedKind"], string> = {
  bili_up: "B站 UP 主投稿", bili_season: "B站合集", bili_series: "B站系列", bili_fav: "B站收藏夹",
  yt_channel: "YouTube 频道", yt_playlist: "YouTube 播放列表",
};

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

// Server times are already +08:00, so the wall-clock prefix is the local time.
export function shortTime(rfc3339: string): string {
  return rfc3339.slice(0, 16).replace("T", " ");
}
