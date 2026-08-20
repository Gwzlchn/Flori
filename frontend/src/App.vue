<script setup lang="ts">
import { apiClient } from "./api/client";

void apiClient;
</script>

<template>
  <main>
    <h1>Flori vNext</h1>
    <p>工程骨架已就绪。</p>
  </main>
</template>

<style scoped>
main {
  max-width: 48rem;
  margin: 4rem auto;
  padding: 0 1.5rem;
  font-family: system-ui, sans-serif;
}
</style>
