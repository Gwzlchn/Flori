import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// 依赖 mock(须在 import 组件前)。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import GlossaryView from './GlossaryView.vue'

// GlossaryTerm 夹具(按 types.GlossaryTerm 补齐字段)。
function makeTerm(over: Record<string, unknown> = {}) {
  return {
    domain: '机器学习',
    term: '注意力机制',
    definition: '一种加权聚合机制',
    occurrences: [{}, {}],
    related: ['自注意力'],
    status: 'accepted',
    is_topic: false,
    definition_locked: false,
    created_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function factory() {
  return mount(GlossaryView, {
    global: {
      provide: { showToast: vi.fn() },
      stubs: { StatusBadge: true },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  api.get.mockResolvedValue([])
  api.post.mockResolvedValue(undefined)
  api.put.mockResolvedValue(undefined)
  api.del.mockResolvedValue(undefined)
})

describe('GlossaryView', () => {
  it('onMounted 调 GET /api/glossary（无筛选无 query）', async () => {
    factory()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/glossary')
  })

  it('渲染头部标题与说明', async () => {
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('概念库')
  })

  it('空态：无概念时展示空文案与新增按钮', async () => {
    api.get.mockResolvedValue([])
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('还没有概念')
    expect(w.text()).toContain('新增概念')
  })

  it('错误态：加载失败展示错误信息与重试', async () => {
    api.get.mockRejectedValueOnce({ message: '加载炸了' })
    const w = factory()
    await flushPromises()
    expect(w.text()).toContain('加载炸了')
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))
    expect(retry).toBeTruthy()
    api.get.mockResolvedValue([])
    await retry!.trigger('click')
    await flushPromises()
    // 重试再次请求
    expect(api.get).toHaveBeenCalledWith('/api/glossary')
  })

  it('列表：候选与已采纳分区渲染', async () => {
    api.get.mockResolvedValue([
      makeTerm({ term: 'A候选', status: 'suggested' }),
      makeTerm({ term: 'B采纳', status: 'accepted' }),
    ])
    const w = factory()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('待审建议')
    expect(t).toContain('已采纳')
    expect(t).toContain('A候选')
    expect(t).toContain('B采纳')
  })

  it('采纳候选：POST accept 后重新加载', async () => {
    api.get.mockResolvedValue([makeTerm({ term: '候选词', status: 'suggested' })])
    const w = factory()
    await flushPromises()
    // 精确匹配行内「采纳」:区分于区头的「全部采纳」批量按钮。
    const acceptBtn = w.findAll('button').find((b) => b.text() === '采纳')
    expect(acceptBtn).toBeTruthy()
    await acceptBtn!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(
      '/api/glossary/' + encodeURIComponent('机器学习') + '/' + encodeURIComponent('候选词') + '/accept',
    )
  })

  it('全部采纳：POST /api/glossary/batch(action=accept,全量候选)', async () => {
    api.get.mockResolvedValue([makeTerm({ term: '候选词', status: 'suggested' })])
    api.post.mockResolvedValue({ updated: 1, skipped: 0 })
    const w = factory()
    await flushPromises()
    const batchBtn = w.findAll('button').find((b) => b.text().includes('全部采纳'))
    expect(batchBtn).toBeTruthy()
    await batchBtn!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/glossary/batch', {
      action: 'accept',
      items: [{ domain: '机器学习', term: '候选词' }],
    })
  })

  it('驳回候选：POST reject', async () => {
    api.get.mockResolvedValue([makeTerm({ term: '垃圾词', status: 'suggested' })])
    const w = factory()
    await flushPromises()
    const rejectBtn = w.findAll('button').find((b) => (b.attributes('title') || '').includes('驳回'))
    expect(rejectBtn).toBeTruthy()
    await rejectBtn!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(
      '/api/glossary/' + encodeURIComponent('机器学习') + '/' + encodeURIComponent('垃圾词') + '/reject',
    )
  })

  it('关注概念：POST watch(watched 取反)', async () => {
    api.get.mockResolvedValue([makeTerm({ term: '好词', status: 'accepted', watched: false })])
    const w = factory()
    await flushPromises()
    const watchBtn = w.findAll('button').find((b) => (b.attributes('title') || '').includes('关注'))
    expect(watchBtn).toBeTruthy()
    await watchBtn!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(
      '/api/glossary/' + encodeURIComponent('机器学习') + '/' + encodeURIComponent('好词') + '/watch',
      { watched: true },
    )
  })

  it('删除概念：confirm 通过后 DELETE 并重载', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    api.get.mockResolvedValue([makeTerm({ term: '待删', status: 'suggested' })])
    const w = factory()
    await flushPromises()
    const delBtn = w.findAll('button').find((b) => (b.attributes('title') || '') === '删除')
    expect(delBtn).toBeTruthy()
    await delBtn!.trigger('click')
    await flushPromises()
    expect(api.del).toHaveBeenCalledWith(
      '/api/glossary/' + encodeURIComponent('机器学习') + '/' + encodeURIComponent('待删'),
    )
    vi.unstubAllGlobals()
  })

  it('删除概念：confirm 取消则不发请求', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    api.get.mockResolvedValue([makeTerm({ term: '待删', status: 'suggested' })])
    const w = factory()
    await flushPromises()
    const delBtn = w.findAll('button').find((b) => (b.attributes('title') || '') === '删除')
    await delBtn!.trigger('click')
    await flushPromises()
    expect(api.del).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('已采纳项点击：跳转概念详情', async () => {
    api.get.mockResolvedValue([makeTerm({ term: '采纳词', status: 'accepted' })])
    const w = factory()
    await flushPromises()
    const occ = w.find('.occ')
    expect(occ.exists()).toBe(true)
    await occ.trigger('click')
    expect(push).toHaveBeenCalledWith(
      '/kb/' + encodeURIComponent('机器学习') + '/concepts/' + encodeURIComponent('采纳词'),
    )
  })

  it('切换主题：POST topic 取反 is_topic', async () => {
    api.get.mockResolvedValue([makeTerm({ term: '采纳词', status: 'accepted', is_topic: false })])
    const w = factory()
    await flushPromises()
    const topicBtn = w.findAll('button').find((b) => (b.attributes('title') || '').includes('主题'))
    expect(topicBtn).toBeTruthy()
    await topicBtn!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(
      '/api/glossary/' + encodeURIComponent('机器学习') + '/' + encodeURIComponent('采纳词') + '/topic',
      { is_topic: true },
    )
  })

  it('新增弹窗：打开后校验空输入报错', async () => {
    const w = factory()
    await flushPromises()
    const openBtn = w.findAll('button').find((b) => b.text().includes('新增概念'))
    await openBtn!.trigger('click')
    expect(w.text()).toContain('新增概念')
    // 不填直接提交 → 校验报错,不发请求
    const submit = w.findAll('button').find((b) => b.text().includes('添加'))
    await submit!.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('知识库与概念名不能为空')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('新增弹窗：填写后 POST 创建概念', async () => {
    const w = factory()
    await flushPromises()
    const openBtn = w.findAll('button').find((b) => b.text().includes('新增概念'))
    await openBtn!.trigger('click')
    const inputs = w.findAll('.modal input.input')
    // 第 0 个=知识库,第 1 个=概念名
    await inputs[0].setValue('NLP')
    await inputs[1].setValue('词向量')
    const submit = w.findAll('button').find((b) => b.text().includes('添加'))
    await submit!.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(
      '/api/glossary?domain=' + encodeURIComponent('NLP'),
      { term: '词向量', definition: null, related: [] },
    )
  })
})
