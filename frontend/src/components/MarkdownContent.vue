<script setup lang="ts">
/* eslint-disable vue/no-v-html -- markdown-it runs with HTML disabled; only strict UUID citations are injected. */
import MarkdownIt from "markdown-it";
import { computed } from "vue";

const props = defineProps<{ content: string; activeEvidenceId: string }>();
const emit = defineEmits<{ select: [evidenceId: string] }>();
const md = new MarkdownIt({ html: false, linkify: false, typographer: false, breaks: false });
const evidencePattern = /\[\[evidence:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]\]/gu;

md.renderer.rules.text = (tokens, index) => md.utils.escapeHtml(tokens[index]?.content ?? "").replace(
  evidencePattern, (_marker, id: string) => {
    const active = id === props.activeEvidenceId ? " is-active" : "";
    const pressed = id === props.activeEvidenceId ? "true" : "false";
    return `<button type="button" class="citation${active}" data-evidence-id="${id}" aria-pressed="${pressed}">证据 ${id.slice(0, 8)}</button>`;
  },
);
const rendered = computed(() => md.render(props.content));

function select(event: MouseEvent): void {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest<HTMLButtonElement>("button[data-evidence-id]");
  const id = button?.dataset.evidenceId;
  if (id) emit("select", id);
}
</script>

<template>
  <div
    class="markdown"
    @click="select"
    v-html="rendered"
  />
</template>
