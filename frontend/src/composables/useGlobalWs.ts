import { ref, onUnmounted } from 'vue'
import type { SystemStatus } from '../types'
import { createWsReconnect } from './useWsReconnect'

const MAX_RETRIES = 10

const systemStatus = ref<SystemStatus | null>(null)
let started = false
let refCount = 0

const conn = createWsReconnect({
  url: () => '/api/ws/global',
  withToken: true,
  maxRetries: MAX_RETRIES,
  shouldReconnect: () => refCount > 0,
  onMessage: (data) => { systemStatus.value = JSON.parse(data) },
})

// 网络恢复 / 页面重新可见时,重置退避并重连:否则连续断开达 MAX_RETRIES 后会永久放弃,
// 只能整页刷新才恢复。仅在仍有页面引用(refCount>0)时触发。
function wake() {
  if (refCount > 0) conn.reconnect()
}
if (typeof window !== 'undefined') {
  window.addEventListener('online', wake)
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') wake()
  })
}

export function useGlobalWs() {
  if (!started) {
    started = true
    conn.connect()
  }
  refCount++

  onUnmounted(() => {
    refCount--
    if (refCount <= 0) {
      refCount = 0
      conn.disconnect()
      started = false
    }
  })

  // reconnect 暴露给 UI:断连提示可提供重新连接入口。
  return { systemStatus, connected: conn.connected, reconnect: conn.reconnect }
}
