import { defineComponent, ref, toRefs } from 'vue'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import type {
  ConceptDefinitionVersion,
  ConceptEvidence,
  ConceptTermDetail,
} from '../../types'

const api = {
  get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn(),
}
vi.mock('../../composables/useApi', () => ({ useApi: () => api }))

import ConceptDefinitionPanel from './ConceptDefinitionPanel.vue'
import { useConceptDefinition } from '../../composables/useConceptDefinition'

const validEvidenceId = `ce_${'1'.repeat(64)}`
const unsafeEvidenceId = `ce_${'2'.repeat(64)}`
const staleEvidenceId = `ce_${'3'.repeat(64)}`

function version(over: Partial<ConceptDefinitionVersion> = {}): ConceptDefinitionVersion {
  return {
    definition_version_id: 'cdv-current',
    domain: 'ml',
    term: 'gradient',
    version: 2,
    definition: '梯度是函数的偏导数向量',
    source_evidence_ids: [validEvidenceId],
    source_set_fingerprint: 'a'.repeat(64),
    strategy: 'automatic_resynthesis',
    provider: 'test-provider',
    model: 'test-model',
    prompt_hash: 'b'.repeat(64),
    input_hash: 'c'.repeat(64),
    supersedes_version_id: 'cdv-old',
    actor: 'scheduler:auto',
    created_at: '2026-07-15T00:00:00Z',
    ...over,
  }
}

function evidence(over: Partial<ConceptEvidence> = {}): ConceptEvidence {
  return {
    evidence_id: validEvidenceId,
    job_id: 'job-paper',
    content_type: 'document',
    document_kind: 'research_paper',
    source_fingerprint: 'd'.repeat(64),
    note_type: 'smart',
    chunk_id: 'job-paper:smart:0',
    section: '方法',
    excerpt: '梯度给出了函数增长最快的方向。',
    reason: null,
    locator: { kind: 'pdf', page: 3, bbox: null },
    link: { kind: 'pdf', href: `/api/evidence/${validEvidenceId}/open`, label: '第 3 页' },
    ...over,
  }
}

function detail(over: Partial<ConceptTermDetail> = {}): ConceptTermDetail {
  const current = over.current_definition ?? version()
  return {
    domain: 'ml',
    term: 'gradient',
    definition: current.definition,
    zh_name: '梯度',
    aliases: [],
    occurrences: [
      { job_id: 'job-paper', content_type: 'document', document_kind: 'research_paper', location: 'p.3', title: '论文' },
      { job_id: 'job-video', content_type: 'video', location: '03:20', title: '视频' },
    ],
    occurrence_total: 12,
    occurrence_limit: 100,
    related: [{ term: 'backprop', rel: 'related' }],
    status: 'accepted',
    watched: false,
    is_topic: false,
    definition_locked: false,
    current_definition_version_id: current.definition_version_id,
    lock_revision: 4,
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-15T00:00:00Z',
    current_definition: current,
    definition_history: [current, version({
      definition_version_id: 'cdv-old', version: 1, definition: '旧定义',
      supersedes_version_id: null, strategy: 'manual_edit', source_evidence_ids: [],
    })],
    definition_history_total: 2,
    definition_history_limit: 100,
    attestation: {
      domain: 'ml',
      term: 'gradient',
      level: 'corroborated',
      evidence_count: 2,
      job_count: 2,
      source_fingerprint_count: 2,
      content_type_count: 2,
      source_set_fingerprint: 'e'.repeat(64),
      included: [
        evidence(),
        evidence({
          evidence_id: unsafeEvidenceId,
          job_id: 'job-video',
          content_type: 'video',
          locator: { kind: 'media', start_ms: 200000, end_ms: 205000 },
          link: { kind: 'media', href: 'https://unsafe.example/evidence', label: '03:20' },
        }),
      ],
      excluded: [evidence({
        evidence_id: staleEvidenceId,
        reason: 'source_changed',
        locator: null,
        link: null,
      })],
    },
    ...over,
  }
}

function detailFor(term: string, definition: string): ConceptTermDetail {
  const current = version({
    definition_version_id: `cdv-${term}`,
    term,
    definition,
    supersedes_version_id: null,
  })
  const value = detail({
    term,
    definition,
    current_definition_version_id: current.definition_version_id,
    current_definition: current,
    definition_history: [current],
    definition_history_total: 1,
  })
  value.attestation = { ...value.attestation, term }
  return value
}

const Harness = defineComponent({
  components: { ConceptDefinitionPanel },
  setup() {
    const domain = ref('ml')
    const term = ref('gradient')
    const controller = useConceptDefinition(domain, term)
    return { controller }
  },
  template: '<ConceptDefinitionPanel :controller="controller" />',
})

const IdentityHarness = defineComponent({
  components: { ConceptDefinitionPanel },
  props: {
    domain: { type: String, required: true },
    term: { type: String, required: true },
  },
  setup(props) {
    const refs = toRefs(props)
    const controller = useConceptDefinition(refs.domain, refs.term)
    return { controller }
  },
  template: '<ConceptDefinitionPanel :controller="controller" />',
})

