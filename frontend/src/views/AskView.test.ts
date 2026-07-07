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

// POST /api/ask 的 202 响应(命中:task_id 有、answer_markdown=null,答案走轮询)。
function askResp(over: Record<string, any> = {}) {
  return {
    question: '反向传播和梯度下降有什么区别？',
    task_id: 'at_1',
    answer_markdown: null,
    sources: [
      {
        job_id: 'j_bp', title: '反向传播详解', domain: 'ml', content_type: 'video',
        evidence: { chunk_id: 'j_bp:smart:0', chunk_index: 0, section: null, snippet: '反向传播' },
      },
      { job_id: 'j_grad', title: '梯度下降综述', domain: 'ml', content_type: 'paper' },
    ],
    retrieved_count: 2,
    ...over,
  }
}

// 轮询 GET /api/ai-tasks/{id}/result 的 done 结果(answer_markdown = content)。
function doneResult(md = '反向传播用于计算梯度 [来源1]。\n\n## 共识 / 分歧\n各来源一致认为它是核心。') {
  return { status: 'done', task_id: 'at_1', answer_markdown: md, content: md }
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

describe('AskView 提问流程(异步)', () => {
  it('提交 /api/ask(202) → 轮询 result 渲染答案 markdown + 来源 chips', async () => {
    post.mockResolvedValue(askResp())
    const w = await mountView()
    get.mockResolvedValue(doneResult())   // 轮询 result 立即 done

    await w.find('textarea').setValue('反向传播和梯度下降有什么区别？')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toBe('/api/ask')
    expect(post.mock.calls[0][1]).toMatchObject({ question: '反向传播和梯度下降有什么区别？' })
    // 拿到 task_id 后轮询了 result 端点
    expect(get).toHaveBeenCalledWith('/api/ai-tasks/at_1/result')

    const t = w.text()
    expect(t).toContain('反向传播用于计算梯度')   // 答案来自 result(MarkdownViewer 渲染)
    expect(t).toContain('来源1')
    expect(t).toContain('共识 / 分歧')
    expect(t).toContain('综合自 2 条来源')
    expect(t).toContain('反向传播详解')           // 来源 chips(随 202 即得)
    expect(t).toContain('片段 1')
    expect(t).toContain('梯度下降综述')
  })

  it('domain 选择拼进请求体（空=null 全库）', async () => {
    post.mockResolvedValue(askResp())
    const w = await mountView()
    get.mockResolvedValue(doneResult())
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
    post.mockResolvedValue(askResp({
      sources: [{ job_id: 'a b/c', title: 'T', domain: 'ml', content_type: 'video' }],
    }))
    const w = await mountView()
    get.mockResolvedValue(doneResult())
    await w.find('textarea').setValue('问题')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    await w.find('.source-chip').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/a%20b%2Fc')
  })
})

describe('AskView 空/错误态', () => {
  it('无命中(task_id=null)显示空状态与提示文案,不轮询', async () => {
    post.mockResolvedValue(askResp({
      task_id: null,
      answer_markdown: '没有找到相关笔记，无法作答。',
      sources: [],
      retrieved_count: 0,
    }))
    const w = await mountView()
    get.mockClear()
    await w.find('textarea').setValue('量子计算机超导体')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('没有找到相关笔记')
    expect(w.find('.source-chip').exists()).toBe(false)
    expect(get).not.toHaveBeenCalledWith(expect.stringContaining('/result'))  // 无 task 不轮询
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

  it('轮询 result 返回 error → 显示 AI 错误', async () => {
    post.mockResolvedValue(askResp())
    const w = await mountView()
    get.mockResolvedValue({ status: 'error', task_id: 'at_1', error: 'provider down' })
    await w.find('textarea').setValue('Q')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('provider down')
  })

  it('答案就绪后展开「查看 AI 审计」→ 拉 /log 展示白盒', async () => {
    post.mockResolvedValue(askResp())
    const w = await mountView()
    get.mockImplementation((p: string) => {
      if (p.includes('/result')) return Promise.resolve(doneResult())
      if (p.includes('/log')) return Promise.resolve({
        task_id: 'at_1', count: 1,
        calls: [{
          task_id: 'at_1', provider: 'claude-cli', model: 'subscription', ok: true,
          record: {
            output: '审计输出OUT',
            prompt: { system: 'SYS', messages: [{ role: 'user', content: 'USER内容' }] },
            usage: { input_tokens: 10, cost_usd: 0.1 },
          },
        }],
      })
      return Promise.resolve({ domains: [] })
    })
    await w.find('textarea').setValue('Q')
    await w.find('button.btn-submit').trigger('click')
    await flushPromises()
    const auditBtn = w.findAll('button').find((b) => b.text().includes('查看 AI 审计'))
    expect(auditBtn).toBeTruthy()
    await auditBtn!.trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/ai-tasks/at_1/log')
    const t = w.text()
    expect(t).toContain('claude-cli')
    expect(t).toContain('审计输出OUT')
  })
})
