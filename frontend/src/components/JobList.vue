<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { isActive, jobStatusText, platformText, shortTime, stepText, type Schemas } from "../client";

const props = defineProps<{
  jobs: Schemas["JobSummary"][]; domains: Schemas["Domain"][]; collections: Schemas["Collection"][];
}>();

const status = ref<"all" | "active" | Schemas["JobStatus"]>("all");
const domainId = ref<number | null>(null);
// "none" keeps jobs submitted without a collection.
const collection = ref<"all" | "none" | number>("all");

const domainNames = computed(() => new Map(props.domains.map((item) => [item.id, item.name])));
const collectionNames = computed(() => new Map(props.collections.map((item) => [item.id, item.name])));
const choices = computed(() => props.collections.filter((item) => domainId.value === null || item.domain_id === domainId.value));
const visible = computed(() => props.jobs.filter((job) =>
  (status.value === "all" || (status.value === "active" ? isActive(job.status) : job.status === status.value))
  && (domainId.value === null || job.domain_id === domainId.value)
  && (collection.value === "all" || (collection.value === "none" ? job.collection_id == null : job.collection_id === collection.value))));

watch(domainId, () => { collection.value = "all"; });

function place(job: Schemas["JobSummary"]): string {
  const name = job.collection_id == null ? undefined : collectionNames.value.get(job.collection_id);
  return [domainNames.value.get(job.domain_id), name].filter(Boolean).join(" / ");
}
</script>

<template>
  <section class="card">
    <header class="list-head">
      <h2>任务</h2>
      <div class="row">
        <select v-model="status" aria-label="按状态筛选">
          <option value="all">全部状态</option>
          <option value="active">进行中</option>
          <option v-for="(text, key) in jobStatusText" :key="key" :value="key">{{ text }}</option>
        </select>
        <select v-model="domainId" aria-label="按领域筛选">
          <option :value="null">全部领域</option>
          <option v-for="item in domains" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
        <select v-model="collection" aria-label="按集合筛选">
          <option value="all">全部集合</option>
          <option value="none">未放进集合</option>
          <option v-for="item in choices" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </div>
    </header>
    <ul class="jobs">
      <li v-for="job in visible" :key="job.id">
        <a class="job-row" :href="`/jobs/${job.id}`">
          <span class="title">{{ job.title || job.source_id }}</span>
          <span class="pill" :class="`state-${job.status}`">{{ jobStatusText[job.status] }}</span>
          <small class="meta">
            #{{ job.id }} · {{ platformText[job.platform] }} · {{ place(job) }} · {{ shortTime(job.created) }}
            <template v-if="job.status === 'running' && job.current_step"> · {{ stepText[job.current_step] }}</template>
          </small>
        </a>
      </li>
    </ul>
    <p v-if="!visible.length" class="empty">{{ jobs.length ? "没有符合条件的任务。" : "还没有任务,粘贴一个链接开始。" }}</p>
  </section>
</template>