async function mountPanel() {
  const wrapper = mount(Harness)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ConceptDefinitionPanel', () => {
  it('展示当前版本、历史、佐证和真实出现总数，并只链接安全有效证据', async () => {
    api.get.mockResolvedValue(detail())
    const wrapper = await mountPanel()

    const text = wrapper.text()
    expect(text).toContain('梯度是函数的偏导数向量')
    expect(text).toContain('当前 v2')
    expect(text).toContain('历史 2 版')
    expect(text).toContain('出现 12 处')
    expect(text).toContain('多源互证')
    expect(text).toContain('2 条证据')
    expect(wrapper.findAll('a.evidence-locator')).toHaveLength(1)
    expect(wrapper.get('a.evidence-locator').attributes('href')).toBe(`/api/evidence/${validEvidenceId}/open`)
    expect(wrapper.html()).not.toContain('https://unsafe.example')
  })

  it('人工编辑携带双 CAS，409 后明确提示并重载最新版本', async () => {
    const refreshed = detail({
      current_definition: version({
        definition_version_id: 'cdv-concurrent', version: 3, definition: '其他操作写入的新定义',
      }),
      current_definition_version_id: 'cdv-concurrent',
      lock_revision: 5,
    })
    api.get.mockResolvedValueOnce(detail()).mockResolvedValueOnce(refreshed)
    api.put.mockRejectedValue({ status: 409, message: 'API 409: concept changed' })
    const wrapper = await mountPanel()

    await wrapper.get('[data-test="definition-edit"]').trigger('click')
    await wrapper.get('[data-test="definition-input"]').setValue('我的定义')
    await wrapper.get('[data-test="definition-save"]').trigger('click')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith('/api/glossary/ml/gradient', {
      term: 'gradient',
      definition: '我的定义',
      expected_current_version_id: 'cdv-current',
      expected_lock_revision: 4,
    })
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已重新加载最新版本')
    expect(wrapper.text()).toContain('其他操作写入的新定义')
    expect(wrapper.find('[data-test="definition-input"]').exists()).toBe(false)
  })

  it('锁定使用双 CAS，刷新后禁用编辑与重综合，并可用新 revision 解锁', async () => {
    const locked = detail({ definition_locked: true, lock_revision: 5 })
    api.get
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce(locked)
      .mockResolvedValueOnce(detail({ lock_revision: 6 }))
    api.post.mockResolvedValue({ changed: true })
    const wrapper = await mountPanel()

    await wrapper.get('[data-test="definition-lock"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenNthCalledWith(1, '/api/glossary/ml/gradient/lock', {
      expected_current_version_id: 'cdv-current', expected_lock_revision: 4,
    })
    expect(wrapper.get('[data-test="definition-edit"]').attributes()).toHaveProperty('disabled')
    expect(wrapper.get('[data-test="definition-resynthesize"]').attributes()).toHaveProperty('disabled')
    expect(wrapper.get('[data-test="definition-lock"]').text()).toContain('解锁')

    await wrapper.get('[data-test="definition-lock"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenNthCalledWith(2, '/api/glossary/ml/gradient/unlock', {
      expected_current_version_id: 'cdv-current', expected_lock_revision: 5,
    })
  })

  it('重综合 provider 失败可恢复，保留当前定义并重新启用按钮', async () => {
    api.get.mockResolvedValue(detail())
    api.post.mockRejectedValue({ status: 502, message: 'API 502: concept synthesis failed' })
    const wrapper = await mountPanel()

    await wrapper.get('[data-test="definition-resynthesize"]').trigger('click')
    await flushPromises()

    expect(api.post).toHaveBeenCalledWith('/api/glossary/ml/gradient/resynthesize', {
      expected_current_version_id: 'cdv-current', expected_lock_revision: 4,
    })
    expect(wrapper.text()).toContain('定义重综合失败，请稍后重试')
    expect(wrapper.text()).toContain('梯度是函数的偏导数向量')
    expect(wrapper.get('[data-test="definition-resynthesize"]').attributes('disabled')).toBeUndefined()
  })

  it('概念身份切换立即清详情、操作态与旧草稿，新 CAS 不可保存旧草稿', async () => {
    let resolveNew!: (value: ConceptTermDetail) => void
    const newRequest = new Promise<ConceptTermDetail>((resolve) => { resolveNew = resolve })
    api.get.mockImplementation((path: string) => (
      path.endsWith('/old') ? Promise.resolve(detailFor('old', 'old definition')) : newRequest
    ))
    api.post.mockRejectedValue({ status: 502, message: 'API 502: concept synthesis failed' })
    const wrapper = mount(IdentityHarness, { props: { domain: 'ml', term: 'old' } })
    await flushPromises()

    await wrapper.get('[data-test="definition-resynthesize"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="definition-error"]').exists()).toBe(true)
    await wrapper.get('[data-test="definition-edit"]').trigger('click')
    await wrapper.get('[data-test="definition-input"]').setValue('old unsaved draft')

    await wrapper.setProps({ term: 'new' })
    expect(wrapper.find('[data-test="definition-input"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="definition-error"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('old definition')

    resolveNew(detailFor('new', 'new definition'))
    await flushPromises()
    expect(wrapper.text()).toContain('new definition')
    expect(wrapper.find('[data-test="definition-input"]').exists()).toBe(false)
    expect(api.put).not.toHaveBeenCalled()

    await wrapper.get('[data-test="definition-edit"]').trigger('click')
    expect((wrapper.get('[data-test="definition-input"]').element as HTMLTextAreaElement).value)
      .toBe('new definition')
  })
})
