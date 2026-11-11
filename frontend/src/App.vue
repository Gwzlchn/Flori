<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { api, isActive, type Schemas } from "./client";
import FeedPanel from "./components/FeedPanel.vue";
import JobDetail from "./components/JobDetail.vue";
import JobList from "./components/JobList.vue";
import SubmitBar from "./components/SubmitBar.vue";
import TaxonomyPanel from "./components/TaxonomyPanel.vue";

type Route = { view: "home" | "taxonomy" | "missing" } | { view: "job"; id: number; tab: string } | { view: "feed"; id: number };

const path = ref(location.pathname);
const ready = ref(false);
const version = ref("");
const domains = ref<Schemas["Domain"][]>([]);
const collections = ref<Schemas["Collection"][]>([]);
const jobs = ref<Schemas["JobSummary"][]>([]);
let timer: number | undefined;

const route = computed((): Route => {
  // /jobs/<id> is the note, /jobs/<id>/<tab> another document or the 元信息 tab.
  const job = /^\/jobs\/(\d+)(?:\/([^/]+))?\/?$/.exec(path.value);
  if (job) return { view: "job", id: Number(job[1]), tab: job[2] ?? "" };
  const feed = /^\/collections\/(\d+)\/feed\/?$/.exec(path.value);
  if (feed) return { view: "feed", id: Number(feed[1]) };
  if (path.value === "/") return { view: "home" };
  return { view: path.value === "/taxonomy" ? "taxonomy" : "missing" };
});

// Same-origin links navigate in place. /api links (files, video) and obsidian:// stay with the browser.
function follow(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target instanceof Element ? event.target.closest("a") : null;
  if (!link || link.target || link.origin !== location.origin || link.pathname.startsWith("/api/")) return;
  event.preventDefault();
  if (link.pathname !== location.pathname) history.pushState(null, "", link.pathname);
  const before = route.value;
  path.value = link.pathname;
  const after = route.value;
  // Switching tabs of one job keeps the reader where they are.
  if (!(before.view === "job" && after.view === "job" && before.id === after.id)) window.scrollTo(0, 0);
}

function popped(): void {
  path.value = location.pathname;
}

async function loadTaxonomy(): Promise<void> {
  const [domainList, collectionList] = await Promise.all([api.GET("/api/domains"), api.GET("/api/collections")]);
  domains.value = domainList.data ?? [];
  collections.value = collectionList.data ?? [];
}

async function loadJobs(): Promise<void> {
  const { data } = await api.GET("/api/jobs");
  if (data) jobs.value = data;
}

watch(() => route.value.view, (view) => {
  if (view === "home") void loadJobs();
});

onMounted(async () => {
  window.addEventListener("popstate", popped);
  // The list only needs refreshing while something is still moving.
  timer = window.setInterval(() => {
    if (route.value.view === "home" && jobs.value.some((job) => isActive(job.status))) void loadJobs();
  }, 5000);
  void api.GET("/api/health").then(({ data }) => { version.value = data?.version ?? ""; });
  await Promise.all([loadTaxonomy(), loadJobs()]).finally(() => { ready.value = true; });
});

onBeforeUnmount(() => {
  window.removeEventListener("popstate", popped);
  window.clearInterval(timer);
});
</script>

<template>
  <div class="shell" @click="follow">
    <header class="topbar">
      <a class="brand" href="/">Flori</a>
      <nav class="nav">
        <a href="/" :class="{ on: route.view === 'home' || route.view === 'job' }">任务</a>
        <a href="/taxonomy" :class="{ on: route.view === 'taxonomy' || route.view === 'feed' }">领域与订阅</a>
      </nav>
      <small v-if="version" class="meta">v{{ version }}</small>
    </header>
    <main v-if="ready" class="page">
      <template v-if="route.view === 'home'">
        <SubmitBar :domains :collections @submitted="loadJobs" />
        <JobList :jobs :domains :collections />
      </template>
      <JobDetail v-else-if="route.view === 'job'" :id="route.id" :key="route.id" :tab="route.tab" :domains :collections />
      <TaxonomyPanel v-else-if="route.view === 'taxonomy'" :domains :collections @changed="loadTaxonomy" />
      <FeedPanel v-else-if="route.view === 'feed'" :id="route.id" :key="route.id" :domains :collections @changed="loadTaxonomy" />
      <p v-else class="empty">页面不存在。<a href="/">回到任务列表</a></p>
    </main>
  </div>
</template>
