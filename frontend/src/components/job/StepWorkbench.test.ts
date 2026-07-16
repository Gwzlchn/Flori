import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const get = vi.fn()
const getText = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get, getText, post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() }),
}))

import StepWorkbench from './StepWorkbench.vue'

const steps = [
  {
    name: '03_sections', label: '章节切分', status: 'done',
    started_at: '2026-07-16T00:00:00Z', finished_at: '2026-07-16T00:00:01Z',
    duration_sec: 1, meta: {}, error: null,
  },
  {
    name: '04_translate', label: '翻译', status: 'done',
    started_at: '2026-07-16T00:00:01Z', finished_at: '2026-07-16T00:01:01Z',
    duration_sec: 60, meta: {}, error: null,
  },
]

const usage = [
  {
    step: '04_translate', worker_id: 'ai-1', provider: 'claude-cli', model: 'opus',
    input_tokens: 1000, output_tokens: 200, cache_creation_tokens: 100,
    cache_read_tokens: 400, cost_usd: 0.25, duration_sec: 20, num_turns: 1,
    cache_hit_rate_pct: 26.7,
  },
  {
    step: '04_translate', worker_id: 'ai-1', provider: 'claude-cli', model: 'opus',
    input_tokens: 2000, output_tokens: 300, cache_creation_tokens: 0,
    cache_read_tokens: 500, cost_usd: 0.5, duration_sec: 30, num_turns: 1,
    cache_hit_rate_pct: 20,
  },
]

function mountWorkbench(selectedStep: string) {
  return mount(StepWorkbench, {
    props: { jobId: 'job-1', steps, selectedStep },
    global: {
      stubs: {
        MarkdownViewer: true,
        AiLogPanel: { props: ['jobId', 'step'], template: '<div data-test="ai-log-panel">审计 {{ step }}</div>' },
      },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  get.mockImplementation((url: string) => {
    if (url.endsWith('/artifacts')) return Promise.resolve({ groups: [], total_bytes: 1024 })
    if (url.endsWith('/usage')) return Promise.resolve({ usage })
    return Promise.resolve({})
  })
  getText.mockResolvedValue('')
})

describe('StepWorkbench 步骤级 AI 用量', () => {
  it('CPU 步不展示整个 job 的 AI 总开销', async () => {
    const wrapper = mountWorkbench('03_sections')
    await flushPromises()

    expect(wrapper.text()).toContain('章节切分')
    expect(wrapper.text()).not.toContain('AI 用量')
    expect(wrapper.text()).not.toContain('$0.7500')
  })

  it('AI 步先展示本步汇总,箭头展开同一区域内的逐次审计日志', async () => {
    const wrapper = mountWorkbench('04_translate')
    await flushPromises()

    expect(wrapper.text()).toContain('AI 用量')
    expect(wrapper.text()).toContain('$0.7500')
    expect(wrapper.text()).toContain('2 次')
    expect(wrapper.text()).toContain('入 3,000')
    expect(wrapper.text()).toContain('出 500')
    expect(wrapper.text()).toContain('读缓存 900')
    expect(wrapper.text()).toContain('写缓存 100')
    expect(wrapper.text()).toContain('命中 22.5%')
    expect(wrapper.find('[data-test="ai-log-panel"]').exists()).toBe(false)

    const toggle = wrapper.findAll('button').find(button => button.text().includes('展开审计日志'))
    expect(toggle).toBeTruthy()
    await toggle!.trigger('click')

    expect(wrapper.text()).toContain('收起审计日志')
    expect(wrapper.find('[data-test="ai-log-panel"]').text()).toContain('审计 04_translate')
  })

  it('重跑入口只作为所选步骤的上下文操作出现', async () => {
    const wrapper = mount(StepWorkbench, {
      props: { jobId: 'job-1', steps, selectedStep: '03_sections', canRerun: true },
      global: { stubs: { MarkdownViewer: true, AiLogPanel: true } },
    })
    await flushPromises()

    const button = wrapper.findAll('button').find(item => item.text().includes('重跑此步骤及后续'))
    expect(button).toBeTruthy()
    await button!.trigger('click')
    expect(wrapper.emitted('rerun')).toHaveLength(1)
  })
})
