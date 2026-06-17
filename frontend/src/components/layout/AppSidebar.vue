<script setup lang="ts">
import { useRoute } from 'vue-router'
import { Layers, ListTodo, Search, Settings } from 'lucide-vue-next'

const route = useRoute()

// 领域中心 IA：领域为锚 + 跨域逃生口(全部内容/搜索) + 设置(含 Worker 运维)。
// 集合/术语在领域工作台内归口，不占顶级导航。
const navItems = [
  { path: '/', label: '领域', icon: Layers },
  { path: '/jobs', label: '全部内容', icon: ListTodo },
  { path: '/search', label: '搜索', icon: Search },
  { path: '/settings', label: '设置', icon: Settings },
]

function isActive(path: string) {
  // 领域(/) 在领域总览与领域工作台(/domains/*)下都高亮；集合详情也归领域。
  if (path === '/') return route.path === '/' || route.path.startsWith('/domains') || route.path.startsWith('/collections')
  return route.path.startsWith(path)
}
</script>

<template>
  <aside class="w-56 bg-white border-r border-gray-200 flex flex-col h-screen sticky top-0">
    <div class="p-4 border-b border-gray-200">
      <h1 class="text-lg font-bold text-gray-800">Mnemo</h1>
    </div>
    <nav class="flex-1 py-2">
      <router-link
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        class="flex items-center gap-3 px-4 py-2.5 mx-2 rounded-lg text-sm transition-colors"
        :class="isActive(item.path) ? 'bg-blue-50 text-blue-700 font-medium' : 'text-gray-600 hover:bg-gray-50'"
      >
        <component :is="item.icon" :size="18" />
        <span>{{ item.label }}</span>
      </router-link>
    </nav>
  </aside>
</template>
