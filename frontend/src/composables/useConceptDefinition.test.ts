import { defineComponent, toRefs } from 'vue'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import type { ConceptTermDetail } from '../types'

const api = {
  get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn(),
}
vi.mock('./useApi', () => ({ useApi: () => api }))

import { useConceptDefinition } from './useConceptDefinition'

function concept(term: string): ConceptTermDetail {
  const definitionVersionId = `cdv-${term}`
  return {
    domain: 'ml', term, definition: `${term} definition`, zh_name: '', aliases: [], occurrences: [],
    occurrence_total: 0, occurrence_limit: 100, related: [], status: 'accepted', watched: false,
    is_topic: false, definition_locked: false, current_definition_version_id: definitionVersionId,
    lock_revision: 0, created_at: null, updated_at: null,
    current_definition: {
      definition_version_id: definitionVersionId, domain: 'ml', term, version: 1,
      definition: `${term} definition`, source_evidence_ids: [], source_set_fingerprint: '',
      strategy: 'manual_edit', provider: null, model: null, prompt_hash: null, input_hash: null,
      supersedes_version_id: null, actor: 'test', created_at: '2026-07-15T00:00:00Z',
    },
    definition_history: [], definition_history_total: 1, definition_history_limit: 100,
    attestation: {
      domain: 'ml', term, level: 'none', evidence_count: 0, job_count: 0,
      source_fingerprint_count: 0, content_type_count: 0, source_set_fingerprint: '',
      included: [], excluded: [],
    },
  }
}

const Harness = defineComponent({
  props: { domain: { type: String, required: true }, term: { type: String, required: true } },
  setup(props) {
    const refs = toRefs(props)
    const controller = useConceptDefinition(refs.domain, refs.term)
    return { controller }
  },
  template: '<div data-test="term">{{ controller.detail.value?.term || "empty" }}</div>',
})

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useConceptDefinition', () => {
  it('路由切换会 abort 旧请求，且旧响应不能覆盖新概念', async () => {
    let resolveOld!: (value: ConceptTermDetail) => void
    let resolveNew!: (value: ConceptTermDetail) => void
    const oldPromise = new Promise<ConceptTermDetail>((resolve) => { resolveOld = resolve })
    const newPromise = new Promise<ConceptTermDetail>((resolve) => { resolveNew = resolve })
    api.get.mockImplementation((path: string) => path.endsWith('/old') ? oldPromise : newPromise)

    const wrapper = mount(Harness, { props: { domain: 'ml', term: 'old' } })
    await Promise.resolve()
    const oldSignal = api.get.mock.calls[0][1] as AbortSignal

    await wrapper.setProps({ term: 'new' })
    expect(oldSignal.aborted).toBe(true)
    expect(wrapper.get('[data-test="term"]').text()).toBe('empty')
    resolveNew(concept('new'))
    await flushPromises()
    expect(wrapper.get('[data-test="term"]').text()).toBe('new')

    resolveOld(concept('old'))
    await flushPromises()
    expect(wrapper.get('[data-test="term"]').text()).toBe('new')
    expect(api.get.mock.calls.map((call) => call[0])).toEqual([
      '/api/glossary/ml/old', '/api/glossary/ml/new',
    ])
  })
})
