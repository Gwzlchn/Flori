<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../api/client";
import UiIcon from "./UiIcon.vue";

const floriLogo = new URL("../assets/flori-logo.png", import.meta.url).href;

const props = defineProps<{
  domains: components["schemas"]["DomainView"][];
  collections: components["schemas"]["CollectionView"][];
  sources: components["schemas"]["SourceView"][];
  activeSourceId: string | undefined;
  selectedDomainId: string;
  selectedCollectionId: string;
  status: string;
  collapsed: boolean;
}>();
const emit = defineEmits<{
  open: [jobId: string];
  select: [domainId: string, collectionId?: string];
  submit: [];
  close: [];
  toggle: [];
}>();

function collectionsOf(domainId: string): components["schemas"]["CollectionView"][] {
  return props.collections.filter((item) => item.domain_id === domainId);
}
function sourcesOf(domainId: string, collectionId?: string): components["schemas"]["SourceView"][] {
  return props.sources.filter((source) => source.domain_id === domainId
    && (collectionId ? source.collection_ids.includes(collectionId) : source.collection_ids.length === 0));
}
const selectedLabel = computed(() => props.collections.find((item) => item.collection_id === props.selectedCollectionId)?.name
  ?? props.domains.find((item) => item.domain_id === props.selectedDomainId)?.name ?? "知识库");
const expandedDomains = ref<Set<string>>(new Set());
const expandedCollections = ref<Set<string>>(new Set());

function toggled(values: Set<string>, id: string): Set<string> {
  const next = new Set(values);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}
function selectDomain(domainId: string): void {
  expandedDomains.value = toggled(expandedDomains.value, domainId);
  emit("select", domainId);
}
function selectCollection(domainId: string, collectionId: string): void {
  expandedDomains.value = new Set(expandedDomains.value).add(domainId);
  expandedCollections.value = toggled(expandedCollections.value, collectionId);
  emit("select", domainId, collectionId);
}
function selectUncategorized(domainId: string): void {
  const key = `uncategorized:${domainId}`;
  expandedCollections.value = toggled(expandedCollections.value, key);
  emit("select", domainId);
}

watch(() => [props.selectedDomainId, props.selectedCollectionId] as const, ([domainId, collectionId]) => {
  if (domainId) expandedDomains.value = new Set(expandedDomains.value).add(domainId);
  if (collectionId) expandedCollections.value = new Set(expandedCollections.value).add(collectionId);
}, { immediate: true });
</script>

<template>
  <aside class="sidebar">
    <a
      class="brand"
      href="#workspace"
      aria-label="Flori 知识库"
    >
      <span class="brand-mark"><img
        :src="floriLogo"
        alt=""
      ></span>
      <span><strong>Flori</strong><small>Knowledge base</small></span>
    </a>
    <button
      type="button"
      class="sidebar-close"
      aria-label="关闭导航"
      @click="emit('close')"
    >
      ×
    </button>
    <div class="sidebar-primary">
      <button
        type="button"
        @click="emit('submit')"
      >
        <UiIcon name="send" /><span>投递内容</span>
      </button>
      <span title="当前发布成果"><UiIcon
        name="archive"
        :size="17"
      /></span>
    </div>
    <div class="library-heading">
      <span><UiIcon
        name="book"
        :size="15"
      />知识库</span><small>{{ status }}</small>
    </div>
    <nav
      class="library-tree"
      aria-label="领域、分类与内容"
    >
      <section
        v-for="domain in domains"
        :key="domain.domain_id"
        class="domain-group"
      >
        <button
          type="button"
          class="tree-domain"
          :class="{ 'is-active': selectedDomainId === domain.domain_id && !selectedCollectionId, 'is-expanded': expandedDomains.has(domain.domain_id) }"
          :aria-expanded="expandedDomains.has(domain.domain_id)"
          @click="selectDomain(domain.domain_id)"
        >
          <UiIcon
            name="chevron"
            :size="13"
          /><b>{{ domain.name }}</b><small>{{ domain.source_count }}</small>
        </button>
        <div
          v-for="collection in collectionsOf(domain.domain_id)"
          v-show="expandedDomains.has(domain.domain_id)"
          :key="collection.collection_id"
          class="collection-group"
        >
          <button
            type="button"
            class="tree-collection"
            :class="{ 'is-active': selectedCollectionId === collection.collection_id, 'is-expanded': expandedCollections.has(collection.collection_id) }"
            :aria-expanded="expandedCollections.has(collection.collection_id)"
            @click="selectCollection(domain.domain_id, collection.collection_id)"
          >
            <UiIcon
              name="chevron"
              :size="14"
            /><b>{{ collection.name }}</b><small>{{ collection.source_count }}</small>
          </button>
          <button
            v-for="source in sourcesOf(domain.domain_id, collection.collection_id)"
            v-show="expandedCollections.has(collection.collection_id)"
            :key="source.source_id"
            type="button"
            class="tree-source"
            :class="{ 'is-active': activeSourceId === source.source_id }"
            :disabled="!source.current_job_id"
            @click="source.current_job_id && emit('open', source.current_job_id)"
          >
            <UiIcon
              name="file"
              :size="13"
            /><b>{{ source.title ?? source.canonical_ref }}</b>
          </button>
        </div>
        <div
          v-if="expandedDomains.has(domain.domain_id) && sourcesOf(domain.domain_id).length"
          class="collection-group"
        >
          <button
            type="button"
            class="tree-collection"
            :class="{ 'is-active': selectedDomainId === domain.domain_id && !selectedCollectionId, 'is-expanded': expandedCollections.has(`uncategorized:${domain.domain_id}`) }"
            :aria-expanded="expandedCollections.has(`uncategorized:${domain.domain_id}`)"
            @click="selectUncategorized(domain.domain_id)"
          >
            <UiIcon
              name="folder"
              :size="14"
            /><b>未归分类</b><small>{{ sourcesOf(domain.domain_id).length }}</small>
          </button>
          <button
            v-for="source in sourcesOf(domain.domain_id)"
            v-show="expandedCollections.has(`uncategorized:${domain.domain_id}`)"
            :key="source.source_id"
            type="button"
            class="tree-source"
            :class="{ 'is-active': activeSourceId === source.source_id }"
            :disabled="!source.current_job_id"
            @click="source.current_job_id && emit('open', source.current_job_id)"
          >
            <UiIcon
              name="file"
              :size="13"
            /><b>{{ source.title ?? source.canonical_ref }}</b>
          </button>
        </div>
      </section>
    </nav>
    <button
      class="sidebar-upload"
      type="button"
      @click="emit('submit')"
    >
      <UiIcon
        name="send"
        :size="14"
      />投递到 {{ selectedLabel }}
    </button>
    <footer class="side-footer">
      <span class="health-copy"><span class="health-dot" /> Home Core 已连接</span>
      <button
        type="button"
        class="sidebar-toggle"
        :aria-label="collapsed ? '展开导航' : '收起导航'"
        :title="collapsed ? '展开导航' : '收起导航'"
        @click="emit('toggle')"
      >
        <UiIcon
          name="panel"
          :size="16"
        />
      </button>
    </footer>
  </aside>
