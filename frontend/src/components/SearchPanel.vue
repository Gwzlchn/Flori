<script setup lang="ts">
import { onMounted, ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";

const query = ref("");
const submittedQuery = ref("");
const results = ref<components["schemas"]["SearchHit"][]>([]);
const status = ref("输入关键词，搜索当前发布成果。");
const searching = ref(false);
let requestSequence = 0;

function normalizedQuery(): string {
  return query.value.trim().split(/\s+/u).filter(Boolean).join(" ");
}

function rememberQuery(value: string): void {
  const url = new URL(window.location.href);
  if (value) url.searchParams.set("q", value);
  else url.searchParams.delete("q");
  history.replaceState(null, "", url);
}

function validate(value: string): string | undefined {
  const length = Array.from(value).length;
  if (length === 0) return "invalid_request: 请输入搜索词。";
  if (length > 200) return "invalid_request: 搜索词不能超过 200 个字符。";
  return undefined;
}

async function search(): Promise<void> {
  const value = normalizedQuery();
  query.value = value;
  const validation = validate(value);
  if (validation) {
    requestSequence += 1;
    searching.value = false;
    results.value = [];
    submittedQuery.value = "";
    rememberQuery("");
    status.value = validation;
    return;
  }

  const sequence = ++requestSequence;
  searching.value = true;
  results.value = [];
  submittedQuery.value = value;
  rememberQuery(value);
  status.value = "正在搜索 current 成果…";
  try {
    const response = await apiClient.GET("/api/v1/search", {
      params: { query: { q: value, limit: 20 } },
    });
    if (sequence !== requestSequence) return;
    if (!response.data) {
      status.value = apiError(response.error, "search_failed: 搜索失败。");
      return;
    }
    results.value = response.data;
    status.value = response.data.length
      ? `找到 ${response.data.length} 条 current 成果。`
      : "没有找到 current 成果。";
  } catch {
    if (sequence === requestSequence) status.value = "network_temporary: 无法连接 Flori，请稍后重试。";
  } finally {
    if (sequence === requestSequence) searching.value = false;
  }
}

function openResult(hit: components["schemas"]["SearchHit"], evidenceId?: string): void {
  const url = new URL(window.location.href);
  url.searchParams.set("job_id", hit.job_id);
  if (evidenceId) url.searchParams.set("evidence_id", evidenceId);
  else url.searchParams.delete("evidence_id");
  if (submittedQuery.value) url.searchParams.set("q", submittedQuery.value);
  window.location.assign(url.toString());
}

onMounted(() => {
  const saved = new URL(window.location.href).searchParams.get("q");
  if (saved !== null) {
    query.value = saved;
    void search();
  }
});
</script>

<template>
  <section
    class="card"
    aria-labelledby="search-title"
  >
    <h2 id="search-title">
      搜索知识成果
    </h2>
    <form
      class="search-form"
      @submit.prevent="search"
    >
      <label for="knowledge-query">关键词</label>
      <div class="search-row">
        <input
          id="knowledge-query"
          v-model="query"
          type="search"
          maxlength="201"
          autocomplete="off"
          :disabled="searching"
        >
        <button
          type="submit"
          :disabled="searching"
        >
          {{ searching ? "搜索中…" : "搜索" }}
        </button>
      </div>
    </form>
    <p
      class="notice"
      aria-live="polite"
    >
      {{ status }}
    </p>
    <ol
      v-if="results.length"
      class="results"
    >
      <li
        v-for="hit in results"
        :key="hit.chunk_id"
      >
        <article>
          <h3>{{ hit.title }}</h3>
          <p class="body">
            {{ hit.body }}
          </p>
          <div class="actions">
            <button
              type="button"
              class="secondary"
              @click="openResult(hit)"
            >
              打开阅读
            </button>
            <button
              v-for="evidenceId in hit.evidence_ids"
              :key="evidenceId"
              type="button"
              class="secondary"
              @click="openResult(hit, evidenceId)"
            >
              查看证据 {{ evidenceId.slice(0, 8) }}
            </button>
          </div>
        </article>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.search-form, .results, .actions { display: grid; gap: 0.75rem; }
.search-row { display: flex; gap: 0.5rem; }
.search-row input { flex: 1; min-width: 0; }
.results { padding-left: 1.25rem; }
.results li + li { margin-top: 1rem; }
.body { line-height: 1.6; white-space: pre-wrap; }
.actions { grid-template-columns: repeat(auto-fit, minmax(10rem, max-content)); }
</style>
