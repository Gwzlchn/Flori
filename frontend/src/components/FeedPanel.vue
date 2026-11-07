<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import { api, failure, feedKindText, jobStatusText, type Schemas } from "../client";

const props = defineProps<{ id: number; domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();
const emit = defineEmits<{ changed: [] }>();

const batch = 20;
const collection = computed(() => props.collections.find((item) => item.id === props.id));
const domain = computed(() => props.domains.find((item) => item.id === collection.value?.domain_id));
const feedUrl = ref(collection.value?.feed?.url ?? "");
const list = ref<Schemas["FeedList"]>();
const picked = ref(new Set<string>());
const translate = ref<Schemas["TranslateMode"]>("auto");
const busy = ref(false);
const cooling = ref(false);
// Shown next to the control that caused it; the item list is long on phones.
const status = ref<{ at: "bind" | "refresh" | "submit"; text: string; ok: boolean }>();
let cooldown: number | undefined;

const open = computed(() => list.value?.items.filter((item) => !item.job) ?? []);

watch(() => collection.value?.feed?.url, (url) => { feedUrl.value = url ?? ""; });
onBeforeUnmount(() => window.clearTimeout(cooldown));

function key(item: Schemas["FeedPick"]): string {
  return `${item.platform}:${item.source_id}`;
}

function duration(seconds: number): string {
  const time = [Math.floor(seconds / 60) % 60, seconds % 60].map((part) => String(part).padStart(2, "0")).join(":");
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)}:${time}` : time;
}

function start(): void {
  busy.value = true;
  status.value = undefined;
}

async function bind(url: string | null): Promise<void> {
  start();
  const { data, error, response } = await api.PUT("/api/collections/{id}/feed", {
    params: { path: { id: props.id } }, body: { url },
  }).finally(() => { busy.value = false; });
  if (!data) {
    status.value = { at: "bind", text: failure(error, response), ok: false };
    return;
  }
  list.value = undefined;
  picked.value = new Set();
  status.value = { at: "bind", text: url ? "订阅已保存。" : "已解绑订阅。", ok: true };
  emit("changed");
}

async function refresh(): Promise<void> {
  // The upstream sites rate-limit; one refresh per 10s is plenty.
  cooling.value = true;
  cooldown = window.setTimeout(() => { cooling.value = false; }, 10_000);
  start();
  const { data, error, response } = await api.GET("/api/collections/{id}/feed", {
    params: { path: { id: props.id }, query: { limit: 30 } },
  }).finally(() => { busy.value = false; });
  if (!data) {
    status.value = { at: "refresh", text: failure(error, response), ok: false };
    return;
  }
  list.value = data;
  picked.value = new Set();
}

function toggle(item: Schemas["FeedItem"]): void {
  const next = new Set(picked.value);
  if (!next.delete(key(item)) && next.size < batch) next.add(key(item));
  picked.value = next;
}

async function submit(): Promise<void> {
  const items = open.value.filter((item) => picked.value.has(key(item)))
    .map((item) => ({ platform: item.platform, source_id: item.source_id }));
  start();
  const { data, error, response } = await api.POST("/api/collections/{id}/feed/jobs", {
    params: { path: { id: props.id } }, body: { items, translate: translate.value },
  }).finally(() => { busy.value = false; });
  if (!data) {
    status.value = { at: "submit", text: failure(error, response), ok: false };
    return;
  }
  const jobs = new Map([...data.created, ...data.skipped].map((item) => [key(item), item.job]));
  for (const item of list.value?.items ?? []) {
    const job = jobs.get(key(item));
    if (job) item.job = job;
  }
  picked.value = new Set();
  const skipped = data.skipped.length ? `,${data.skipped.length} 条之前已投递` : "";
  status.value = { at: "submit", text: `已投递 ${data.created.length} 条${skipped}。`, ok: true };
}
</script>

<template>
  <p v-if="!collection" class="empty">集合不存在。<a href="/taxonomy">回到领域与订阅</a></p>
  <div v-else class="stack">
    <section class="card stack">
      <div>
        <p class="meta"><a href="/taxonomy">领域与订阅</a> / {{ domain?.name }}</p>
        <h2>{{ collection.name }} 的订阅</h2>
        <p class="meta">
          {{ collection.feed ? `已绑定 ${feedKindText[collection.feed.kind]}` : "还没有绑定订阅" }}。
          支持 B站 UP 主投稿、合集、系列、公开收藏夹,YouTube 频道和播放列表。
        </p>
      </div>
      <form class="row" @submit.prevent="bind(feedUrl.trim())">
        <input v-model="feedUrl" class="grow" type="text" inputmode="url" autocomplete="off" placeholder="订阅来源链接" aria-label="订阅来源链接">
        <button class="btn primary" :disabled="busy || !feedUrl.trim()">保存</button>
        <button v-if="collection.feed" type="button" class="btn danger-outline" :disabled="busy" @click="bind(null)">解绑</button>
      </form>
      <p v-if="status?.at === 'bind'" :class="status.ok ? 'ok' : 'error'">{{ status.text }}</p>
    </section>

    <section v-if="collection.feed" class="card stack">
      <header class="row">
        <h2 class="grow">{{ list?.title || "最近更新" }}</h2>
        <button type="button" class="btn" :disabled="busy || cooling" @click="refresh">{{ busy && !list ? "获取中…" : "更新" }}</button>
      </header>
      <p v-if="status?.at === 'refresh'" class="error">{{ status.text }}</p>
      <p v-if="!list" class="empty">点"更新"列出最近 30 条。</p>
      <template v-else>
        <div class="row">
          <button type="button" class="btn compact" :disabled="!open.length" @click="picked = new Set(open.slice(0, batch).map(key))">全选未投递(最多 {{ batch }})</button>
          <button type="button" class="btn compact" :disabled="!picked.size" @click="picked = new Set()">清空</button>
          <span class="grow meta">已选 {{ picked.size }} / {{ batch }}</span>
        </div>
        <ul class="feed-items">
          <li v-for="item in list.items" :key="key(item)">
            <label class="row">
              <input type="checkbox" :checked="picked.has(key(item))" :disabled="!!item.job || (!picked.has(key(item)) && picked.size >= batch)" @change="toggle(item)">
              <span class="grow">
                {{ item.title }}
                <small class="meta">{{ [item.published, item.duration_s == null ? null : duration(item.duration_s)].filter(Boolean).join(" · ") }}</small>
              </span>
              <a v-if="item.job" class="pill" :class="`state-${item.job.status}`" :href="`/jobs/${item.job.id}`">{{ jobStatusText[item.job.status] }}</a>
            </label>
          </li>
        </ul>
        <p v-if="!list.items.length" class="empty">来源里没有可投递的条目。</p>
        <div class="row">
          <select v-model="translate" aria-label="翻译">
            <option value="auto">自动翻译</option>
            <option value="off">不翻译</option>
          </select>
          <button type="button" class="btn primary" :disabled="busy || !picked.size" @click="submit">投递所选 {{ picked.size }} 条</button>
        </div>
        <p v-if="status?.at === 'submit'" :class="status.ok ? 'ok' : 'error'">{{ status.text }}</p>
      </template>
    </section>
  </div>
</template>
