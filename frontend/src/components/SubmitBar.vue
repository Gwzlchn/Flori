<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { api, failure, type Schemas } from "../client";

const props = defineProps<{ domains: Schemas["Domain"][]; collections: Schemas["Collection"][] }>();
const emit = defineEmits<{ submitted: [] }>();

const url = ref("");
const domainId = ref<number>();
const collectionId = ref<number | null>(null);
const translate = ref<Schemas["TranslateMode"]>("auto");
const busy = ref(false);
const message = ref("");
const createdId = ref<number>();
const existingId = ref<number>();

const choices = computed(() => props.collections.filter((item) => item.domain_id === domainId.value));

watch(() => props.domains, (list) => {
  if (!list.some((item) => item.id === domainId.value)) domainId.value = list[0]?.id;
}, { immediate: true });
watch(domainId, () => { collectionId.value = null; });

async function submit(): Promise<void> {
  const link = url.value.trim();
  if (!link || domainId.value === undefined) return;
  busy.value = true;
  message.value = "";
  createdId.value = undefined;
  existingId.value = undefined;
  const { data, error, response } = await api.POST("/api/jobs", {
    body: { url: link, domain_id: domainId.value, collection_id: collectionId.value, translate: translate.value },
  }).finally(() => { busy.value = false; });
  if (data) {
    url.value = "";
    createdId.value = data.id;
    emit("submitted");
    return;
  }
  message.value = failure(error, response);
  existingId.value = error?.job_id ?? undefined;
}
</script>

<template>
  <form class="card submit" @submit.prevent="submit">
    <input v-model="url" class="grow" type="text" inputmode="url" autocomplete="off" placeholder="粘贴 B站、YouTube 或 arXiv 链接,也可以填 arXiv 编号" aria-label="链接">
    <div class="row">
      <select v-model="domainId" aria-label="领域">
        <option v-for="item in domains" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
      <select v-model="collectionId" aria-label="集合">
        <option :value="null">不放进集合</option>
        <option v-for="item in choices" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
      <select v-model="translate" aria-label="翻译">
        <option value="auto">自动翻译</option>
        <option value="off">不翻译</option>
      </select>
      <button class="btn primary" :disabled="busy || !url.trim() || domainId === undefined">{{ busy ? "投递中" : "投递" }}</button>
    </div>
    <p v-if="!domains.length" class="meta">还没有领域,先去<a href="/taxonomy">领域与订阅</a>建一个。</p>
    <p v-if="createdId" class="ok">已投递,<a :href="`/jobs/${createdId}`">查看任务 #{{ createdId }}</a></p>
    <p v-if="message" class="error">
      {{ message }}<a v-if="existingId" :href="`/jobs/${existingId}`">,打开已有任务 #{{ existingId }}</a>
    </p>
  </form>
</template>
