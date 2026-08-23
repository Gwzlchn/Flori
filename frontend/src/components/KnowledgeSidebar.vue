<script setup lang="ts">
import { computed } from "vue";

import type { components } from "../api/client";

const props = defineProps<{
  domains: components["schemas"]["DomainView"][];
  collections: components["schemas"]["CollectionView"][];
  sources: components["schemas"]["SourceView"][];
  activeSourceId: string | undefined;
  selectedDomainId: string;
  selectedCollectionId: string;
  status: string;
}>();
const emit = defineEmits<{
  open: [jobId: string];
  select: [domainId: string, collectionId?: string];
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
</script>

<template>
  <aside class="sidebar">
    <a
      class="brand"
      href="#workspace"
      aria-label="Flori 知识库"
    >
      <span class="brand-mark">✦</span>
      <span><strong>Flori</strong><small>Knowledge base</small></span>
    </a>
    <div class="library-heading">
      <span>知识库</span><small>{{ status }}</small>
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
          :class="{ 'is-active': selectedDomainId === domain.domain_id && !selectedCollectionId }"
          @click="emit('select', domain.domain_id)"
        >
          <span>▾</span><b>{{ domain.name }}</b><small>{{ domain.source_count }}</small>
        </button>
        <div
          v-for="collection in collectionsOf(domain.domain_id)"
          :key="collection.collection_id"
          class="collection-group"
        >
          <button
            type="button"
            class="tree-collection"
            :class="{ 'is-active': selectedCollectionId === collection.collection_id }"
            @click="emit('select', domain.domain_id, collection.collection_id)"
          >
            <span>▤</span><b>{{ collection.name }}</b><small>{{ collection.source_count }}</small>
          </button>
          <button
            v-for="source in sourcesOf(domain.domain_id, collection.collection_id)"
            :key="source.source_id"
            type="button"
            class="tree-source"
            :class="{ 'is-active': activeSourceId === source.source_id }"
            :disabled="!source.current_job_id"
            @click="source.current_job_id && emit('open', source.current_job_id)"
          >
            <span>◦</span><b>{{ source.title ?? source.canonical_ref }}</b>
          </button>
        </div>
        <div
          v-if="sourcesOf(domain.domain_id).length"
          class="collection-group"
        >
          <button
            type="button"
            class="tree-collection"
            :class="{ 'is-active': selectedDomainId === domain.domain_id && !selectedCollectionId }"
            @click="emit('select', domain.domain_id)"
          >
            <span>⌂</span><b>未归分类</b><small>{{ sourcesOf(domain.domain_id).length }}</small>
          </button>
          <button
            v-for="source in sourcesOf(domain.domain_id)"
            :key="source.source_id"
            type="button"
            class="tree-source"
            :class="{ 'is-active': activeSourceId === source.source_id }"
            :disabled="!source.current_job_id"
            @click="source.current_job_id && emit('open', source.current_job_id)"
          >
            <span>◦</span><b>{{ source.title ?? source.canonical_ref }}</b>
          </button>
        </div>
      </section>
    </nav>
    <a
      class="sidebar-upload"
      href="#upload"
    ><span>＋</span>投递到 {{ selectedLabel }}</a>
    <footer class="side-footer">
      <span class="health-dot" /> vNext · current only
    </footer>
  </aside>
</template>
