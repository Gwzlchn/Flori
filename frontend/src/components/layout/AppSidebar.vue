<script setup lang="ts">
import { useRoute } from 'vue-router'
import { Home, ListTodo, Library, Search, BookA, HardDrive, Settings } from 'lucide-vue-next'

const route = useRoute()

const navItems = [
  { path: '/', label: '首页', icon: Home },
  { path: '/jobs', label: '任务', icon: ListTodo },
  { path: '/collections', label: '集合', icon: Library },
  { path: '/search', label: '搜索', icon: Search },
  { path: '/glossary', label: '术语', icon: BookA },
  { path: '/workers', label: 'Worker', icon: HardDrive },
  { path: '/settings', label: '设置', icon: Settings },
]

function isActive(path: string) {
  if (path === '/') return route.path === '/'
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
