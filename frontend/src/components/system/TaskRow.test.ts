import { beforeEach, describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { installSourceCatalog } from '../../constants/sources'

const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

const deleteJob = vi.fn(() => Promise.resolve())
vi.mock('../../stores/jobs', () => ({
  useJobStore: () => ({ deleteJob }),
}))

import TaskRow from './TaskRow.vue'

const NOW = new Date('2026-06-27T14:40:00').getTime()

function mountRow(props: Record<string, any>) {
  return mount(TaskRow, {
    props: { now: NOW, ...props },
    global: { stubs: { StatusBadge: { template: '<span class="sb">{{ status }}</span>', props: ['status'] } } },
  })
}

beforeEach(() => {
  installSourceCatalog({
    content_types: [{ type: 'paper', label: '论文', upload_extensions: ['.pdf'] }],
    job_sources: [],
    subscription_sources: [],
  })
})

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

  it('排队中:语义徽章「排队中」+ 投递点 + 已等(优先级数字弱化进 tooltip)', () => {
    const enq = NOW / 1000 - 300   // 5 分钟前入队
    const w = mountRow({ state: 'queued', jobId: 'j', step: 's', priority: 100, enqueuedAt: enq })
    const t = w.text()
    expect(t).toContain('排队中')          // 徽章只显语义文案,不显裸优先级数字
    expect(t).not.toContain('优先级 100')   // 原始数字弱化到 title tooltip
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

  it('默认显示删除按钮;confirm 后调 deleteJob + emit deleted + 不跳转', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    deleteJob.mockClear(); push.mockClear()
    const w = mountRow({ state: 'completed', jobId: 'j_del', step: 's', status: 'done' })
    const btn = w.find('.task-del')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    expect(deleteJob).toHaveBeenCalledWith('j_del')
    expect(w.emitted('deleted')?.[0]).toEqual(['j_del'])
    expect(push).not.toHaveBeenCalled()   // @click.stop:不触发整行跳转
  })

  it('confirm 取消则不删', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    deleteJob.mockClear()
    const w = mountRow({ state: 'completed', jobId: 'j', step: 's' })
    await w.find('.task-del').trigger('click')
    expect(deleteJob).not.toHaveBeenCalled()
  })

  it('deletable=false 不显示删除按钮', () => {
    const w = mountRow({ state: 'queued', jobId: 'j', step: 's', deletable: false })
    expect(w.find('.task-del').exists()).toBe(false)
  })
})
