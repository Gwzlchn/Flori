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

function suggestion(over: Record<string, any> = {}) {
  return {
    suggestion_id: 'ss_1',
    batch_id: 'sb_1',
    ordinal: 0,
    status: 'suggested',
    revision: 1,
    domain: 'ml',
    concept_term: '反向传播',
    knowledge_key: 'backpropagation',
    card_type: 'basic',
    front: '反向传播如何计算梯度?',
    back: '沿计算图反向应用链式法则。',
    explanation: '每个局部导数只计算一次。',
    accepted_card_id: null,
    rejection_reason: null,
    evidence: [{
      evidence_id: 'se_1',
      job_id: 'j_1',
      chunk_id: 'chunk_1',
      note_type: 'note',
      source_domain: 'ml',
      current_domain: 'ml',
      title: '神经网络',
      section: '反向传播',
      quote: '反向传播沿计算图反向应用链式法则。',
      quote_sha256: 'a'.repeat(64),
      body_sha256: 'b'.repeat(64),
      locator: { page: 3 },
      status: 'valid',
      invalid_reason: null,
    }],
    created_at: '2026-07-14T00:00:00+00:00',
    updated_at: '2026-07-14T00:00:00+00:00',
    ...over,
  }
}

function batch(over: Record<string, any> = {}) {
  return {
    batch_id: 'sb_1',
    domain: 'general',
    status: 'queued',
    revision: 2,
    attempt: 1,
    task_id: 'at_1',
    provider: 'claude-cli',
    model: 'default',
    max_cards: 10,
    error_code: null,
    error_message: null,
    deadline_at: '2026-07-14T01:00:00+00:00',
    evidence_count: 3,
    suggestion_count: 0,
    created_at: '2026-07-14T00:00:00+00:00',
    updated_at: '2026-07-14T00:00:00+00:00',
    ...over,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function mountView(
  batchLoader?: (path: string) => Promise<any>,
  dueLoader?: (path: string) => Promise<any>,
) {
  get.mockImplementation((path: string) => {
    if (path.startsWith('/api/study/due')) {
      return dueLoader ? dueLoader(path) : Promise.resolve({ total: 1, items: [card()] })
    }
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
    if (path.startsWith('/api/study/suggestions')) return Promise.resolve({ total: 0, items: [] })
    if (path.startsWith('/api/study/mastery')) return Promise.resolve({
      total: 1,
      items: [{
        domain: 'ml',
        concept_term: '反向传播',
        score: 80,
        level: 'learning',
        reviewed_cards: 1,
        reviews_total: 2,
        last_reviewed_at: '2026-07-14T00:00:00+00:00',
      }],
    })
    if (path.startsWith('/api/study/suggestion-batches/')) {
      return batchLoader ? batchLoader(path) : Promise.resolve(batch())
    }
    return Promise.resolve({})
  })
  const wrapper = mount(StudyView, {
    global: { provide: { showToast: vi.fn() } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.useRealTimers()
  get.mockReset()
  post.mockReset()
  del.mockReset()
  push.mockReset()
  localStorage.clear()
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
    expect(get).toHaveBeenCalledWith('/api/study/suggestions?status=suggested&limit=100')
    expect(get).toHaveBeenCalledWith('/api/study/mastery')
    expect(w.text()).toContain('待复习 203')
    expect(w.text()).toContain('卡片 251')
    expect(w.text()).toContain('留存 88%')
    expect(w.text()).toContain('概念掌握度')
    expect(w.text()).toContain('learning')
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
    expect(get).toHaveBeenCalledTimes(8)
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

  it.each([408, 429, 500, 502, 503, 504])(
    'HTTP %s 可能已提交,重试复用 request_id',
    async (status) => {
      const rejected: any = new Error(`HTTP ${status}`)
      rejected.status = status
      post.mockRejectedValueOnce(rejected).mockResolvedValueOnce(card({ revision: 2 }))
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
    },
  )

  it.each([400, 401, 403, 404])(
    '确定性 HTTP %s 后重试释放 request_id',
    async (status) => {
      const rejected: any = new Error(`HTTP ${status}`)
      rejected.status = status
      post.mockRejectedValueOnce(rejected).mockResolvedValueOnce(card({ revision: 2 }))
      const w = await mountView()
      await w.find('button.reveal').trigger('click')
      const good = w.findAll('button.grade').find((b) => b.text().includes('掌握'))!
      await good.trigger('click')
      await flushPromises()
      await good.trigger('click')
      await flushPromises()

      const reviewCalls = post.mock.calls.filter(([path]) => path === '/api/study/reviews')
      expect(reviewCalls).toHaveLength(2)
      expect(reviewCalls[0][1].request_id).not.toBe(reviewCalls[1][1].request_id)
    },
  )

  it('确定性失败后重试释放 request_id', async () => {
    const rejected: any = new Error('invalid grade')
    rejected.status = 422
    post.mockRejectedValueOnce(rejected).mockResolvedValueOnce(card({ revision: 2 }))
    const w = await mountView()
    await w.find('button.reveal').trigger('click')
    const good = w.findAll('button.grade').find((b) => b.text().includes('掌握'))!
    await good.trigger('click')
    await flushPromises()
    await good.trigger('click')
    await flushPromises()

    const reviewCalls = post.mock.calls.filter(([path]) => path === '/api/study/reviews')
    expect(reviewCalls).toHaveLength(2)
    expect(reviewCalls[0][1].request_id).not.toBe(reviewCalls[1][1].request_id)
  })

  it('revision 冲突后刷新卡片并用新 revision 和 request_id 重试', async () => {
    const conflict: any = new Error('stale revision')
    conflict.status = 409
    post.mockRejectedValueOnce(conflict).mockResolvedValueOnce(card({ revision: 3 }))
    let dueLoads = 0
    const w = await mountView(undefined, async () => {
      dueLoads += 1
      return {
        total: 1,
        items: [card({ revision: dueLoads === 1 ? 1 : 2 })],
      }
    })
    await w.find('button.reveal').trigger('click')
    let good = w.findAll('button.grade').find((b) => b.text().includes('掌握'))!
    await good.trigger('click')
    await flushPromises()
    expect(dueLoads).toBe(2)

    await w.find('button.reveal').trigger('click')
    good = w.findAll('button.grade').find((b) => b.text().includes('掌握'))!
    await good.trigger('click')
    await flushPromises()

    const reviewCalls = post.mock.calls.filter(([path]) => path === '/api/study/reviews')
    expect(reviewCalls).toHaveLength(2)
    expect(reviewCalls[0][1].expected_revision).toBe(1)
    expect(reviewCalls[1][1].expected_revision).toBe(2)
    expect(reviewCalls[0][1].request_id).not.toBe(reviewCalls[1][1].request_id)
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

  it('创建生成批次并显示可恢复的排队状态', async () => {
    post.mockResolvedValue(batch({ batch_id: 'sb_new', task_id: 'at_new' }))
    const w = await mountView()

    await w.find('button.generate-suggestions').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/study/suggestion-batches', {
      request_id: expect.stringMatching(/^study-suggestion-batch:/),
      domain: 'general',
      max_cards: 10,
    })
    expect(localStorage.getItem('study_suggestion_batch_id')).toBe('sb_new')
    expect(w.text()).toContain('生成批次 queued')
    w.unmount()
  })

  it('生成批次模糊失败重试复用 request_id,输入变化则换新', async () => {
    post
      .mockRejectedValueOnce(new Error('network reset'))
      .mockRejectedValueOnce(new Error('network reset again'))
      .mockResolvedValueOnce(batch({ batch_id: 'sb_new' }))
    const w = await mountView()
    const generate = w.find('button.generate-suggestions')

    await generate.trigger('click')
    await flushPromises()
    await generate.trigger('click')
    await flushPromises()
    const firstCalls = post.mock.calls.filter(([path]) => path === '/api/study/suggestion-batches')
    expect(firstCalls).toHaveLength(2)
    expect(firstCalls[0][1].request_id).toBe(firstCalls[1][1].request_id)

    await w.find('input.suggestion-max').setValue(11)
    await generate.trigger('click')
    await flushPromises()
    const allCalls = post.mock.calls.filter(([path]) => path === '/api/study/suggestion-batches')
    expect(allCalls[2][1].request_id).not.toBe(allCalls[1][1].request_id)
    w.unmount()
  })

  it('重试失败批次的模糊失败复用 request_id', async () => {
    localStorage.setItem('study_suggestion_batch_id', 'sb_failed')
    post
      .mockRejectedValueOnce(new Error('network reset'))
      .mockResolvedValueOnce(batch({ batch_id: 'sb_failed', status: 'queued', revision: 4 }))
    const w = await mountView(() => Promise.resolve(batch({
      batch_id: 'sb_failed',
      status: 'failed',
      revision: 3,
      error_code: 'timeout',
      error_message: 'timed out',
    })))
    const retry = w.find('.batch-status button')

    await retry.trigger('click')
    await flushPromises()
    await retry.trigger('click')
    await flushPromises()
    const calls = post.mock.calls.filter(([path]) => String(path).endsWith('/retry'))
    expect(calls).toHaveLength(2)
    expect(calls[0][1].request_id).toBe(calls[1][1].request_id)
    w.unmount()
  })

  it('旧批次 in-flight 响应不会覆盖新批次', async () => {
    vi.useFakeTimers()
    localStorage.setItem('study_suggestion_batch_id', 'sb_old')
    const oldPoll = deferred<any>()
    post.mockResolvedValue(batch({ batch_id: 'sb_new', status: 'queued' }))
    const w = await mountView((path) => path.endsWith('/sb_old')
      ? oldPoll.promise
      : Promise.resolve(batch({ batch_id: 'sb_new', status: 'queued' })))

    await w.find('button.generate-suggestions').trigger('click')
    await flushPromises()
    expect((w.vm as any).currentBatch.batch_id).toBe('sb_new')
    oldPoll.resolve(batch({ batch_id: 'sb_old', status: 'failed' }))
    await flushPromises()

    expect((w.vm as any).currentBatch.batch_id).toBe('sb_new')
    expect(localStorage.getItem('study_suggestion_batch_id')).toBe('sb_new')
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    const polls = get.mock.calls.filter(([path]) => String(path).includes('suggestion-batches/'))
    expect(polls).toHaveLength(2)
    expect((w.vm as any).currentBatch.batch_id).toBe('sb_new')
    w.unmount()
  })

  it('卸载后 in-flight poll 不写状态也不重新定时', async () => {
    vi.useFakeTimers()
    localStorage.setItem('study_suggestion_batch_id', 'sb_old')
    const oldPoll = deferred<any>()
    const w = await mountView(() => oldPoll.promise)
    w.unmount()

    oldPoll.resolve(batch({ batch_id: 'sb_old', status: 'queued' }))
    await flushPromises()
    await vi.runAllTimersAsync()

    const pollCalls = get.mock.calls.filter(([path]) => String(path).includes('suggestion-batches/'))
    expect(pollCalls).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('成功 poll 清除之前的批次错误', async () => {
    vi.useFakeTimers()
    localStorage.setItem('study_suggestion_batch_id', 'sb_poll')
    let attempt = 0
    const w = await mountView(() => {
      attempt += 1
      return attempt === 1
        ? Promise.reject(new Error('temporary poll failure'))
        : Promise.resolve(batch({ batch_id: 'sb_poll', status: 'queued' }))
    })
    expect(w.text()).toContain('temporary poll failure')

    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()

    expect(w.text()).not.toContain('temporary poll failure')
    expect((w.vm as any).currentBatch.batch_id).toBe('sb_poll')
    w.unmount()
  })

  it('预览证据并保存候选编辑', async () => {
    get.mockImplementation((path: string) => {
      if (path.startsWith('/api/study/due')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/cards')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/stats')) return Promise.resolve({
        total: 0,
        statuses: { suggested: 0, active: 0, suspended: 0, rejected: 0 },
        due: 0,
        reviewed_cards: 0,
        reviews_total: 0,
        grades: { again: 0, hard: 0, good: 0, easy: 0 },
        retained_reviews: 0,
        retention_rate: 0,
      })
      if (path.startsWith('/api/study/suggestions')) return Promise.resolve({ total: 1, items: [suggestion()] })
      if (path.startsWith('/api/study/mastery')) return Promise.resolve({ total: 0, items: [] })
      return Promise.resolve({})
    })
    post.mockResolvedValue({ batch_id: 'sb_1', items: [], cards: [] })
    const w = mount(StudyView, { global: { provide: { showToast: vi.fn() } } })
    await flushPromises()

    expect(w.text()).toContain('反向传播沿计算图反向应用链式法则')
    const editor = w.find('.suggestion-editor')
    await editor.find('textarea').setValue('编辑后的问题')
    await w.find('button.save-suggestion').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/study/suggestions/operations', {
      request_id: expect.stringMatching(/^study-suggestion-operation:/),
      batch_id: 'sb_1',
      items: [{
        suggestion_id: 'ss_1',
        expected_revision: 1,
        action: 'edit',
        patch: {
          card_type: 'basic',
          front: '编辑后的问题',
          back: '沿计算图反向应用链式法则。',
          explanation: '每个局部导数只计算一次。',
          concept_term: '反向传播',
        },
      }],
    })
  })

  it('候选操作模糊失败重试复用 request_id', async () => {
    get.mockImplementation((path: string) => {
      if (path.startsWith('/api/study/due')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/cards')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/stats')) return Promise.resolve({
        total: 0,
        statuses: { suggested: 0, active: 0, suspended: 0, rejected: 0 },
        due: 0,
        reviewed_cards: 0,
        reviews_total: 0,
        grades: { again: 0, hard: 0, good: 0, easy: 0 },
        retained_reviews: 0,
        retention_rate: 0,
      })
      if (path.startsWith('/api/study/suggestions')) {
        return Promise.resolve({ total: 1, items: [suggestion()] })
      }
      if (path.startsWith('/api/study/mastery')) return Promise.resolve({ total: 0, items: [] })
      return Promise.resolve({})
    })
    post
      .mockRejectedValueOnce(new Error('network reset'))
      .mockResolvedValueOnce({ batch_id: 'sb_1', items: [], cards: [] })
    const w = mount(StudyView, { global: { provide: { showToast: vi.fn() } } })
    await flushPromises()
    const save = w.find('button.save-suggestion')

    await save.trigger('click')
    await flushPromises()
    await save.trigger('click')
    await flushPromises()

    const calls = post.mock.calls.filter(([path]) => path === '/api/study/suggestions/operations')
    expect(calls).toHaveLength(2)
    expect(calls[0][1].request_id).toBe(calls[1][1].request_id)
  })

  it('同批候选批量接受并在 409 时刷新', async () => {
    const toast = vi.fn()
    let suggestionLoads = 0
    get.mockImplementation((path: string) => {
      if (path.startsWith('/api/study/due')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/cards')) return Promise.resolve({ total: 0, items: [] })
      if (path.startsWith('/api/study/stats')) return Promise.resolve({
        total: 0,
        statuses: { suggested: 0, active: 0, suspended: 0, rejected: 0 },
        due: 0,
        reviewed_cards: 0,
        reviews_total: 0,
        grades: { again: 0, hard: 0, good: 0, easy: 0 },
        retained_reviews: 0,
        retention_rate: 0,
      })
      if (path.startsWith('/api/study/suggestions')) {
        suggestionLoads += 1
        return Promise.resolve({
          total: 2,
          items: [suggestion(), suggestion({ suggestion_id: 'ss_2', ordinal: 1, front: '第二题' })],
        })
      }
      if (path.startsWith('/api/study/mastery')) return Promise.resolve({ total: 0, items: [] })
      return Promise.resolve({})
    })
    const conflict: any = new Error('revision conflict')
    conflict.status = 409
    post.mockRejectedValue(conflict)
    const w = mount(StudyView, { global: { provide: { showToast: toast } } })
    await flushPromises()

    const checks = w.findAll('.suggestion-select input[type="checkbox"]')
    await checks[0].setValue(true)
    await checks[1].setValue(true)
    const accept = w.findAll('.bulk-bar button').find((button) => button.text().includes('接受'))!
    await accept.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/study/suggestions/operations', expect.objectContaining({
      batch_id: 'sb_1',
      items: [
        expect.objectContaining({ suggestion_id: 'ss_1', action: 'accept', expected_revision: 1 }),
        expect.objectContaining({ suggestion_id: 'ss_2', action: 'accept', expected_revision: 1 }),
      ],
    }))
    expect(suggestionLoads).toBe(2)
    expect(toast).toHaveBeenCalledWith('候选已变化,已刷新最新状态', 'error')
  })
})
