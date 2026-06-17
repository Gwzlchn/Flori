<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import { useGlobalStore } from '../stores/global'
import BiliLogin from '../components/settings/BiliLogin.vue'
import CookieUpload from '../components/auth/CookieUpload.vue'
import StatusBadge from '../components/common/StatusBadge.vue'
import ProfileEditor from '../components/settings/ProfileEditor.vue'
import type { AuthStatus } from '../types'
import { Shield, Globe, BookOpen, ChevronRight, HardDrive } from 'lucide-vue-next'

const api = useApi()
const globalStore = useGlobalStore()

const authStatus = ref<AuthStatus | null>(null)
const loading = ref(true)

onMounted(async () => {
  try {
    const [auth] = await Promise.all([
      api.get<AuthStatus>('/api/auth/status'),
      globalStore.fetchProfiles(),
    ])
    authStatus.value = auth
  } finally {
    loading.value = false
  }
})

async function refreshAuth() {
  authStatus.value = await api.get<AuthStatus>('/api/auth/status')
}

const editingDomain = ref<string | null>(null)

function openProfile(domain: string) {
  editingDomain.value = domain
}

async function onProfileSaved() {
  await globalStore.fetchProfiles()
}
</script>

<template>
  <div class="space-y-6">
    <h2 class="text-xl font-bold">设置</h2>

    <div v-if="loading" class="text-sm text-gray-400 py-8 text-center">加载中...</div>

    <template v-else>
      <!-- Platform Auth -->
      <section class="bg-white border border-gray-200 rounded-xl p-4 space-y-4">
        <h3 class="text-sm font-semibold text-gray-700 flex items-center gap-2">
          <Shield :size="16" />
          平台认证
        </h3>

        <!-- Bilibili：扫码登录走 /api/bili/* 契约，组件自管状态。 -->
        <BiliLogin />

        <hr class="border-gray-100" />

        <!-- YouTube -->
        <div class="space-y-2">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium">YouTube</span>
            <StatusBadge v-if="authStatus" :status="authStatus.youtube.has_cookies ? 'done' : 'failed'" />
            <span v-if="authStatus" class="text-xs text-gray-500">
              {{ authStatus.youtube.has_cookies ? '已配置' : '未配置' }}
            </span>
          </div>
          <CookieUpload platform="youtube" @success="refreshAuth" />
        </div>
      </section>

      <!-- Profiles -->
      <section class="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
        <h3 class="text-sm font-semibold text-gray-700 flex items-center gap-2">
          <BookOpen :size="16" />
          领域 Profile
        </h3>
        <div v-if="globalStore.profiles.length === 0" class="text-sm text-gray-400">暂无 Profile</div>
        <div v-else class="space-y-2">
          <button
            v-for="p in globalStore.profiles"
            :key="p.domain"
            @click="openProfile(p.domain)"
            class="w-full flex items-center justify-between py-2 px-2 -mx-2 rounded-lg border-b border-gray-50 last:border-0 hover:bg-gray-50 transition-colors text-left"
          >
            <div>
              <span class="text-sm font-medium">{{ p.domain }}</span>
              <span v-if="p.role" class="text-xs text-gray-500 ml-2">{{ p.role }}</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-400">{{ p.terminology_count }} 个术语</span>
              <ChevronRight :size="16" class="text-gray-300" />
            </div>
          </button>
        </div>
      </section>

      <!-- 运维：Worker 监控归入设置(不占顶级导航) -->
      <section class="bg-white border border-gray-200 rounded-xl p-4">
        <h3 class="text-sm font-semibold text-gray-700 flex items-center gap-2 mb-3">
          <HardDrive :size="16" />
          运维
        </h3>
        <router-link
          to="/workers"
          class="w-full flex items-center justify-between py-2 px-2 -mx-2 rounded-lg hover:bg-gray-50 transition-colors"
        >
          <span class="text-sm font-medium">Worker 监控</span>
          <ChevronRight :size="16" class="text-gray-300" />
        </router-link>
      </section>
    </template>

    <ProfileEditor
      v-if="editingDomain"
      :domain="editingDomain"
      @close="editingDomain = null"
      @saved="onProfileSaved"
    />
  </div>
</template>
