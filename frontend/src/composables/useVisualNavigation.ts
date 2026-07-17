import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'

type VisualElement = HTMLElement | null

function reducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

export function useVisualNavigation(visualIds: ComputedRef<string[]>) {
  const route = useRoute()
  const router = useRouter()
  const activeVisualId = ref('')
  const elements = new Map<string, HTMLElement>()
  const visible = new Map<string, number>()
  let observer: IntersectionObserver | null = null
  let mounted = false

  const allowed = computed(() => new Set(visualIds.value))

  function syncQuery(id: string): void {
    const current = typeof route.query.visual === 'string' ? route.query.visual : ''
    if (current === id && route.query.tab === 'figures') return
    void router.replace({ query: { ...route.query, tab: 'figures', visual: id } })
  }

  function focusAndScroll(id: string, focus: boolean): boolean {
    if (!allowed.value.has(id)) return false
    const element = elements.get(id)
    if (!element) return false
    activeVisualId.value = id
    element.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' })
    if (focus) element.focus({ preventScroll: true })
    return true
  }

  async function selectVisual(id: string): Promise<void> {
    if (!allowed.value.has(id)) return
    activeVisualId.value = id
    syncQuery(id)
    await nextTick()
    focusAndScroll(id, true)
  }

  function updateFromVisible(): void {
    const candidates = [...visible.entries()]
      .filter(([id, ratio]) => allowed.value.has(id) && ratio > 0)
      .sort((left, right) => {
        const leftTop = elements.get(left[0])?.getBoundingClientRect().top ?? Number.MAX_SAFE_INTEGER
        const rightTop = elements.get(right[0])?.getBoundingClientRect().top ?? Number.MAX_SAFE_INTEGER
        return Math.abs(leftTop - 96) - Math.abs(rightTop - 96) || right[1] - left[1]
      })
    const id = candidates[0]?.[0]
    if (!id || id === activeVisualId.value) return
    activeVisualId.value = id
    syncQuery(id)
  }

  function ensureObserver(): void {
    if (observer || typeof IntersectionObserver === 'undefined') return
    observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const id = (entry.target as HTMLElement).dataset.visualId
        if (id) visible.set(id, entry.isIntersecting ? entry.intersectionRatio : 0)
      }
      updateFromVisible()
    }, { rootMargin: '-96px 0px -65% 0px', threshold: [0, 0.1, 0.5, 1] })
    for (const element of elements.values()) observer.observe(element)
  }

  function registerVisual(id: string, element: VisualElement): void {
    const previous = elements.get(id)
    if (previous && previous !== element) observer?.unobserve(previous)
    if (!element) {
      elements.delete(id)
      visible.delete(id)
      return
    }
    element.dataset.visualId = id
    elements.set(id, element)
    ensureObserver()
    observer?.observe(element)
    if (mounted && route.query.tab === 'figures' && route.query.visual === id) {
      void nextTick(() => focusAndScroll(id, false))
    }
  }

  async function restoreRouteTarget(): Promise<void> {
    if (route.query.tab !== 'figures' || typeof route.query.visual !== 'string') return
    const id = route.query.visual
    if (!allowed.value.has(id)) return
    activeVisualId.value = id
    await nextTick()
    focusAndScroll(id, false)
  }

  watch(
    [visualIds, () => route.query.tab, () => route.query.visual],
    () => { void restoreRouteTarget() },
    { flush: 'post' },
  )

  onMounted(() => {
    mounted = true
    ensureObserver()
    void restoreRouteTarget()
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    observer = null
    elements.clear()
    visible.clear()
  })

  return { activeVisualId, registerVisual, selectVisual }
}
