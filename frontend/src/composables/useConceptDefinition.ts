import { onScopeDispose, ref, unref, watch, type MaybeRef } from 'vue'
import { useApi } from './useApi'
import type {
  ConceptCasRequest,
  ConceptResynthesizeResponse,
  ConceptTermDetail,
  GlossaryTerm,
} from '../types'

export interface ConceptDefinitionOptions {
  enabled?: MaybeRef<boolean>
}

function statusOf(error: unknown): number | null {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const status = Number((error as { status?: unknown }).status)
    if (Number.isInteger(status)) return status
  }
  const match = String((error as { message?: unknown })?.message ?? error).match(/API\s+(\d{3})/)
  return match ? Number(match[1]) : null
}

function messageOf(error: unknown): string {
  return String((error as { message?: unknown })?.message ?? '') || '加载失败'
}

function isAbort(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error
    && (error as { name?: unknown }).name === 'AbortError'
}

export function useConceptDefinition(
  domain: MaybeRef<string>,
  term: MaybeRef<string>,
  options: ConceptDefinitionOptions = {},
) {
  const api = useApi()
  const detail = ref<ConceptTermDetail | null>(null)
  const loading = ref(false)
  const notFound = ref(false)
  const error = ref('')
  const actionBusy = ref(false)
  const actionError = ref('')
  const actionMessage = ref('')

  let alive = true
  let loadGeneration = 0
  let mutationGeneration = 0
  let loadAbort: AbortController | null = null

  const identityKey = (d: string, t: string) => `${d.trim()}\u0000${t.trim()}`
  const currentKey = () => identityKey(unref(domain), unref(term))
  const isEnabled = () => options.enabled === undefined || Boolean(unref(options.enabled))
  const detailPath = (d: string, t: string) => (
    `/api/glossary/${encodeURIComponent(d)}/${encodeURIComponent(t)}`
  )

  async function load(): Promise<ConceptTermDetail | null> {
    const generation = ++loadGeneration
    loadAbort?.abort()
    loadAbort = null

    const d = unref(domain).trim()
    const t = unref(term).trim()
    if (!alive || !isEnabled() || !d || !t) {
      detail.value = null
      loading.value = false
      notFound.value = false
      error.value = ''
      return null
    }

    const abort = new AbortController()
    loadAbort = abort
    loading.value = true
    notFound.value = false
    error.value = ''
    try {
      const loaded = await api.get<ConceptTermDetail>(detailPath(d, t), abort.signal)
      if (!alive || generation !== loadGeneration) return null
      detail.value = loaded
      return loaded
    } catch (caught) {
      if (!alive || generation !== loadGeneration || isAbort(caught)) return null
      detail.value = null
      if (statusOf(caught) === 404) notFound.value = true
      else error.value = messageOf(caught)
      return null
    } finally {
      if (generation === loadGeneration) {
        loading.value = false
        if (loadAbort === abort) loadAbort = null
      }
    }
  }

  function casFor(snapshot: ConceptTermDetail): ConceptCasRequest {
    return {
      expected_current_version_id: snapshot.current_definition.definition_version_id,
      expected_lock_revision: snapshot.lock_revision,
    }
  }

  async function mutate<T>(
    operation: (snapshot: ConceptTermDetail) => Promise<T>,
    successMessage: (result: T) => string,
  ): Promise<boolean> {
    const snapshot = detail.value
    if (!snapshot || actionBusy.value) return false
    const key = currentKey()
    if (identityKey(snapshot.domain, snapshot.term) !== key) return false
    const mutation = ++mutationGeneration
    actionBusy.value = true
    actionError.value = ''
    actionMessage.value = ''
    try {
      const result = await operation(snapshot)
      if (!alive || currentKey() !== key) return false
      await load()
      if (!alive || currentKey() !== key) return false
      actionMessage.value = successMessage(result)
      return true
    } catch (caught) {
      if (!alive || currentKey() !== key) return false
      const status = statusOf(caught)
      if (status === 409) {
        await load()
        if (alive && currentKey() === key) {
          actionError.value = '概念已被其他操作更新，已重新加载最新版本，请确认后重试。'
        }
      } else if (status === 502) {
        actionError.value = '定义重综合失败，请稍后重试。'
      } else {
        actionError.value = messageOf(caught) || '操作失败，请重试。'
      }
      return false
    } finally {
      if (mutation === mutationGeneration) actionBusy.value = false
    }
  }

  async function saveDefinition(definition: string): Promise<boolean> {
    return mutate(
      (snapshot) => api.put<GlossaryTerm>(detailPath(snapshot.domain, snapshot.term), {
        term: snapshot.term,
        definition: definition.trim(),
        ...casFor(snapshot),
      }),
      () => '定义已保存为新版本。',
    )
  }

  async function setLocked(locked: boolean): Promise<boolean> {
    return mutate(
      (snapshot) => api.post(
        `${detailPath(snapshot.domain, snapshot.term)}/${locked ? 'lock' : 'unlock'}`,
        casFor(snapshot),
      ),
      () => locked ? '定义已锁定。' : '定义已解锁。',
    )
  }

  async function resynthesize(): Promise<boolean> {
    return mutate(
      (snapshot) => api.post<ConceptResynthesizeResponse>(
        `${detailPath(snapshot.domain, snapshot.term)}/resynthesize`,
        casFor(snapshot),
      ),
      (result) => {
        if (result.created) return '已生成新的证据定义版本。'
        const reasons = {
          locked: '定义已锁定，未执行重综合。',
          no_quorum: '可靠来源不足，未执行重综合。',
          source_set_unchanged: '佐证集合未变化，无需生成新版本。',
          input_too_large: '可靠佐证超过单次综合上限。',
        }
        return result.reason ? reasons[result.reason] : '未生成新定义版本。'
      },
    )
  }

  function clearActionNotice() {
    actionError.value = ''
    actionMessage.value = ''
  }

  watch(
    [() => unref(domain), () => unref(term), isEnabled],
    () => {
      mutationGeneration += 1
      detail.value = null
      loading.value = false
      notFound.value = false
      error.value = ''
      actionBusy.value = false
      clearActionNotice()
      void load()
    },
    { immediate: true, flush: 'sync' },
  )

  onScopeDispose(() => {
    alive = false
    loadGeneration += 1
    mutationGeneration += 1
    actionBusy.value = false
    loadAbort?.abort()
    loadAbort = null
  })

  return {
    detail,
    loading,
    notFound,
    error,
    actionBusy,
    actionError,
    actionMessage,
    load,
    saveDefinition,
    setLocked,
    resynthesize,
    clearActionNotice,
  }
}

export type ConceptDefinitionController = ReturnType<typeof useConceptDefinition>
