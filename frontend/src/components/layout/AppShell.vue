<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './AppSidebar.vue'
import TopBar from './TopBar.vue'

const RAIL_KEY = 'flori.sidebar.rail'
function loadRail(): boolean {
  try { return localStorage.getItem(RAIL_KEY) === '1' } catch { return false }
}

// 折叠态(rail):窄侧栏只留图标。由 .app.rail 的 flori.css 规则驱动。
const rail = ref(loadRail())
function toggleRail() {
  rail.value = !rail.value
  try { localStorage.setItem(RAIL_KEY, rail.value ? '1' : '0') } catch { /* ignore */ }
}

// 移动端抽屉:窄屏下侧栏化为左侧抽屉,由顶栏汉堡开合;遮罩 / 导航 / 路由变化关闭。
const mobileOpen = ref(false)
const route = useRoute()
watch(() => route.fullPath, () => { mobileOpen.value = false })
</script>

<template>
  <div class="app" :class="{ rail, 'mobile-open': mobileOpen }">
    <AppSidebar
      :mobile-open="mobileOpen"
      :rail="rail"
      @toggle-rail="toggleRail"
      @nav="mobileOpen = false"
    />
    <div class="scrim" @click="mobileOpen = false" />
    <div class="main">
      <TopBar @toggle-mobile="mobileOpen = !mobileOpen" />
      <router-view />
    </div>
  </div>
</template>
