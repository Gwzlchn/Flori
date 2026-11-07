<script setup lang="ts">
import { onMounted, ref } from "vue";

import { api } from "./client";

const version = ref("");

onMounted(async () => {
  const { data } = await api.GET("/api/health");
  version.value = data?.version ?? "";
});
</script>

<template>
  <main>
    <h1>Flori</h1>
    <p>{{ version }}</p>
  </main>
</template>