</template>

<style scoped>
.sidebar-close, .sidebar-toggle { display: grid; place-items: center; border: 0; color: var(--muted); background: transparent; cursor: pointer; }
.sidebar-close { position: absolute; top: 16px; right: 12px; display: none; width: 32px; height: 32px; border-radius: 5px; font-size: 22px; }
.sidebar-close:hover, .sidebar-toggle:hover { color: var(--ink); background: var(--line-soft); }
.side-footer { justify-content: space-between; }
.health-copy { display: flex; gap: 7px; align-items: center; min-width: 0; }
.sidebar-toggle { width: 30px; height: 30px; flex: none; border-radius: 5px; }
:global(.sidebar-collapsed) .sidebar { align-items: center; padding-inline: 8px; }
:global(.sidebar-collapsed) .brand { padding-inline: 0; }
:global(.sidebar-collapsed) .brand span:last-child,
:global(.sidebar-collapsed) .library-heading,
:global(.sidebar-collapsed) .library-tree,
:global(.sidebar-collapsed) .sidebar-upload,
:global(.sidebar-collapsed) .health-copy,
:global(.sidebar-collapsed) .sidebar-primary button span { display: none; }
:global(.sidebar-collapsed) .sidebar-primary { grid-template-columns: 34px; }
:global(.sidebar-collapsed) .sidebar-primary button { justify-content: center; padding: 0; }
@media (max-width: 980px) {
  .sidebar-close { display: grid; }
  :global(.sidebar-collapsed) .sidebar { align-items: stretch; padding: 15px 12px; }
  :global(.sidebar-collapsed) .brand { padding: 2px 8px 12px; }
  :global(.sidebar-collapsed) .brand span:last-child,
  :global(.sidebar-collapsed) .library-heading,
  :global(.sidebar-collapsed) .library-tree,
  :global(.sidebar-collapsed) .sidebar-upload,
  :global(.sidebar-collapsed) .health-copy,
  :global(.sidebar-collapsed) .sidebar-primary button span { display: initial; }
  :global(.sidebar-collapsed) .sidebar-primary { grid-template-columns: minmax(0, 1fr) 34px; }
  :global(.sidebar-collapsed) .sidebar-primary button { justify-content: flex-start; padding: 0 10px; }
  .sidebar-toggle { display: none; }
}
.tree-domain :deep(.ui-icon), .tree-collection :deep(.ui-icon) { transition: transform .12s ease; }
.tree-domain.is-expanded :deep(.ui-icon), .tree-collection.is-expanded :deep(.ui-icon) { transform: rotate(90deg); }
</style>
