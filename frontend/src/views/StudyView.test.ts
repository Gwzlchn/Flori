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
    revision: 1,
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
    if (path.startsWith('/api/study/stats')) return Promise.resolve({
      total: 251,
      statuses: { suggested: 0, active: 203, suspended: 48, rejected: 0 },
      due: 203,
      reviewed_cards: 20,
      reviews_total: 24,
      grades: { again: 3, hard: 4, good: 10, easy: 7 },
      retained_reviews: 21,
      retention_rate: 0.875,
    })
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
    expect(get).toHaveBeenCalledWith('/api/study/stats')
    expect(w.text()).toContain('待复习 203')
    expect(w.text()).toContain('卡片 251')
    expect(w.text()).toContain('留存 88%')
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
    expect(post).toHaveBeenCalledWith('/api/study/reviews', {
      request_id: expect.stringMatching(/^study-review:/),
      card_id: 'sc_1',
      expected_revision: 1,
      grade: 'good',
    })
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
    expect(get).toHaveBeenCalledTimes(6)
  })

  it('模糊失败后重试复用 request_id', async () => {
    post.mockRejectedValueOnce(new Error('network reset')).mockResolvedValueOnce(card({ revision: 2 }))
    const w = await mountView()
    await w.find('button.reveal').trigger('click')
    const good = w.findAll('button.grade').find((b) => b.text().includes('掌握'))!
    await good.trigger('click')
    await flushPromises()
    await good.trigger('click')
    await flushPromises()
    const reviewCalls = post.mock.calls.filter(([path]) => path === '/api/study/reviews')
    expect(reviewCalls).toHaveLength(2)
    expect(reviewCalls[0][1].request_id).toBe(reviewCalls[1][1].request_id)
  })

  it('只允许 suspended 恢复,suggested 只显示驳回', async () => {
    get.mockImplementation((path: string) => {
      if (path.startsWith('/api/study/due')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/cards')) return Promise.resolve({
        total: 3,
        items: [
          card({ card_id: 'suspended', status: 'suspended', revision: 2 }),
          card({ card_id: 'suggested', status: 'suggested', revision: 4 }),
          card({ card_id: 'rejected', status: 'rejected', revision: 5 }),
        ],
      })
      return Promise.resolve({
        total: 3,
        statuses: { suggested: 1, active: 0, suspended: 1, rejected: 1 },
        due: 0,
        reviewed_cards: 0,
        reviews_total: 0,
        grades: { again: 0, hard: 0, good: 0, easy: 0 },
        retained_reviews: 0,
        retention_rate: 0,
      })
    })
    const w = mount(StudyView, { global: { provide: { showToast: vi.fn() } } })
    await flushPromises()
    expect(w.findAll('button[title="恢复"]')).toHaveLength(1)
    expect(w.findAll('button[title="驳回"]')).toHaveLength(1)
    await w.find('button[title="驳回"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/study/cards/suggested/status', {
      status: 'rejected',
      expected_revision: 4,
    })
  })
})
