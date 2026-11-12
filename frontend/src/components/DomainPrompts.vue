<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { api, failure, guardUnsaved, textRows, type Schemas } from "../client";

type Prompt = Schemas["DomainPrompt"];
type Note = { text: string; ok: boolean };

const props = defineProps<{ id: number; domains: Schemas["Domain"][] }>();

const domain = computed(() => props.domains.find((item) => item.id === props.id));
const prompts = ref<Prompt[]>();
const drafts = ref<Partial<Record<Schemas["PromptName"], string>>>({});
const notes = ref<Partial<Record<Schemas["PromptName"], Note | undefined>>>({});
const message = ref("");
const busy = ref(false);

guardUnsaved(() => prompts.value?.some((prompt) => drafts.value[prompt.name] !== prompt.text) ?? false);

onMounted(async () => {
  if (!domain.value) return;
  const { data, error, response } = await api.GET("/api/domains/{id}/prompts", {
    params: { path: { id: props.id } },
  });
  if (!data) {
    message.value = failure(error, response);
    return;
  }
  prompts.value = data;
  drafts.value = Object.fromEntries(data.map((prompt) => [prompt.name, prompt.text]));
});

// Empty text removes the requirement.
async function save(prompt: Prompt, text: string): Promise<void> {
  busy.value = true;
  notes.value[prompt.name] = undefined;
  const { data, error, response } = await api.PUT("/api/domains/{id}/prompts/{name}", {
    params: { path: { id: props.id, name: prompt.name } }, body: { text },
  }).finally(() => { busy.value = false; });
  if (!data) {
    notes.value[prompt.name] = { text: failure(error, response), ok: false };
    return;
  }
  prompts.value = prompts.value?.map((item) => (item.name === data.name ? data : item));
  drafts.value[data.name] = data.text;
  notes.value[data.name] = { text: data.text ? "已保存。" : "已清空,这个领域不再追加要求。", ok: true };
}

// One tap would drop the saved text for good, so it asks like 恢复默认 does.
async function clear(prompt: Prompt): Promise<void> {
  if (!window.confirm(`清空"${prompt.label}"的领域要求?写好的内容会丢掉。`)) return;
  await save(prompt, "");
}
</script>

<template>
  <p v-if="!domain" class="empty">领域不存在。<a href="/taxonomy">回到领域与订阅</a></p>
  <div v-else class="stack">
    <section class="card">
      <p class="meta"><a href="/taxonomy">领域与订阅</a> / {{ domain.name }}</p>
      <h2>{{ domain.name }} 的领域要求</h2>
      <p class="meta">
        领域要求接在提示词末尾,标题是"## 领域要求({{ domain.name }})",只发给这个领域的任务;
        保存后开始的步骤生效,重试也算。留空就是不追加。提示词本身在<a href="/prompts">提示词</a>页改。
      </p>
      <p v-if="message" class="error">{{ message }}</p>
      <p v-else-if="!prompts" class="empty">加载中…</p>
    </section>
    <section v-for="prompt in prompts" :key="prompt.name" class="card stack">
      <h3>{{ prompt.label }} <small class="meta">{{ prompt.name }}</small></h3>
      <details class="prompt-base">
        <summary>提示词(只读)</summary>
        <pre>{{ prompt.base }}</pre>
      </details>
      <textarea
        v-model="drafts[prompt.name]" :rows="textRows(drafts[prompt.name] ?? '', 3)" spellcheck="false"
        :aria-label="`${prompt.label}的领域要求`" :placeholder="`${domain.name} 领域额外的要求,接在上面的提示词之后`"
      />
      <div class="row">
        <button type="button" class="btn primary" :disabled="busy || drafts[prompt.name] === prompt.text" @click="save(prompt, drafts[prompt.name] ?? '')">保存</button>
        <button v-if="prompt.text" type="button" class="btn danger-outline" :disabled="busy" @click="clear(prompt)">清空</button>
      </div>
      <p v-if="notes[prompt.name]" :class="notes[prompt.name]?.ok ? 'ok' : 'error'">{{ notes[prompt.name]?.text }}</p>
    </section>
  </div>
</template>
