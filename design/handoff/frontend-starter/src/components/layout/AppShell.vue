<!-- 应用外壳：.app 网格 = 侧栏 + 主区（顶栏 + <slot/>）。对应原型 .app / .main 结构。 -->
<script setup lang="ts">
import { ref } from 'vue'
import AppSidebar from './AppSidebar.vue'
import TopBar from './TopBar.vue'

// 侧栏折叠态 -> .app.rail（只剩图标 + hover 提示）
const railed = ref(false)
function toggleRail() {
  railed.value = !railed.value
}
</script>

<template>
  <div class="app" :class="{ rail: railed }">
    <AppSidebar :railed="railed" @toggle-rail="toggleRail" />
    <div class="main">
      <TopBar />
      <slot />
    </div>
  </div>
</template>
