import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import { useWorkerStore } from '../stores/workers'

const push = vi.fn()
const replace = vi.fn()
let query: any = {}
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace }),
  useRoute: () => ({ query }),
}))

import QueueView from './QueueView.vue'

function makeQueue() {
  return {
    limit: 200,
    pools: [
      {
        name: 'ai', queued_count: 2, queued_shown: 1,
        running: [{ state: 'running', job_id: 'j_r', step: '10_smart', pool: 'ai',
          title: 'Transformer', started_at: '2026-06-27T12:00:00Z', worker_hostname: 'office-pc' }],
        queued: [{ state: 'queued', job_id: 'j_q', step: '10_smart', pool: 'ai',
          title: 'RLHF', priority: 100, enqueued_at: 1747483200 }],
      },
      { name: 'cpu', queued_count: 0, queued_shown: 0, running: [], queued: [] },
    ],
  }
}

const TaskRowStub = { props: ['state', 'jobId'], template: '<div class="trow" :data-state="state">{{ jobId }}</div>' }

let pinia: ReturnType<typeof createTestingPinia>
function mountView() {
  return mount(QueueView, { global: { plugins: [pinia], stubs: { TaskRow: TaskRowStub } } })
}

beforeEach(() => {
  vi.clearAllMocks()
  query = {}
  pinia = createTestingPinia({ createSpy: vi.fn, stubActions: true })
  setActivePinia(pinia)
  ;(useWorkerStore().fetchQueue as any).mockResolvedValue(makeQueue())
})

describe('QueueView', () => {
  it('渲染页头与运行/排队总数', async () => {
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('任务队列')
    expect(t).toContain('运行中 1')   // 总运行(ai=1)
    expect(t).toContain('排队中 2')   // 总排队 count(ai queued_count=2)
  })

  it('按池分组渲染运行中 + 排队中 TaskRow', async () => {
    const w = mountView()
    await flushPromises()
    const rows = w.findAll('.trow')
    expect(rows.length).toBe(2)   // 1 running + 1 queued(cpu 池空)
    expect(rows.some(r => r.attributes('data-state') === 'running')).toBe(true)
    expect(rows.some(r => r.attributes('data-state') === 'queued')).toBe(true)
  })

  it('排队截断时显「共 N 已列前 M」', async () => {
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('共 2 条')   // queued_count 2 > queued_shown 1
  })

  it('点池 chip 过滤并写入 query', async () => {
    const w = mountView()
    await flushPromises()
    const chip = w.findAll('.chip').find(c => c.text() === 'ai')
    await chip!.trigger('click')
    expect(replace).toHaveBeenCalledWith({ query: { pool: 'ai' } })
  })

  it('预选 query.pool 时只显该池', async () => {
    query = { pool: 'cpu' }
    const w = mountView()
    await flushPromises()
    const poolNames = w.findAll('.pool-name').map(n => n.text())
    expect(poolNames).toEqual(['cpu'])   // 仅 cpu(text-transform 仅视觉大写,DOM 文本仍原值)
  })
})
