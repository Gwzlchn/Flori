import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import JobPipelinePanel from './JobPipelinePanel.vue'

const emptyTotal = {
  provider: '', cost: 0, equiv: false, showUsd: false, hasQoder: false,
  credits: null, creditsPartial: false, qoderCalls: 0, creditReports: 0, calls: 0,
}

describe('JobPipelinePanel AI 成本展示', () => {
  it('Part步骤可选择自己的工作台scope并单独重试', async () => {
    const step = {
      name: '08_punctuate', label: '口播稿', status: 'failed',
      started_at: null, finished_at: null, duration_sec: null,
      meta: {}, error: 'boom', worker_id: null,
    }
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-1', steps: [],
        parts: [{
          part_id: 'pt_a', part_index: 1, title: '开场', url: null,
          status: 'failed', progress_pct: 80, media: {}, steps: [step],
        }],
        dagSteps: [], statusByKey: {}, selectedStep: '08_punctuate', selectedPartId: 'pt_a',
        usageByStep: {}, totalAi: emptyTotal, jobStatus: 'failed',
        rebuilding: false, updateAvailable: false, promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    const stepButton = wrapper.find('.part-steps button')
    expect(stepButton.classes()).toContain('selected')
    await stepButton.trigger('click')
    expect(wrapper.emitted('selectPartStep')).toEqual([['pt_a', '08_punctuate']])
    await wrapper.find('.part-retry').trigger('click')
    expect(wrapper.emitted('rerunPart')).toEqual([['pt_a', '08_punctuate']])
  })

  it('DAG 总计和节点固定两位小数且节点不显示约等号', () => {
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-1',
        steps: [],
        dagSteps: [
          { key: '01_download', label: '下载', pool: 'io', needs: [] },
          { key: '04_translate', label: '翻译', pool: 'ai', needs: ['01_download'] },
        ],
        statusByKey: { '01_download': 'done', '04_translate': 'done' },
        selectedStep: '04_translate',
        usageByStep: { '04_translate': {
          provider: 'claude-cli', cost: 4.9346, equiv: true, showUsd: true,
          hasQoder: false, credits: null, creditsPartial: false, qoderCalls: 0, creditReports: 0,
        } },
        totalAi: {
          provider: 'claude-cli', cost: 6.4855, equiv: true, showUsd: true,
          hasQoder: false, credits: null, creditsPartial: false,
          qoderCalls: 0, creditReports: 0, calls: 2,
        },
        jobStatus: 'done',
        rebuilding: false,
        updateAvailable: false,
        promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    expect(wrapper.text()).toContain('AI 总开销 $6.49')
    expect(wrapper.text()).toContain('$4.93')
    expect(wrapper.text()).not.toContain('$6.4855')
    expect(wrapper.text()).not.toContain('$4.9346')
    expect(wrapper.text()).not.toContain('≈')
  })

  it('Qoder DAG 总计和节点展示 credit,不展示误导性零美元', () => {
    const qoderUsage = {
      provider: 'qoder-cli', cost: 0, equiv: false, showUsd: false,
      hasQoder: true, credits: 2.2650132450000005, creditsPartial: false,
      qoderCalls: 1, creditReports: 1,
    }
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-qoder', steps: [],
        dagSteps: [{ key: '04_smart', label: '智能笔记', pool: 'ai', needs: [] }],
        statusByKey: { '04_smart': 'done' }, selectedStep: '04_smart',
        usageByStep: { '04_smart': qoderUsage },
        totalAi: { ...qoderUsage, calls: 1 },
        jobStatus: 'done', rebuilding: false, updateAvailable: false, promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    expect(wrapper.text()).toContain('2.2650 credits')
    expect(wrapper.text()).not.toContain('$0.00')
  })

  it('Qoder credit 未上报时不冒充为 0 credit', () => {
    const missingUsage = {
      provider: 'qoder-cli', cost: 0, equiv: false, showUsd: false,
      hasQoder: true, credits: null, creditsPartial: false,
      qoderCalls: 1, creditReports: 0,
    }
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-qoder-missing', steps: [],
        dagSteps: [{ key: '04_smart', label: '智能笔记', pool: 'ai', needs: [] }],
        statusByKey: { '04_smart': 'done' }, selectedStep: '04_smart',
        usageByStep: { '04_smart': missingUsage },
        totalAi: { ...missingUsage, calls: 1 },
        jobStatus: 'done', rebuilding: false, updateAvailable: false, promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    expect(wrapper.text()).toContain('credits 未上报')
    expect(wrapper.text()).not.toContain('0.0000 credits')
  })

  it('失败任务全局只提供从失败处继续,步骤重跑留在步骤详情', () => {
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-1', steps: [], dagSteps: [], statusByKey: {}, selectedStep: '01_download',
        usageByStep: {}, totalAi: emptyTotal, jobStatus: 'failed',
        rebuilding: false, updateAvailable: false, promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    expect(wrapper.text()).toContain('从失败处继续')
    expect(wrapper.text()).not.toContain('重建新版本')
    expect(wrapper.text()).not.toContain('从「下载」重跑')
  })

  it('仅检测到定义更新时提供版本升级入口', () => {
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-1', steps: [], dagSteps: [], statusByKey: {}, selectedStep: '',
        usageByStep: {}, totalAi: emptyTotal, jobStatus: 'done',
        rebuilding: false, updateAvailable: true, promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    const update = wrapper.find('button[data-test="pipeline-update"]')
    expect(update.exists()).toBe(true)
    expect(update.text()).toBe('更新到最新流程')
    expect(wrapper.find('.update-desc').text()).toBe('流程或 Prompt 已更新,将创建新版本并保留当前版本')
  })

  it('失败续跑与流程更新放在同一操作行', () => {
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-1', steps: [], dagSteps: [], statusByKey: {}, selectedStep: '01_download',
        usageByStep: {}, totalAi: emptyTotal, jobStatus: 'failed',
        rebuilding: false, updateAvailable: true, promptRows: [],
      },
      global: { stubs: { StepWorkbench: true } },
    })

    const actions = wrapper.find('[data-test="job-actions"]')
    expect(actions.findAll('button')).toHaveLength(2)
    expect(actions.findAll('button').map(button => button.text())).toEqual(['从失败处继续', '更新到最新流程'])
    expect(wrapper.find('.update-strip').exists()).toBe(false)
  })
})
