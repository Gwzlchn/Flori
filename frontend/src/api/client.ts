import createClient from "openapi-fetch";

import type { components, paths } from "../../.generated/api";

export type { components };

export const apiClient = createClient<paths>({
  headers: {
    "X-Flori-Protocol": "1",
  },
});

export async function watchJobEvents(
  jobId: components["schemas"]["JobId"],
  after: number,
  signal: AbortSignal,
  onCursor: (cursor: number) => void,
): Promise<"ended" | "expired" | "failed"> {
  try {
    const result = await apiClient.GET("/api/v1/jobs/{job_id}/events", {
      params: { path: { job_id: jobId }, header: { "Last-Event-ID": after } },
      parseAs: "stream",
      signal,
    });
    if (result.error?.error.code === "event_cursor_expired") return "expired";
    if (!result.data || !result.response.headers.get("content-type")?.startsWith("text/event-stream")) return "failed";
    const reader = result.data.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) return "ended";
      buffer += chunk.value;
      if (buffer.length > 64 * 1024) return "failed";
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const event = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const id = event.split("\n").find((line) => line.startsWith("id: "))?.slice(4);
        if (id && /^\d+$/.test(id)) {
          const cursor = Number(id);
          if (Number.isSafeInteger(cursor) && cursor > after) {
            after = cursor;
            onCursor(cursor);
          }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
    return "ended";
  } catch {
    return signal.aborted ? "ended" : "failed";
  }
}

export function apiError(
  response: components["schemas"]["ErrorResponse"] | undefined,
  fallback: string,
): string {
  return response ? `${response.error.code}: ${response.error.message}` : fallback;
}
