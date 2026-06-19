<script setup lang="ts">
import { ref } from 'vue'
import AppSidebar from './AppSidebar.vue'
import TopBar from './TopBar.vue'

// 折叠态(rail):窄侧栏只留图标。由 .app.rail 的 mnemo.css 规则驱动。
const rail = ref(false)
</script>

<template>
  <div class="app" :class="{ rail }">
    <AppSidebar @toggle-rail="rail = !rail" />
    <div class="main">
      <TopBar />
      <router-view />
    </div>
  </div>
</template>
