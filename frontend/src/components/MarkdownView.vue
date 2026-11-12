<script setup lang="ts">
/* eslint-disable vue/no-v-html -- markdown-it runs with html disabled, so only its own escaped output is injected. */
import "katex/dist/katex.min.css";
import MarkdownIt from "markdown-it";
import { computed, ref, watchEffect } from "vue";

import { fileUrl } from "../client";

// `seekable` is set while a player is on screen; only then are timestamps made clickable.
const props = defineProps<{ source: string; base: string; assets: Record<string, string>; seekable?: boolean }>();
const emit = defineEmits<{ seek: [seconds: number] }>();

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

// Notes cite video time as `[MM:SS]` or `[H:MM:SS]`; the transcript heads each section with the
// bare time. Both become buttons. Only text tokens are rewritten, so code spans, code blocks and
// link text keep theirs as written. The rule runs after text_join, which is what turns the
// transcript's escaped `\[` into plain text.
const STAMP = /\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]/g;
const BARE = /^(?:\d{1,2}:)?\d{1,2}:\d{2}$/;

function stampSeconds(clock: string): number {
  return clock.split(":").reduce((total, part) => total * 60 + Number(part), 0);
}

md.core.ruler.push("timestamps", (state) => {
  if (!props.seekable) return;
  const stamp = (label: string, clock: string) => {
    const token = new state.Token("timestamp", "", 0);
    token.content = label;
    token.attrSet("data-seconds", String(stampSeconds(clock)));
    return token;
  };
  const text = (content: string) => {
    const token = new state.Token("text", "", 0);
    token.content = content;
    return token;
  };
  state.tokens.forEach((block, index) => {
    if (block.type !== "inline" || !block.children) return;
    const only = block.children.length === 1 ? block.children[0] : undefined;
    if (only?.type === "text" && state.tokens[index - 1]?.type === "heading_open" && BARE.test(only.content.trim())) {
      block.children = [stamp(only.content.trim(), only.content.trim())];
      return;
    }
    let links = 0;
    block.children = block.children.flatMap((child) => {
      if (child.type === "link_open") links += 1;
      if (child.type === "link_close") links -= 1;
      if (child.type !== "text" || links > 0) return [child];
      const parts = [];
      let end = 0;
      for (const match of child.content.matchAll(STAMP)) {
        if (match.index > end) parts.push(text(child.content.slice(end, match.index)));
        parts.push(stamp(match[0], match[1] ?? ""));
        end = match.index + match[0].length;
      }
      if (end === 0) return [child];
      if (end < child.content.length) parts.push(text(child.content.slice(end)));
      return parts;
    });
  });
});
md.renderer.rules.timestamp = (tokens, index) => {
  const token = tokens[index];
  const seconds = token?.attrGet("data-seconds") ?? "";
  // Both values are digits, colons and brackets by construction; escaped all the same.
  return `<button type="button" class="stamp" data-seconds="${md.utils.escapeHtml(seconds)}" title="跳到视频的这一刻">${md.utils.escapeHtml(token?.content ?? "")}</button>`;
};

function pick(event: MouseEvent): void {
  const stamp = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-seconds]") : null;
  if (stamp) emit("seek", Number(stamp.dataset.seconds));
}

// Papers and notes write formulas as $…$ and $$…$$, which Obsidian renders. KaTeX is large and only
// papers need it, so it loads the first time a source has a dollar sign; until then formulas show
// as text. KaTeX's default trust is off, so \href, \url and similar in untrusted TeX stay inert,
// and a formula it cannot parse is shown as its source.
const math = ref(false);
let loading: Promise<void> | undefined;
watchEffect(() => {
  if (math.value || loading || !props.source.includes("$")) return;
  loading = import("@vscode/markdown-it-katex")
    .then(({ default: katex }) => {
      md.use(katex, { throwOnError: false });
      math.value = true;
    })
    .catch((error: unknown) => {
      loading = undefined;
      console.error("KaTeX failed to load", error);
    });
});

const parts = computed(() => {
  const front = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(props.source);
  const body = front ? props.source.slice(front[0].length) : props.source;
  return { front: front?.[1] ?? "", html: md.render(body), math: math.value, seekable: props.seekable };
});
</script>

<template>
  <details v-if="parts.front" class="frontmatter">
    <summary>属性</summary>
    <pre>{{ parts.front }}</pre>
  </details>
  <div class="markdown" @click="pick" v-html="parts.html" />
</template>
