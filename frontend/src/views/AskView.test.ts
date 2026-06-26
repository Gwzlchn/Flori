import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

// router:共享 push 间谍断言来源 chip 跳转;useRoute 给空(AskView 不读 params)。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

// useApi:post 打 /api/ask;get 供 domains store 拉 /api/domains(真 Pinia)。
const post = vi.fn()
const get = vi.fn()
vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get, post, put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }),
}))

import AskView from './AskView.vue'

function answerFixture(over: Record<string, any> = {}) {
  return {
    question: '反向传播和梯度下降有什么区别？',
    answer_markdown:
      '反向传播用于计算梯度 [来源1]。\n\n## 共识 / 分歧\n各来源一致认为它是核心。',
    sources: [
      { job_id: 'j_bp', title: '反向传播详解', domain: 'ml', content_type: 'video' },
      { job_id: 'j_grad', title: '梯度下降综述', domain: 'ml', content_type: 'paper' },
    ],
    retrieved_count: 2,
    ...over,
  }
}

async function mountView() {
  // get 默认回 domains(onMounted 拉一次),用例可前置覆盖。
  get.mockResolvedValue({ domains: [{ domain: 'ml', display_name: 'ML' }] })
  const w = mount(AskView)
  await flushPromises()
  return w
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

describe('AskView 初始态', () => {
  it('未提问显示初始提示，不打 /api/ask', async () => {
    const w = await mountView()
    expect(w.text()).toContain('提个问题开始吧')
    expect(post).not.toHaveBeenCalled()
  })

  it('域选择器从 domains store 渲染选项', async () => {
    const w = await mountView()
    const opts = w.findAll('select option')
    expect(opts[0].text()).toContain('全部知识库')
    expect(opts.some((o) => o.text() === 'ML')).toBe(true)
  })
})

describe('AskView 提问流程', () => {
  it('提交 POST /api/ask 后渲染答案 markdown + 来源 chips', async () => {
    post.mockResolvedValue(answerFixture())
    const w = await mountView()

    await w.find('textarea').setValue('反向传播和梯度下降有什么区别？')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toBe('/api/ask')
    expect(post.mock.calls[0][1]).toMatchObject({ question: '反向传播和梯度下降有什么区别？' })

    const t = w.text()
    // MarkdownViewer 渲染的答案正文(含内联引用与共识/分歧段)。
    expect(t).toContain('反向传播用于计算梯度')
    expect(t).toContain('来源1')
    expect(t).toContain('共识 / 分歧')
    // 来源 chips:两条,带标题。
    expect(t).toContain('综合自 2 篇笔记')
    expect(t).toContain('反向传播详解')
    expect(t).toContain('梯度下降综述')
  })

  it('domain 选择拼进请求体（空=null 全库）', async () => {
    post.mockResolvedValue(answerFixture())
    const w = await mountView()
    await w.find('textarea').setValue('问题X')
    await w.find('select').setValue('ml')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    expect(post.mock.calls[0][1]).toMatchObject({ question: '问题X', domain: 'ml' })
  })

  it('空问题不触发请求（按钮 disabled）', async () => {
    const w = await mountView()
    const btn = w.find('button.btn-submit')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)
    await btn.trigger('click')
    expect(post).not.toHaveBeenCalled()
  })

  it('点击来源 chip 跳 /content/{job_id}（encode）', async () => {
    post.mockResolvedValue(answerFixture({
      sources: [{ job_id: 'a b/c', title: 'T', domain: 'ml', content_type: 'video' }],
    }))
    const w = await mountView()
    await w.find('textarea').setValue('问题')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    await w.find('.source-chip').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/a%20b%2Fc')
  })
})

describe('AskView 空/错误态', () => {
  it('无命中显示空状态与后端提示文案', async () => {
    post.mockResolvedValue(answerFixture({
      answer_markdown: '没有找到相关笔记，无法作答。',
      sources: [],
      retrieved_count: 0,
    }))
    const w = await mountView()
    await w.find('textarea').setValue('量子计算机超导体')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('没有找到相关笔记')
    expect(w.find('.source-chip').exists()).toBe(false)
  })

  it('请求抛错显示错误态与重试按钮', async () => {
    post.mockRejectedValue(new Error('综合后端炸了'))
    const w = await mountView()
    await w.find('textarea').setValue('boom')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('综合后端炸了')
    expect(w.findAll('button').some((b) => b.text() === '重试')).toBe(true)
  })
})
