<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Layers, ListTodo, PlusCircle, MoreHorizontal, Library, Search, BookA, Settings, X } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()

const tabs = [
  { path: '/', label: '领域', icon: Layers },
  { path: '/jobs', label: '全部内容', icon: ListTodo },
  { path: '/', label: '投递', icon: PlusCircle, highlight: true, action: 'submit' },
]

// 次要页面收进“更多”抽屉：集合/搜索/术语/设置（Worker 在设置内）。
const moreItems = [
  { path: '/collections', label: '集合', icon: Library },
  { path: '/search', label: '搜索', icon: Search },
  { path: '/glossary', label: '术语', icon: BookA },
  { path: '/settings', label: '设置', icon: Settings },
]

const showMore = ref(false)

function isActive(path: string, action?: string) {
  if (action === 'submit') return false
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

const moreActive = () => moreItems.some(i => route.path.startsWith(i.path))

function handleTab(tab: typeof tabs[number]) {
  if (tab.action === 'submit') {
    if (route.path !== '/') {
      router.push('/')
    }
    setTimeout(() => {
      document.querySelector('[data-submit-form]')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 100)
    return
  }
  router.push(tab.path)
}

function goMore(path: string) {
  showMore.value = false
  router.push(path)
}
</script>

<template>
  <!-- 单根 + 自带 md:hidden:多根组件时父传的 md:hidden class 不会落到根上,
       会导致桌面端底部导航不隐藏、fixed 盖住内容底部("尾部被挡")。 -->
  <div class="md:hidden">
  <nav class="fixed bottom-0 left-0 right-0 z-40 bg-white border-t border-gray-200 flex justify-around py-1.5 safe-area-pb">
    <a
      v-for="(tab, idx) in tabs"
      :key="idx"
      @click.prevent="handleTab(tab)"
      class="flex flex-col items-center gap-0.5 px-2 py-1 text-xs transition-colors cursor-pointer"
      :class="[
        isActive(tab.path, tab.action) ? 'text-blue-600' : 'text-gray-500',
        tab.highlight ? 'text-blue-600' : ''
      ]"
    >
      <component :is="tab.icon" :size="20" :stroke-width="tab.highlight ? 2.5 : 1.5" />
      <span>{{ tab.label }}</span>
    </a>
    <a
      @click.prevent="showMore = true"
      class="flex flex-col items-center gap-0.5 px-2 py-1 text-xs transition-colors cursor-pointer"
      :class="moreActive() ? 'text-blue-600' : 'text-gray-500'"
    >
      <component :is="MoreHorizontal" :size="20" :stroke-width="1.5" />
      <span>更多</span>
    </a>
  </nav>

  <!-- “更多”抽屉：收纳次要页面 -->
  <transition name="more-fade">
    <div v-if="showMore" class="fixed inset-0 z-50 flex flex-col justify-end" @click.self="showMore = false">
      <div class="absolute inset-0 bg-black/30" />
      <div class="relative bg-white rounded-t-2xl safe-area-pb pt-2 pb-2 shadow-xl">
        <div class="flex items-center justify-between px-4 py-2">
          <span class="text-sm font-semibold text-gray-700">更多</span>
          <button @click="showMore = false" class="p-1 text-gray-400 hover:text-gray-600">
            <X :size="18" />
          </button>
        </div>
        <div class="grid grid-cols-4 gap-1 px-3 pb-2">
          <a
            v-for="item in moreItems"
            :key="item.path"
            @click.prevent="goMore(item.path)"
            class="flex flex-col items-center gap-1 px-2 py-3 rounded-lg text-xs cursor-pointer transition-colors"
            :class="route.path.startsWith(item.path) ? 'text-blue-600 bg-blue-50' : 'text-gray-600 hover:bg-gray-50'"
          >
            <component :is="item.icon" :size="22" :stroke-width="1.5" />
            <span>{{ item.label }}</span>
          </a>
        </div>
      </div>
    </div>
  </transition>
  </div>
</template>

<style scoped>
.safe-area-pb {
  padding-bottom: max(0.375rem, env(safe-area-inset-bottom));
}
.more-fade-enter-active,
.more-fade-leave-active {
  transition: opacity 0.15s ease;
}
.more-fade-enter-from,
.more-fade-leave-to {
  opacity: 0;
}
</style>
