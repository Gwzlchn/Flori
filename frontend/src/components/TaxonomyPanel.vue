<script setup lang="ts">
import { ref } from "vue";

import { api, failure, feedKindText, type Schemas } from "../client";

const props = defineProps<{ domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();
const emit = defineEmits<{ changed: [] }>();

// Same rule as the server; names become vault folders and cannot be renamed.
const slug = /^[a-z0-9][a-z0-9-]{0,47}$/;
const domainName = ref("");
const collectionNames = ref<Record<number, string>>({});
const message = ref("");
const busy = ref(false);

function valid(name: string): boolean {
  if (slug.test(name)) return true;
  message.value = `"${name}" 不能用:只能包含小写字母、数字和连字符,以字母或数字开头,最长 48 个字符。`;
  return false;
}

async function send(call: Promise<{ error?: { message?: string }; response: Response }>): Promise<boolean> {
  busy.value = true;
  message.value = "";
  const { error, response } = await call.finally(() => { busy.value = false; });
  if (!response.ok) {
    message.value = failure(error, response);
    return false;
  }
  emit("changed");
  return true;
}

async function addDomain(): Promise<void> {
  const name = domainName.value.trim();
  if (valid(name) && await send(api.POST("/api/domains", { body: { name } }))) domainName.value = "";
}

async function addCollection(domainId: number): Promise<void> {
  const name = (collectionNames.value[domainId] ?? "").trim();
  if (valid(name) && await send(api.POST("/api/collections", { body: { domain_id: domainId, name } }))) {
    collectionNames.value[domainId] = "";
  }
}

async function remove(kind: "domains" | "collections", id: number, name: string): Promise<void> {
  if (!window.confirm(`删除${kind === "domains" ? "领域" : "集合"} ${name}?`)) return;
  const options = { params: { path: { id } } };
  await send(kind === "domains" ? api.DELETE("/api/domains/{id}", options) : api.DELETE("/api/collections/{id}", options));
}
</script>

<template>
  <section class="card stack">
    <div>
      <h2>领域与集合</h2>
      <p class="meta">名字就是 Obsidian 里的文件夹名,建好后不能改名;领域下还有集合或任务、集合下还有任务时不能删除。</p>
    </div>
    <form class="row" @submit.prevent="addDomain">
      <input v-model="domainName" class="grow" type="text" autocapitalize="off" autocomplete="off" placeholder="新领域,如 ml" aria-label="新领域名字">
      <button class="btn primary" :disabled="busy || !domainName.trim()">新建领域</button>
    </form>
    <p v-if="message" class="error">{{ message }}</p>
    <p v-if="!domains.length" class="empty">还没有领域。</p>
    <article v-for="domain in domains" :key="domain.id" class="domain">
      <header class="row">
        <h3 class="grow">{{ domain.name }}</h3>
        <button type="button" class="btn compact danger-outline" :disabled="busy" @click="remove('domains', domain.id, domain.name)">删除领域</button>
      </header>
      <ul class="collections">
        <li v-for="item in props.collections.filter((c) => c.domain_id === domain.id)" :key="item.id" class="row">
          <span class="grow">{{ item.name }}</span>
          <a class="btn compact" :href="`/collections/${item.id}/feed`">{{ item.feed ? feedKindText[item.feed.kind] : "绑定订阅" }}</a>
          <button type="button" class="btn compact danger-outline" :disabled="busy" @click="remove('collections', item.id, item.name)">删除</button>
        </li>
      </ul>
      <form class="row" @submit.prevent="addCollection(domain.id)">
        <input v-model="collectionNames[domain.id]" class="grow" type="text" autocapitalize="off" autocomplete="off" placeholder="新集合" aria-label="新集合名字">
        <button class="btn" :disabled="busy || !collectionNames[domain.id]?.trim()">新建集合</button>
      </form>
    </article>
  </section>
</template>
