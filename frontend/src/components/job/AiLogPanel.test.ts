import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// useApi.get 打 /api/jobs/{id}/ai-logs;其余方法占位。
const get = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get, post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }),
}))

import AiLogPanel from './AiLogPanel.vue'

function callFixture(over: Record<string, any> = {}) {
  return {
    call_index: 0, ok: true, session_id: 'sess-1',
    routing: {
      provider: 'claude-cli', model: 'claude-opus-4-8[1m]', tier_used: 'primary',
      attempts: [{ tier: 'primary', provider: 'claude-cli', ok: true }],
    },
    latency: { api_ms: 900, duration_total_sec: 1.5 },
    usage: { input_tokens: 2341, output_tokens: 1180, cache_creation_input_tokens: 5, cache_read_input_tokens: 10 },
    cost: { cost_usd: 0.043, basis: 'cli-equiv' },
    prompt: { rendered: { system: 'SYS', user: 'USER PROMPT' }, template: { source: 'default' } },
    output: { content: '# 智能笔记', num_turns: 1, finish_reason: 'end_turn' },
    flori: { version: '0.4.9' },
    ...over,
  }
}

const mockLogs = (calls: any[]) =>
  get.mockResolvedValue({ job_id: 'j1', steps: [{ step: '11_smart', calls }] })

beforeEach(() => vi.clearAllMocks())

async function mountPanel(step = '11_smart') {
  const w = mount(AiLogPanel, { props: { jobId: 'j1', step } })
  await flushPromises()
  return w
}

describe('AiLogPanel', () => {
  it('Part审计使用显式part_id查询', async () => {
    mockLogs([callFixture()])
    const w = mount(AiLogPanel, { props: { jobId: 'j1', step: '11_smart', partId: 'pt_a' } })
    await flushPromises()
    expect(get).toHaveBeenCalledWith(
      '/api/jobs/j1/ai-logs?step=11_smart&part_id=pt_a',
    )
    expect(w.text()).toContain('1 次调用概览')
  })

  it('先渲染折叠的调用概览,逐次展开后审计字段仍默认折叠', async () => {
    mockLogs([callFixture()])
    const w = await mountPanel()
    expect(get).toHaveBeenCalledWith(expect.stringContaining('/api/jobs/j1/ai-logs?step=11_smart'))
    expect(w.text()).toContain('1 次调用概览')
    expect(w.text()).toContain('调用 1/1')
    expect(w.text()).toContain('claude-cli')
    expect(w.text()).toContain('$0.0430')
    expect(w.text()).toContain('（等价）')

    const call = w.find('details.audit-call')
    expect(call.attributes('open')).toBeUndefined()
    await call.find('summary.audit-summary').trigger('click')
    expect((call.element as HTMLDetailsElement).open).toBe(true)

    const fields = call.findAll('details.audit-field')
    expect(fields).toHaveLength(3)
    expect(fields.every(field => field.attributes('open') == null)).toBe(true)
    expect(w.text()).toContain('USER PROMPT')
    expect(w.text()).toContain('# 智能笔记')
    expect(w.text()).toContain('session sess-1')
  })

  it('多次调用只显示等高概览行,不默认铺开 prompt', async () => {
    mockLogs([callFixture(), callFixture({ call_index: 1, cost: { cost_usd: 0.05 } })])
    const w = await mountPanel()

    expect(w.text()).toContain('2 次调用概览')
    expect(w.findAll('details.audit-call')).toHaveLength(2)
    expect(w.findAll('details.audit-call').every(call => call.attributes('open') == null)).toBe(true)
  })

  it('空态提示', async () => {
    mockLogs([])
    const w = await mountPanel()
    expect(w.text()).toContain('暂无 AI 日志')
  })

  it('失败调用显示错误 + 尝试链', async () => {
    mockLogs([callFixture({
      ok: false, error: 'All providers failed :: down', output: { content: null },
      routing: {
        provider: null, model: null, tier_used: null,
        attempts: [
          { tier: 'primary', provider: 'anthropic', ok: false },
          { tier: 'fallback', provider: 'deepseek', ok: false },
        ],
      },
    })])
    const w = await mountPanel()
    expect(w.text()).toContain('All providers failed')
    expect(w.text()).toContain('尝试链')
    expect(w.text()).toContain('primary/anthropic')
    expect(w.text()).toContain('fallback/deepseek')
  })

  it('step 变化重新拉取', async () => {
    mockLogs([callFixture()])
    const w = await mountPanel()
    expect(get).toHaveBeenCalledTimes(1)
    get.mockResolvedValue({ job_id: 'j1', steps: [{ step: '12_review', calls: [callFixture()] }] })
    await w.setProps({ step: '12_review' })
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(2)
    expect(get).toHaveBeenLastCalledWith(expect.stringContaining('step=12_review'))
  })
})
