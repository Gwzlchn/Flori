<script setup lang="ts">
/* eslint-disable vue/no-v-html -- markdown-it runs with html disabled, so only its own escaped output is injected. */
import MarkdownIt from "markdown-it";
import { computed } from "vue";

import { fileUrl } from "../client";

const props = defineProps<{ source: string; base: string; assets: Record<string, string> }>();

const md = new MarkdownIt({ html: false, linkify: false, typographer: false });
const image = md.renderer.rules.image;
// Notes write images as `assets/<basename>`; in the job they live in the producing step's directory.
// Any other image shows only its alt text. Paper text and AI output are untrusted, and an image
// the browser fetches by itself would reach other hosts, or a side-effect GET on this site.
md.renderer.rules.image = (tokens, index, options, env, self) => {
  const token = tokens[index];
  const name = /^assets\/([^/]+)$/.exec(token?.attrGet("src") ?? "")?.[1];
  const path = name !== undefined && Object.hasOwn(props.assets, name) ? props.assets[name] : undefined;
  if (!token || path === undefined) return md.utils.escapeHtml(self.renderInlineAsText(token?.children ?? [], options, env));
  token.attrSet("src", fileUrl(props.base, path));
  return image(tokens, index, options, env, self);
};

const parts = computed(() => {
  const front = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(props.source);
  return { front: front?.[1] ?? "", html: md.render(front ? props.source.slice(front[0].length) : props.source) };
});
</script>

<template>
  <details v-if="parts.front" class="frontmatter">
    <summary>属性</summary>
    <pre>{{ parts.front }}</pre>
  </details>
  <div class="markdown" v-html="parts.html" />
</template>
