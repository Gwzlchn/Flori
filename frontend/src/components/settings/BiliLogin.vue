<script setup lang="ts">
import { ref, inject, computed, onMounted, onUnmounted } from 'vue'
import { QrCode, RefreshCw, LogOut, Loader, X } from 'lucide-vue-next'
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
const modalOpen = computed(() => phase.value !== 'idle')

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

function closeLogin() {
  stopPolling()
  phase.value = 'idle'
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
  <div class="platform-row">
    <span class="type-pill bili-icon"><QrCode :size="17" /></span>
    <div class="body">
      <div class="title">Bilibili</div>
      <div class="meta">
        <span v-if="statusLoading" class="badge b-mut">读取中</span>
        <span v-else-if="loggedIn" class="badge b-ok">已登录</span>
        <span v-else class="badge b-warn">待登录</span>
        <span>{{ loggedIn ? (uname || '账号已可用') : '扫码后可下载会员/限制内容' }}</span>
      </div>
    </div>
    <button v-if="statusLoading" class="btn sm" disabled><Loader :size="14" class="spin" />读取中</button>
    <button v-else-if="loggedIn" class="btn sm" :disabled="loggingOut" @click="logout">
      <LogOut :size="14" />注销
    </button>
    <button v-else class="btn sm" :disabled="phase !== 'idle'" @click="startLogin">
      <QrCode :size="14" />{{ phase === 'idle' ? '扫码登录' : '扫码中' }}
    </button>

    <Teleport to="body">
      <div v-if="modalOpen" class="overlay show" @click.self="closeLogin">
        <div class="modal bili-modal">
          <div class="hd">
            <QrCode :size="18" class="lead-ic" />
            <b>B站扫码登录</b>
            <button class="iconbtn" title="关闭" @click="closeLogin"><X :size="16" /></button>
          </div>
          <div class="bd bili-body">
            <div v-if="phase === 'starting'" class="bili-state">
              <Loader :size="18" class="spin" />
              <span>生成二维码…</span>
            </div>
            <template v-else-if="phase === 'waiting' || phase === 'scanned'">
              <div class="qr-box"><img :src="qrPng" alt="B站登录二维码" /></div>
              <p :class="phase === 'scanned' ? 'ok' : ''">
                {{ phase === 'scanned' ? '已扫码,请在手机确认' : '请用 B站 App 扫码' }}
              </p>
            </template>
            <div v-else-if="phase === 'expired'" class="bili-state">
              <span class="warn">二维码已过期</span>
              <button class="btn sm" @click="startLogin"><RefreshCw :size="14" />重新生成</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.bili-icon { background: var(--brand-50); color: var(--brand-600); }
.spin { animation: spin 1s linear infinite; }
.bili-modal { max-width: 360px; }
.bili-body { display: flex; flex-direction: column; align-items: center; gap: 12px; text-align: center; }
.bili-state { display: flex; align-items: center; gap: 8px; color: var(--ink-500); font-size: 13px; min-height: 178px; }
.qr-box { width: 176px; height: 176px; display: grid; place-items: center; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface); }
.qr-box img { width: 160px; height: 160px; }
.bili-body p { font-size: 13px; color: var(--ink-600); }
.bili-body p.ok { color: var(--brand-700); }
.warn { color: var(--warn); }
</style>
