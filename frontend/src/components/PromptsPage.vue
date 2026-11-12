<script setup lang="ts">
import { onMounted, ref } from "vue";

import { api, failure, textRows, type Schemas } from "../client";

type Prompt = Schemas["PromptInfo"];
type Note = { text: string; ok: boolean };

const prompts = ref<Prompt[]>();
const drafts = ref<Partial<Record<Schemas["PromptName"], string>>>({});
// Shown under the prompt that caused it; the page is long on phones.
const notes = ref<Partial<Record<Schemas["PromptName"], Note | undefined>>>({});
const message = ref("");
const busy = ref(false);

onMounted(async () => {
  const { data, error, response } = await api.GET("/api/prompts");
  if (!data) {
    message.value = failure(error, response);
    return;
  }
  prompts.value = data;
  drafts.value = Object.fromEntries(data.map((prompt) => [prompt.name, prompt.text]));
});

function show(next: Prompt, note: Note): void {
  prompts.value = prompts.value?.map((prompt) => (prompt.name === next.name ? next : prompt));
  drafts.value[next.name] = next.text;
  notes.value[next.name] = note;
}

async function save(prompt: Prompt): Promise<void> {
  busy.value = true;
  notes.value[prompt.name] = undefined;
  const { data, error, response } = await api.PUT("/api/prompts/{name}", {
    params: { path: { name: prompt.name } }, body: { text: drafts.value[prompt.name] ?? "" },
  }).finally(() => { busy.value = false; });
  if (!data) {
    notes.value[prompt.name] = { text: failure(error, response), ok: false };
    return;
  }
  // The server drops an override equal to the built-in text.
  show(data, { text: data.customized ? "已保存。" : "与默认相同,用的仍是默认提示词。", ok: true });
}

async function reset(prompt: Prompt): Promise<void> {
  if (!window.confirm(`把"${prompt.label}"恢复成默认提示词?改写的内容会丢掉。`)) return;
  busy.value = true;
  notes.value[prompt.name] = undefined;
  const { error, response } = await api.DELETE("/api/prompts/{name}", {
    params: { path: { name: prompt.name } },
  }).finally(() => { busy.value = false; });
  if (!response.ok) {
    notes.value[prompt.name] = { text: failure(error, response), ok: false };
    return;
  }
  show({ ...prompt, text: prompt.default_text, customized: false }, { text: "已恢复默认。", ok: true });
}
</script>

<template>
  <div class="stack">
    <section class="card">
      <p class="meta"><a href="/taxonomy">领域与订阅</a> / 提示词</p>
      <h2>提示词</h2>
      <p class="meta">AI 笔记、翻译与加标点发给模型的指令。保存后开始的步骤用新文本,重试也算;单个领域要多写的要求在领域与订阅页各领域的"领域要求"里。</p>
      <p class="warn">别改输出格式:翻译与加标点必须回 id 不变的 JSON,笔记配图必须写 img:N,改了步骤会通不过校验。</p>
      <p v-if="message" class="error">{{ message }}</p>
      <p v-else-if="!prompts" class="empty">加载中…</p>
    </section>
    <section v-for="prompt in prompts" :key="prompt.name" class="card stack">
      <header class="row">
        <h3 class="grow">{{ prompt.label }} <small class="meta">{{ prompt.name }}</small></h3>
        <span v-if="prompt.customized" class="pill state-running">已改写</span>
      </header>
      <textarea
        v-model="drafts[prompt.name]" :rows="textRows(drafts[prompt.name] ?? '')" spellcheck="false"
        :aria-label="`${prompt.label}的提示词`"
      />
      <div class="row">
        <button type="button" class="btn primary" :disabled="busy || drafts[prompt.name] === prompt.text" @click="save(prompt)">保存</button>
        <button v-if="prompt.customized" type="button" class="btn danger-outline" :disabled="busy" @click="reset(prompt)">恢复默认</button>
      </div>
      <p v-if="notes[prompt.name]" :class="notes[prompt.name]?.ok ? 'ok' : 'error'">{{ notes[prompt.name]?.text }}</p>
    </section>
  </div>
</template>
