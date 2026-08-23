<script setup lang="ts">
import { onMounted, ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";

const emit = defineEmits<{ open: [jobId: string, evidenceId?: string] }>();
const query = ref("");
const submittedQuery = ref("");
const results = ref<components["schemas"]["SearchHit"][]>([]);
const status = ref("输入关键词，搜索当前发布成果。");
const searching = ref(false);
let requestSequence = 0;

function normalizedQuery(): string { return query.value.trim().split(/\s+/u).filter(Boolean).join(" "); }
function rememberQuery(value: string): void {
  const url = new URL(window.location.href);
  if (value) url.searchParams.set("q", value);
  else url.searchParams.delete("q");
  history.replaceState(null, "", url);
}
function validate(value: string): string | undefined {
  const length = Array.from(value).length;
  if (length === 0) return "请输入搜索词。";
  if (length > 200) return "搜索词不能超过 200 个字符。";
  return undefined;
}
function excerpt(body: string): string {
  const clean = body
    .replace(/\[\[evidence:[0-9a-f-]{36}\]\]/gu, "")
    .replace(/^#{1,6}\s+/gmu, "")
    .replace(/\s+/gu, " ")
    .trim();
  return clean.length > 220 ? `${clean.slice(0, 220)}…` : clean;
}

async function search(): Promise<void> {
  const value = normalizedQuery();
  query.value = value;
  const error = validate(value);
  if (error) {
    requestSequence += 1;
    results.value = [];
    submittedQuery.value = "";
    rememberQuery("");
    status.value = error;
    return;
  }
  const sequence = ++requestSequence;
  searching.value = true;
  results.value = [];
  submittedQuery.value = value;
  rememberQuery(value);
  status.value = "正在搜索 current 成果…";
  try {
    const response = await apiClient.GET("/api/v1/search", { params: { query: { q: value, limit: 20 } } });
    if (sequence !== requestSequence) return;
    if (!response.data) {
      status.value = apiError(response.error, "搜索失败。");
      return;
    }
    results.value = response.data;
    status.value = response.data.length ? `找到 ${response.data.length} 条 current 成果。` : "没有找到 current 成果。";
  } catch {
    if (sequence === requestSequence) status.value = "无法连接 Flori，请稍后重试。";
  } finally {
    if (sequence === requestSequence) searching.value = false;
  }
}

function openResult(hit: components["schemas"]["SearchHit"], evidenceId?: string): void {
  const url = new URL(window.location.href);
  url.searchParams.set("job_id", hit.job_id);
  if (evidenceId) url.searchParams.set("evidence_id", evidenceId);
  else url.searchParams.delete("evidence_id");
  history.replaceState(null, "", url);
  emit("open", hit.job_id, evidenceId);
}

onMounted(() => {
  const saved = new URL(window.location.href).searchParams.get("q");
  if (saved !== null) { query.value = saved; void search(); }
});
</script>

<template>
  <div class="global-search">
    <form
      class="search-box"
      @submit.prevent="search"
    >
      <label
        class="visually-hidden"
        for="knowledge-query"
      >搜索知识成果</label>
      <span aria-hidden="true">⌕</span>
      <input
        id="knowledge-query"
        v-model="query"
        type="search"
        maxlength="201"
        autocomplete="off"
        placeholder="搜索笔记、术语或内容…"
        :disabled="searching"
      >
      <button
        type="submit"
        class="search-submit"
        :disabled="searching"
      >
        {{ searching ? "搜索中" : "搜索" }}
      </button>
    </form>
    <section
      v-if="submittedQuery"
      class="search-popover"
      aria-label="搜索结果"
    >
      <div class="search-status">
        <span>{{ status }}</span><button
          type="button"
          aria-label="关闭搜索结果"
          @click="submittedQuery = ''"
        >
          ×
        </button>
      </div>
      <ol
        v-if="results.length"
        class="search-results"
      >
        <li
          v-for="hit in results"
          :key="hit.chunk_id"
        >
          <button
            type="button"
            class="result-main"
            @click="openResult(hit)"
          >
            <strong>{{ hit.title.replace(/ \[(summary|smart_note)\]$/u, "") }}</strong>
            <small>{{ hit.title.endsWith("[summary]") ? "摘要" : "智能笔记" }}</small>
            <span>{{ excerpt(hit.body) }}</span>
          </button>
          <div
            v-if="hit.evidence_ids.length"
            class="evidence-chips"
          >
            <button
              v-for="id in hit.evidence_ids.slice(0, 3)"
              :key="id"
              type="button"
              @click="openResult(hit, id)"
            >
              证据 {{ id.slice(0, 8) }}
            </button>
            <span v-if="hit.evidence_ids.length > 3">+{{ hit.evidence_ids.length - 3 }}</span>
          </div>
        </li>
      </ol>
    </section>
  </div>
</template>
