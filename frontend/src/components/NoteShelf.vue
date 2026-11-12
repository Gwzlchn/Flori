<script lang="ts">
import { computed, ref } from "vue";

import { duration, failedAt, jobStatusText, platformText, shortTime, stepText, type Schemas } from "../client";

type Job = Schemas["JobSummary"];

// Module state, so the chosen filters survive opening a note and coming back.
const domainId = ref<number | null>(null);
const platform = ref<Schemas["Platform"] | null>(null);
</script>

<script setup lang="ts">
const props = defineProps<{ jobs: Job[]; domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();

const domainNames = computed(() => new Map(props.domains.map((item) => [item.id, item.name])));
const collectionNames = computed(() => new Map(props.collections.map((item) => [item.id, item.name])));
// Everything that is not a note yet, newest first as the server lists them. The filters leave
// this block alone: a failure has to stay visible whatever shelf is being browsed.
const unfinished = computed(() => props.jobs.filter((job) => job.status !== "succeeded"));
const notes = computed(() => props.jobs.filter((job) => job.status === "succeeded"));
// A filter left pointing at a deleted domain would hide every note with no tag to undo it.
const domain = computed(() => (domainId.value !== null && domainNames.value.has(domainId.value) ? domainId.value : null));

// Each group counts within the other group's choice, so a tag's number is what clicking it shows.
const inPlatform = computed(() => notes.value.filter((job) => platform.value === null || job.platform === platform.value));
const inDomain = computed(() => notes.value.filter((job) => domain.value === null || job.domain_id === domain.value));
const domainTags = computed(() => [
  { key: null, label: "全部", count: inPlatform.value.length },
  ...props.domains.map((item) => ({
    key: item.id, label: item.name, count: inPlatform.value.filter((job) => job.domain_id === item.id).length,
  })),
]);
const platformTags = computed(() => [
  { key: null, label: "全部", count: inDomain.value.length },
  ...(Object.keys(platformText) as Schemas["Platform"][]).map((key) => ({
    key, label: platformText[key], count: inDomain.value.filter((job) => job.platform === key).length,
  })),
]);

// By source date, newest first. Notes without one come after all dated ones, by submission:
// a submission day is always later than a source date, so mixing the two would float them up.
function newestFirst(a: Job, b: Job): number {
  return (b.published ?? "").localeCompare(a.published ?? "") || b.created.localeCompare(a.created) || b.id - a.id;
}

// Domains and collections in the order of their lists, 未归集合 last; empty ones are left out.
const shelves = computed(() => {
  const shown = inPlatform.value.filter((job) => domain.value === null || job.domain_id === domain.value);
  return props.domains.flatMap((item) => {
    const mine = shown.filter((job) => job.domain_id === item.id);
    if (!mine.length) return [];
    const groups = [
      ...props.collections.filter((c) => c.domain_id === item.id).map((c) => ({ key: c.id, name: c.name, loose: false })),
      { key: 0, name: "未归集合", loose: true },
    ].flatMap((group) => {
      const rows = mine.filter((job) => (group.loose ? job.collection_id == null : job.collection_id === group.key)).sort(newestFirst);
      return rows.length ? [{ ...group, rows }] : [];
    });
    return [{ id: item.id, name: item.name, count: mine.length, groups }];
  });
});
const shownCount = computed(() => shelves.value.reduce((sum, shelf) => sum + shelf.count, 0));

function place(job: Job): string {
  const name = job.collection_id == null ? undefined : collectionNames.value.get(job.collection_id);
  return [domainNames.value.get(job.domain_id), name].filter(Boolean).join(" / ");
}

// A paper's authors arrive joined with ", "; a long list would push the date off the line.
function author(job: Job): string | undefined {
  const names = job.platform === "arxiv" ? job.author?.split(", ") ?? [] : [];
  return names.length > 3 ? `${names.slice(0, 3).join(", ")} 等 ${names.length} 人` : job.author ?? undefined;
}

function facts(job: Job): string {
  return [
    platformText[job.platform], author(job), job.published,
    job.duration_ms == null ? undefined : duration(Math.round(job.duration_ms / 1000)),
  ].filter(Boolean).join(" · ");
}
</script>

<template>
  <section v-if="unfinished.length" class="card">
    <header class="list-head">
      <h2>处理中与未完成</h2>
      <small class="meta">{{ unfinished.length }} 条</small>
    </header>
    <ul class="jobs">
      <li v-for="job in unfinished" :key="job.id">
        <a class="job-row" :href="`/jobs/${job.id}`">
          <span class="title">{{ job.title || job.source_id }}</span>
          <span class="row">
            <span v-if="job.status === 'failed' && job.current_exit_code === 30" class="pill state-failed">凭据失效</span>
            <span class="pill" :class="`state-${job.status}`">{{ jobStatusText[job.status] }}</span>
          </span>
          <small class="meta">
            #{{ job.id }} · {{ platformText[job.platform] }} · {{ place(job) }} · {{ shortTime(job.created) }}
            <template v-if="job.status === 'running' && job.current_step"> · {{ stepText[job.current_step] }}</template>
            <template v-if="job.status === 'failed' && job.current_step"> · {{ failedAt(job.current_step) }}</template>
          </small>
        </a>
      </li>
    </ul>
  </section>

  <section class="card">
    <header class="list-head">
      <h2>笔记</h2>
      <small class="meta">{{ shownCount === notes.length ? `${notes.length} 篇` : `${shownCount} / ${notes.length} 篇` }}</small>
    </header>
    <div v-if="notes.length" class="facets">
      <div class="facet" role="group" aria-label="按领域筛选">
        <span class="meta">领域</span>
        <button
          v-for="tag in domainTags" :key="tag.key ?? 'all'" type="button" class="tag" :class="{ none: !tag.count }"
          :aria-pressed="domain === tag.key" @click="domainId = tag.key"
        >
          {{ tag.label }}<b>{{ tag.count }}</b>
        </button>
      </div>
      <div class="facet" role="group" aria-label="按平台筛选">
        <span class="meta">平台</span>
        <button
          v-for="tag in platformTags" :key="tag.key ?? 'all'" type="button" class="tag" :class="{ none: !tag.count }"
          :aria-pressed="platform === tag.key" @click="platform = tag.key"
        >
          {{ tag.label }}<b>{{ tag.count }}</b>
        </button>
      </div>
    </div>
    <section v-for="shelf in shelves" :key="shelf.id" class="shelf">
      <h3>{{ shelf.name }}<small class="meta">{{ shelf.count }} 篇</small></h3>
      <div v-for="group in shelf.groups" :key="group.key" class="shelf-group">
        <h4 :class="{ loose: group.loose }">{{ group.name }}<small>{{ group.rows.length }}</small></h4>
        <ul class="jobs">
          <li v-for="job in group.rows" :key="job.id">
            <a class="note-row" :href="`/jobs/${job.id}`">
              <span class="title">{{ job.title || job.source_id }}</span>
              <small class="meta">{{ facts(job) }}</small>
              <small v-if="job.description" class="about">{{ job.description }}</small>
            </a>
          </li>
        </ul>
      </div>
    </section>
    <p v-if="!shownCount" class="empty">{{ notes.length ? "没有符合条件的笔记。" : "还没有笔记,粘贴一个链接开始。" }}</p>
  </section>
</template>
