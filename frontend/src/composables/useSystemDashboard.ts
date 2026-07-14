import { computed, inject, onMounted, onUnmounted, ref } from 'vue'
import { useWorkerStore } from '../stores/workers'
import { useGlobalWs } from './useGlobalWs'
import type { FullStatus, PricingStatus, SystemEvent, UsageAggregate } from '../types'

// 系统页所有 HTTP 快照由一个轮询器拥有,避免子 panel 重复请求和轮询重叠。
export function useSystemDashboard() {
  const workerStore = useWorkerStore()
  const showToast = inject<(message: string, type?: 'success' | 'error' | 'info') => void>('showToast', () => {})
  const ws = useGlobalWs()
  const status = ref<FullStatus | null>(null)
  const failStreak = ref(0)
  const usage = ref<UsageAggregate | null>(null)
  const events = ref<SystemEvent[]>([])
  const pricing = ref<PricingStatus | null>(null)
  const pricingBusy = ref(false)
  const poolLimits = ref<Record<string, { default: number; override: number | null }>>({})
  const limitDraft = ref<Record<string, number | null>>({})
  const history = ref<Array<{ ts: number; gw?: any; tun?: any; t?: any; w?: any }>>([])
  let pollTimer: number | undefined
  let refreshPromise: Promise<void> | null = null
  let disposed = false

  async function loadStatus() {
    try {
      const next = await workerStore.fetchFullStatus()
      if (!disposed) { status.value = next; failStreak.value = 0 }
    } catch {
      if (!disposed) {
        failStreak.value++
        status.value = null
      }
    }
  }

  async function loadUsage() {
    try { const next = await workerStore.fetchUsage(); if (!disposed) usage.value = next } catch { /* 非致命 */ }
  }

  async function loadEvents() {
    try { const next = await workerStore.fetchEvents(50); if (!disposed) events.value = next.events } catch { /* 非致命 */ }
  }

  async function loadPricing() {
    try { const next = await workerStore.fetchPricing(); if (!disposed) pricing.value = next } catch { /* 非致命 */ }
  }

  async function loadPoolLimits() {
    try {
      const next = await workerStore.fetchPoolLimits()
      if (disposed) return
      poolLimits.value = next
      limitDraft.value = Object.fromEntries(Object.entries(next).map(([key, value]) => [key, value.override ?? value.default]))
    } catch { /* 非致命 */ }
  }

  async function loadHistory() {
    try { const next = await workerStore.fetchLinkTrafficHistory(); if (!disposed) history.value = next } catch { if (!disposed) history.value = [] }
  }

  function refreshAll() {
    if (refreshPromise) return refreshPromise
    refreshPromise = Promise.all([
      loadStatus(), workerStore.fetchAll(), loadPoolLimits(), loadUsage(), loadEvents(), loadPricing(), loadHistory(),
    ]).then(() => undefined).finally(() => { refreshPromise = null })
    return refreshPromise
  }

  function refreshSlowSnapshot() {
    if (refreshPromise) return refreshPromise
    refreshPromise = Promise.all([loadStatus(), loadUsage(), loadEvents(), loadPricing()])
      .then(() => undefined).finally(() => { refreshPromise = null })
    return refreshPromise
  }

  async function refreshPricing() {
    if (pricingBusy.value) return
    pricingBusy.value = true
    try {
      const next = await workerStore.refreshPricing()
      if (!disposed) pricing.value = next
      showToast('价表已更新', 'success')
    } catch {
      showToast('价表更新失败(网络/上游异常),已保留旧表', 'error')
    } finally {
      pricingBusy.value = false
    }
  }

  onMounted(() => {
    void refreshAll()
    pollTimer = window.setInterval(() => { void refreshSlowSnapshot() }, 15000)
  })
  onUnmounted(() => {
    disposed = true
    if (pollTimer) window.clearInterval(pollTimer)
  })

  return {
    workerStore,
    status,
    failStreak,
    usage,
    events,
    pricing,
    pricingBusy,
    poolLimits,
    limitDraft,
    history,
    systemStatus: ws.systemStatus,
    connected: ws.connected,
    reconnect: ws.reconnect,
    refreshAll,
    loadStatus,
    loadPoolLimits,
    refreshPricing,
    statusFetchFailed: computed(() => failStreak.value > 0 && status.value === null),
  }
}
