import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

const get = vi.fn()
const post = vi.fn()
const del = vi.fn()
vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get, post, del, put: vi.fn(), upload: vi.fn(), getText: vi.fn() }),
}))

import StudyView from './StudyView.vue'

function card(over: Record<string, any> = {}) {
  return {
    card_id: 'sc_1',
    domain: 'ml',
    job_id: 'j_1',
    concept_term: '反向传播',
    card_type: 'basic',
    front: '反向传播解决什么问题?',
    back: '高效计算梯度。',
    explanation: '链式法则让多层网络可训练。',
    evidence: [{ snippet: '反向传播算法通过链式法则计算梯度' }],
    status: 'active',
    source: 'manual',
    created_at: '2026-07-09T00:00:00+00:00',
    updated_at: '2026-07-09T00:00:00+00:00',
    review: {
      due_at: '2026-07-09T00:00:00+00:00',
      interval_days: 0,
      ease: 2.5,
      repetitions: 0,
      lapses: 0,
      last_grade: null,
      last_reviewed_at: null,
      updated_at: '2026-07-09T00:00:00+00:00',
    },
    ...over,
  }
}

async function mountView() {
  get.mockImplementation((path: string) => {
    if (path.startsWith('/api/study/due')) return Promise.resolve({ total: 1, items: [card()] })
    if (path.startsWith('/api/study/cards')) return Promise.resolve({ total: 1, items: [card()] })
    return Promise.resolve({})
  })
  const wrapper = mount(StudyView, {
    global: { provide: { showToast: vi.fn() } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('StudyView', () => {
  it('加载今日复习和卡片库', async () => {
    const w = await mountView()
    expect(w.text()).toContain('学习')
    expect(w.text()).toContain('反向传播解决什么问题?')
    expect(w.text()).toContain('卡片库')
    expect(get).toHaveBeenCalledWith('/api/study/due?limit=50')
    expect(get).toHaveBeenCalledWith('/api/study/cards?limit=100')
  })

  it('显示答案后提交评分', async () => {
    post.mockResolvedValue(card({ review: { ...card().review, last_grade: 'good', repetitions: 1 } }))
    const w = await mountView()
    await w.find('button.reveal').trigger('click')
    expect(w.text()).toContain('高效计算梯度')
    const good = w.findAll('button.grade').find((b) => b.text().includes('掌握'))
    expect(good).toBeTruthy()
    await good!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/study/reviews', { card_id: 'sc_1', grade: 'good' })
  })

  it('创建新卡片后刷新列表', async () => {
    post.mockResolvedValue(card({ card_id: 'sc_new', front: 'Q', back: 'A' }))
    const w = await mountView()
    const inputs = w.findAll('input.input')
    await inputs[1].setValue('新概念')
    const textareas = w.findAll('textarea')
    await textareas[0].setValue('问题')
    await textareas[1].setValue('答案')
    await w.find('form.card-form').trigger('submit')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/study/cards', expect.objectContaining({
      domain: 'general',
      front: '问题',
      back: '答案',
      concept_term: '新概念',
      status: 'active',
    }))
    expect(get).toHaveBeenCalledTimes(4)
  })
})
