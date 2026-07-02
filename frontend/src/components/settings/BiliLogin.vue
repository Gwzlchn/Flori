<script setup lang="ts">
import { ref, inject, onMounted, onUnmounted } from 'vue'
import { QrCode, RefreshCw, LogOut, CheckCircle, Loader } from 'lucide-vue-next'
import { useApi } from '../../composables/useApi'
import type { BiliStatus, BiliLoginStart, BiliLoginPoll } from '../../types'

const api = useApi()
const showToast = inject<(msg: string, type: 'success' | 'error' | 'info') => void>('showToast')

const loggedIn = ref(false)
const uname = ref<string | null>(null)
const statusLoading = ref(true)

// 扫码态:idle 未开始 / starting 生成中 / waiting 等待扫码 / scanned 已扫待确认 / expired 已过期。
const phase = ref<'idle' | 'starting' | 'waiting' | 'scanned' | 'expired'>('idle')
const qrPng = ref('')
const qrcodeKey = ref('')
const loggingOut = ref(false)

let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function refreshStatus() {
  statusLoading.value = true
  try {
    const s = await api.get<BiliStatus>('/api/bili/status')
    loggedIn.value = s.logged_in
    uname.value = s.uname
  } finally {
    statusLoading.value = false
  }
}

async function startLogin() {
  phase.value = 'starting'
  qrPng.value = ''
  try {
    const data = await api.post<BiliLoginStart>('/api/bili/login/start')
    qrPng.value = data.qr_png
    qrcodeKey.value = data.qrcode_key
    phase.value = 'waiting'
    startPolling()
  } catch {
    phase.value = 'idle'
    showToast?.('生成二维码失败', 'error')
  }
}

function startPolling() {
  stopPolling()
  // 每 2s 轮询扫码状态,confirmed/expired 终止。
  pollTimer = setInterval(async () => {
    try {
      const data = await api.get<BiliLoginPoll>(
        `/api/bili/login/poll?qrcode_key=${encodeURIComponent(qrcodeKey.value)}`
      )
      if (data.state === 'scanned') {
        phase.value = 'scanned'
      } else if (data.state === 'confirmed') {
        stopPolling()
        phase.value = 'idle'
        await refreshStatus()
        showToast?.('B站登录成功', 'success')
      } else if (data.state === 'expired') {
        stopPolling()
        phase.value = 'expired'
      }
    } catch {
      // 轮询瞬时失败忽略,下个周期重试。
    }
  }, 2000)
}

async function logout() {
  loggingOut.value = true
  try {
    await api.post<{ ok: boolean }>('/api/bili/logout')
    await refreshStatus()
    showToast?.('已注销', 'success')
  } catch {
    showToast?.('注销失败', 'error')
  } finally {
    loggingOut.value = false
  }
}

onMounted(refreshStatus)
onUnmounted(stopPolling)
</script>

<template>
  <div class="bg-white border border-gray-200 rounded-xl p-4 space-y-3">
    <h4 class="text-sm font-semibold text-gray-700 flex items-center gap-2">
      <QrCode :size="16" />
      B站扫码登录
    </h4>

    <!-- 状态加载中 -->
    <div v-if="statusLoading" class="flex items-center gap-2 text-sm text-gray-500">
      <Loader :size="16" class="animate-spin" />
      读取登录状态...
    </div>

    <!-- 已登录:展示用户名 + 注销 -->
    <div v-else-if="loggedIn" class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-2 text-sm text-green-600">
        <CheckCircle :size="16" />
        <span>已登录{{ uname ? ` · ${uname}` : '' }}</span>
      </div>
      <button
        @click="logout"
        :disabled="loggingOut"
        class="flex items-center gap-1 px-3 py-1.5 bg-gray-100 text-gray-700 text-xs rounded-lg hover:bg-gray-200 transition-colors disabled:opacity-50"
      >
        <LogOut :size="14" />
        注销
      </button>
    </div>

    <!-- 未登录 -->
    <div v-else class="space-y-3">
      <!-- 未开始:扫码登录按钮 -->
      <button
        v-if="phase === 'idle'"
        @click="startLogin"
        class="flex items-center gap-1.5 px-4 py-2 bg-pink-500 text-white rounded-lg text-sm font-medium hover:bg-pink-600 transition-colors"
      >
        <QrCode :size="16" />
        扫码登录
      </button>

      <!-- 生成中 -->
      <div v-else-if="phase === 'starting'" class="flex items-center gap-2 text-sm text-gray-500">
        <Loader :size="16" class="animate-spin" />
        生成二维码...
      </div>

      <!-- 等待扫码 / 已扫码 -->
      <div v-else-if="phase === 'waiting' || phase === 'scanned'" class="space-y-3">
        <div class="w-48 h-48 bg-white border border-gray-200 rounded-lg flex items-center justify-center">
          <img :src="qrPng" alt="B站登录二维码" class="w-44 h-44" />
        </div>
        <p class="text-sm" :class="phase === 'scanned' ? 'text-blue-600' : 'text-gray-600'">
          {{ phase === 'scanned' ? '已扫码，请在手机确认' : '请用 B站 App 扫码' }}
        </p>
      </div>

      <!-- 已过期:允许重新生成 -->
      <div v-else-if="phase === 'expired'" class="space-y-2">
        <p class="text-sm text-orange-600">二维码已过期</p>
        <button
          @click="startLogin"
          class="flex items-center gap-1.5 px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded-lg transition-colors"
        >
          <RefreshCw :size="14" />
          重新生成
        </button>
      </div>
    </div>
  </div>
</template>
