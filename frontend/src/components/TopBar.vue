<script setup lang="ts">
import SearchPanel from "./SearchPanel.vue";
import UiIcon from "./UiIcon.vue";

defineProps<{
  activeView: "about" | "content" | "library" | "system";
  canGoBack: boolean;
  collectionName: string | undefined;
  domainName: string;
  title: string;
}>();
const emit = defineEmits<{
  back: [];
  collection: [];
  domain: [];
  library: [];
  menu: [];
  open: [jobId: string, evidenceId?: string];
}>();
function open(jobId: string, evidenceId?: string): void { emit("open", jobId, evidenceId); }
</script>

<template>
  <header class="topbar">
    <button
      type="button"
      class="mobile-menu"
      aria-label="打开导航"
      @click="emit('menu')"
    >
      <UiIcon name="menu" />
    </button>
    <button
      v-if="canGoBack"
      type="button"
      class="topbar-back"
      aria-label="返回知识库"
      @click="emit('back')"
    >
      ←
    </button>
    <nav
      class="breadcrumb"
      aria-label="面包屑"
    >
      <button
        type="button"
        @click="emit('library')"
      >
        知识库
      </button><b>/</b>
      <template v-if="activeView === 'content'">
        <button
          type="button"
          @click="emit('domain')"
        >
          {{ domainName }}
        </button><b>/</b>
        <button
          v-if="collectionName"
          type="button"
          @click="emit('collection')"
        >
          {{ collectionName }}
        </button><b v-if="collectionName">/</b>
      </template>
      <strong>{{ title }}</strong>
    </nav>
    <SearchPanel @open="open" />
  </header>
</template>

<style scoped>
.mobile-menu { display: none; width: 34px; height: 34px; place-items: center; padding: 0; border: 0; border-radius: 5px; color: var(--muted); background: transparent; cursor: pointer; }
.mobile-menu:hover { color: var(--ink); background: var(--line-soft); }
@media (max-width: 980px) { .mobile-menu { display: grid; } }
</style>
