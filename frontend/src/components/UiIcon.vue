<script setup lang="ts">
defineProps<{
  name: "archive" | "book" | "chevron" | "external" | "file" | "folder" | "image"
    | "info" | "pipeline" | "refresh" | "search" | "send" | "settings";
  size?: number;
}>();
</script>

<template>
  <svg
    class="ui-icon"
    :width="size ?? 16"
    :height="size ?? 16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <template v-if="name === 'archive'">
      <path d="M4 7h16v13H4zM3 3h18v4H3z" /><path d="M9 11h6" />
    </template>
    <template v-else-if="name === 'book'">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5z" /><path d="M4 6.5v13" />
    </template>
    <path
      v-else-if="name === 'chevron'"
      d="m9 18 6-6-6-6"
    />
    <template v-else-if="name === 'external'">
      <path d="M15 3h6v6M10 14 21 3" /><path d="M18 13v7a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h7" />
    </template>
    <template v-else-if="name === 'file'">
      <path d="M14 2H6a2 2 0 0 0-2 2v16h16V8z" /><path d="M14 2v6h6M8 13h8M8 17h6" />
    </template>
    <path
      v-else-if="name === 'folder'"
      d="M3 6h7l2 2h9v11H3z"
    />
    <template v-else-if="name === 'image'">
      <rect
        x="3"
        y="4"
        width="18"
        height="16"
        rx="2"
      /><circle
        cx="9"
        cy="10"
        r="2"
      /><path d="m21 15-5-5L5 20" />
    </template>
    <template v-else-if="name === 'info'">
      <circle
        cx="12"
        cy="12"
        r="9"
      /><path d="M12 11v6M12 7h.01" />
    </template>
    <template v-else-if="name === 'pipeline'">
      <circle
        cx="5"
        cy="6"
        r="2"
      /><circle
        cx="19"
        cy="6"
        r="2"
      /><circle
        cx="12"
        cy="18"
        r="2"
      /><path d="M7 6h10M6 8l5 8M18 8l-5 8" />
    </template>
    <path
      v-else-if="name === 'refresh'"
      d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"
    />
    <template v-else-if="name === 'search'">
      <circle
        cx="11"
        cy="11"
        r="7"
      /><path d="m20 20-4-4" />
    </template>
    <template v-else-if="name === 'send'">
      <path d="m22 2-7 20-4-9-9-4zM22 2 11 13" />
    </template>
    <template v-else>
      <circle
        cx="12"
        cy="12"
        r="3"
      /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z" />
    </template>
  </svg>
</template>
