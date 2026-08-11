import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const get = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get }),
}))

import AiTaskAuditPanel from './AiTaskAuditPanel.vue'

beforeEach(() => vi.clearAllMocks())

describe('AiTaskAuditPanel', () => {
  it('Qoder审计显示原生credits并区分null与0', async () => {
    get.mockResolvedValue({
      task_id: 'task-1', count: 3,
      calls: [
        { provider: 'qoder-cli', model: 'ultimate', ok: true,
          cost_usd: 0, credits: 2.265013245,
          record: { usage: { cost_usd: 0, credits: 2.265013245 } } },
        { provider: 'qoder-cli', model: 'ultimate', ok: true,
          cost_usd: 0, credits: null, record: { usage: { cost_usd: 0, credits: null } } },
        { provider: 'qoder-cli', model: 'ultimate', ok: true,
          cost_usd: 0, credits: 0, record: { usage: { cost_usd: 0, credits: 0 } } },
      ],
    })
    const wrapper = mount(AiTaskAuditPanel, { props: { taskId: 'task-1' } })
    await flushPromises()

    expect(wrapper.text()).toContain('2.2650 credits')
    expect(wrapper.text()).toContain('credits 未上报')
    expect(wrapper.text()).toContain('0.0000 credits')
    expect(wrapper.text()).not.toContain('$0.0000')
  })

  it('Claude审计保留美元等价标识', async () => {
    get.mockResolvedValue({
      task_id: 'task-2', count: 1,
      calls: [{
        provider: 'claude-cli', model: 'claude-opus-5', ok: true,
        cost_usd: 0.25, credits: null,
        record: { usage: { cost_usd: 0.25, credits: null } },
      }],
    })
    const wrapper = mount(AiTaskAuditPanel, { props: { taskId: 'task-2' } })
    await flushPromises()

    expect(wrapper.text()).toContain('$0.2500（等价）')
  })
})
