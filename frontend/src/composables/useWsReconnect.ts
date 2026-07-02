import { ref } from 'vue'
import { getAuthToken } from './useApi'

// WS 连接 + 指数退避重连 + 清理的通用脚手架。global / job 两个端点共用,
// 仅注入 url 工厂与是否续连的判定,不合并端点本身。
export interface WsReconnectOptions {
  url: () => string | null          // 返回 WS 路径(如 /api/ws/global);null/空 → 不连接
  onMessage: (data: string) => void
  shouldReconnect: () => boolean    // onclose 后是否重连
  maxRetries?: number               // 连续重连上限,到顶停(默认 Infinity)
  baseDelay?: number                // 初始退避(默认 1000ms)
  maxDelay?: number                 // 退避上限(默认 30000ms)
  withToken?: boolean               // 拼 ?token=(契约要求 WS 经 query 传 token)
}

export function createWsReconnect(opts: WsReconnectOptions) {
  const connected = ref(false)
  let ws: WebSocket | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let delay = opts.baseDelay ?? 1000
  let retries = 0
  const maxRetries = opts.maxRetries ?? Infinity

  function clearTimer() {
    if (timer) { clearTimeout(timer); timer = null }
  }

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
    const path = opts.url()
    if (!path) return
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    let url = `${protocol}//${location.host}${path}`
    if (opts.withToken) {
      const token = getAuthToken()
      if (token) url += `${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
    }
    ws = new WebSocket(url)
    ws.onopen = () => {
      connected.value = true
      delay = opts.baseDelay ?? 1000
      retries = 0
    }
    ws.onmessage = (e) => opts.onMessage(e.data)
    ws.onclose = () => {
      connected.value = false
      if (retries < maxRetries && opts.shouldReconnect()) {
        clearTimer()
        timer = setTimeout(connect, delay)
        delay = Math.min(delay * 2, opts.maxDelay ?? 30000)
        retries++
      }
    }
    ws.onerror = () => ws?.close()
  }

  function disconnect() {
    clearTimer()
    ws?.close()
    ws = null
  }

  // 手动重连:重置退避与计数后立即连。用于 online/visibilitychange 或达上限后恢复。
  function reconnect() {
    retries = 0
    delay = opts.baseDelay ?? 1000
    if (ws && ws.readyState === WebSocket.OPEN) return
    disconnect()
    connect()
  }

  return { connected, connect, disconnect, reconnect }
}
