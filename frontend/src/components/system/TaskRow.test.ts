import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'

const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

import TaskRow from './TaskRow.vue'

const NOW = new Date('2026-06-27T14:40:00').getTime()

function mountRow(props: Record<string, any>) {
  return mount(TaskRow, {
    props: { now: NOW, ...props },
    global: { stubs: { StatusBadge: { template: '<span class="sb">{{ status }}</span>', props: ['status'] } } },
  })
}

describe('TaskRow', () => {
  it('主显作业标题;无标题退 类型 → 流水线 → job_id', () => {
    expect(mountRow({ state: 'queued', jobId: 'j_1', step: 's', title: 'RLHF 综述' }).find('.title').text())
      .toBe('RLHF 综述')
    expect(mountRow({ state: 'queued', jobId: 'j_1', step: 's', contentType: 'paper' }).find('.title').text())
      .toBe('论文')
    expect(mountRow({ state: 'queued', jobId: 'j_1', step: 's', pipeline: 'video' }).find('.title').text())
      .toBe('video')
    // 全缺 → 兜底 job_id(保证旧 worker 历史无 enrich 时仍可读)
    expect(mountRow({ state: 'queued', jobId: 'jobs_x', step: 's' }).find('.title').text())
      .toBe('jobs_x')
  })

  it('job_id 退为 tooltip(title 属性),不作主显', () => {
    const w = mountRow({ state: 'queued', jobId: 'j_abc', step: 's', title: 'T' })
    expect(w.find('.title').attributes('title')).toBe('j_abc')
  })

  it('排队中:优先级徽章 + 投递点 + 已等', () => {
    const enq = NOW / 1000 - 300   // 5 分钟前入队
    const w = mountRow({ state: 'queued', jobId: 'j', step: 's', priority: 100, enqueuedAt: enq })
    const t = w.text()
    expect(t).toContain('优先级 100')
    expect(t).toContain('投递')
    expect(t).toContain('已等 5m00s')
  })

  it('排队中无 enqueuedAt:退「等待认领」', () => {
    const w = mountRow({ state: 'queued', jobId: 'j', step: 's', priority: 50 })
    expect(w.text()).toContain('等待认领')
  })

  it('运行中:运行徽章 + 开始点 + 已运行', () => {
    const started = new Date('2026-06-27T14:38:00').toISOString()  // 2 分钟前
    const w = mountRow({ state: 'running', jobId: 'j', step: 's', startedAt: started, worker: 'office-pc' })
    const t = w.text()
    expect(t).toContain('运行中')
    expect(t).toContain('开始')
    expect(t).toContain('已运行 2m00s')
    expect(t).toContain('office-pc')
  })

  it('已完成:状态徽章 + 耗时 + 结束点', () => {
    const fin = new Date('2026-06-27T04:12:00').toISOString()
    const w = mountRow({ state: 'completed', jobId: 'j', step: 's', status: 'done', durationSec: 1.0, finishedAt: fin })
    const t = w.text()
    expect(t).toContain('耗时 1s')
    expect(t).toContain('结束')
    expect(w.find('.sb').text()).toBe('done')
  })

  it('整行点击跳内容详情', async () => {
    const w = mountRow({ state: 'queued', jobId: 'j_click', step: 's', title: 'T' })
    await w.find('.row').trigger('click')
    expect(push).toHaveBeenCalledWith('/content/j_click')
  })
})
