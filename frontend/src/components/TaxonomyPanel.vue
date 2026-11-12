<script setup lang="ts">
import { ref } from "vue";

import { api, failure, feedKindText, type Schemas } from "../client";

const props = defineProps<{ domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();
const emit = defineEmits<{ changed: [] }>();

// Same rule as the server (contract 3): names become vault folders and the domain an Obsidian
// tag, and cannot be renamed.
const forbidden = /[\s\p{Cc}\u00ad\u061c\u180e\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff/\\:*?"<>|#^[\],;]/u;
const domainName = ref("");
const collectionNames = ref<Record<number, string>>({});
const message = ref("");
const busy = ref(false);

function valid(name: string): boolean {
  const length = [...name.normalize("NFC")].length;
  if (length <= 32 && !forbidden.test(name) && !/^[.-]|\.$/.test(name)) return true;
  message.value = `"${name}" 不能用:最长 32 个字符,不能有空格或看不见的字符,不能含 / \\ : * ? " < > | # ^ [ ] , ;,不能以 . 或 - 开头,也不能以 . 结尾。`;
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
      <header class="row">
        <h2 class="grow">领域与集合</h2>
        <a class="btn compact" href="/prompts">提示词</a>
      </header>
      <p class="meta">名字就是 Obsidian 里的文件夹名,领域名也是笔记的标签;可以用中文,建好后不能改名;领域下还有集合或任务、集合下还有任务时不能删除。每个领域可以在"领域要求"里给提示词追加要求。</p>
    </div>
    <form class="row" @submit.prevent="addDomain">
      <input v-model="domainName" class="grow" type="text" autocapitalize="off" autocomplete="off" placeholder="新领域,如 计算机系统结构" aria-label="新领域名字">
      <button class="btn primary" :disabled="busy || !domainName.trim()">新建领域</button>
    </form>
    <p v-if="message" class="error">{{ message }}</p>
    <p v-if="!domains.length" class="empty">还没有领域。</p>
    <article v-for="domain in domains" :key="domain.id" class="domain">
      <header class="row">
        <h3 class="grow">{{ domain.name }}</h3>
        <a class="btn compact" :href="`/domains/${domain.id}/prompts`">领域要求</a>
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
