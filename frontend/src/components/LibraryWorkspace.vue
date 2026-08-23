<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";
import UiIcon from "./UiIcon.vue";

const props = defineProps<{
  domain: components["schemas"]["DomainView"] | undefined;
  collection: components["schemas"]["CollectionView"] | undefined;
  collections: components["schemas"]["CollectionView"][];
  sources: components["schemas"]["SourceView"][];
}>();
const emit = defineEmits<{
  open: [jobId: string];
  select: [domainId: string, collectionId?: string];
}>();

const domainCollections = computed(() => props.collections.filter((item) => item.domain_id === props.domain?.domain_id));
const visibleSources = computed(() => props.sources.filter((source) => source.domain_id === props.domain?.domain_id
  && (props.collection ? source.collection_ids.includes(props.collection.collection_id) : source.collection_ids.length === 0)));
const kindLabel: Record<components["schemas"]["SourceKind"], string> = {
  arxiv: "arXiv", pdf_url: "PDF URL", pdf_upload: "PDF", bilibili_video: "Bilibili",
  bilibili_channel: "Bilibili 频道", youtube_video: "YouTube", youtube_channel: "YouTube 频道",
  local_video: "本地视频",
};
</script>

<template>
  <section class="library-workspace">
    <header>
      <p class="eyebrow">
        {{ collection ? "Collection" : "Knowledge domain" }}
      </p>
      <h1>{{ collection?.name ?? domain?.name ?? "知识库" }}</h1>
      <p>{{ domain?.description ?? "把论文转成可检索、可回到原文的研究笔记。" }}</p>
      <dl v-if="domain">
        <div><dt>内容</dt><dd>{{ domain.source_count }}</dd></div>
        <div><dt>分类</dt><dd>{{ domain.collection_count }}</dd></div>
        <div v-if="collection">
          <dt>类型</dt><dd>{{ collection.kind === "manual" ? "手动分类" : "订阅分类" }}</dd>
        </div>
      </dl>
    </header>

    <div
      v-if="!collection && domainCollections.length"
      class="collection-grid"
    >
      <button
        v-for="item in domainCollections"
        :key="item.collection_id"
        type="button"
        @click="emit('select', item.domain_id, item.collection_id)"
      >
        <span><UiIcon name="folder" /><b>{{ item.name }}</b></span>
        <small>{{ item.source_count }} 项内容 · {{ item.kind === "manual" ? "手动" : "订阅" }}</small>
      </button>
    </div>

    <section class="content-list">
      <div class="section-heading">
        <div>
          <p class="eyebrow">
            Content
          </p><h2>{{ collection ? collection.name : "未归分类" }}</h2>
        </div>
        <span>{{ visibleSources.length }} 项</span>
      </div>
      <button
        v-for="source in visibleSources"
        :key="source.source_id"
        type="button"
        :disabled="!source.current_job_id"
        @click="source.current_job_id && emit('open', source.current_job_id)"
      >
        <span class="source-icon"><UiIcon name="file" /></span>
        <span><b>{{ source.title ?? source.canonical_ref }}</b><small>{{ kindLabel[source.kind] }} · {{ source.canonical_ref }}</small></span>
        <em>{{ source.current_job_id ? "打开成果" : "尚未发布" }}</em>
      </button>
      <p
        v-if="!visibleSources.length"
        class="empty-state"
      >
        这里还没有内容。可从下方投递一篇 PDF。
      </p>
    </section>
  </section>
</template>

<style scoped>
.library-workspace { display: grid; gap: 18px; padding: 26px 0 10px; }
header { max-width: 780px; }
header > p { max-width: 680px; color: var(--muted); line-height: 1.7; }
header dl { display: flex; gap: 22px; margin: 18px 0 0; }
header dl div { display: grid; gap: 2px; }
header dt { color: var(--muted); font-size: 11px; }
header dd { margin: 0; color: var(--ink); font-size: 18px; font-weight: 650; }
.collection-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; }
.collection-grid button, .content-list > button { border: 1px solid var(--line); border-radius: 7px; color: var(--ink); text-align: left; background: var(--surface); box-shadow: var(--shadow); cursor: pointer; }
.collection-grid button { display: grid; gap: 6px; padding: 13px; }
.collection-grid button:hover, .content-list > button:hover:not(:disabled) { border-color: #b8d7f2; background: #fbfdff; }
.collection-grid span { display: flex; gap: 8px; align-items: center; }
.collection-grid small, .content-list small { color: var(--muted); }
.content-list { overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
.section-heading { display: flex; align-items: end; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--line); }
.section-heading p, .section-heading h2 { margin-bottom: 0; }
.section-heading > span { color: var(--muted); font-size: 12px; }
.content-list > button { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 11px; align-items: center; width: 100%; padding: 11px 14px; border: 0; border-bottom: 1px solid var(--line-soft); border-radius: 0; box-shadow: none; }
.content-list > button > span:nth-child(2) { display: grid; gap: 2px; min-width: 0; }
.content-list b, .content-list small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.content-list em { color: var(--brand-dark); font-size: 11px; font-style: normal; }
.content-list button:disabled { cursor: not-allowed; opacity: .55; }
.source-icon { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 6px; color: #6155a6; background: #f1eff8; }
.empty-state { margin: 0; padding: 28px 16px; text-align: center; }
@media (max-width: 620px) {
  .library-workspace { padding-top: 18px; }
  .collection-grid { grid-template-columns: 1fr; }
  .content-list > button { grid-template-columns: auto minmax(0, 1fr); }
  .content-list em { grid-column: 2; }
}
</style>
