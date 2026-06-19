<!-- 根组件：套 AppShell，内嵌 <router-view>。对应原型 .app 整体外壳。 -->
<script setup lang="ts">
import AppShell from '@/components/layout/AppShell.vue'
</script>

<template>
  <AppShell>
    <router-view />
  </AppShell>
</template>
