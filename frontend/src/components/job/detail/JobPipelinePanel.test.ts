import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import JobPipelinePanel from './JobPipelinePanel.vue'

describe('JobPipelinePanel AI 成本展示', () => {
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
        usageByStep: { '04_translate': { provider: 'claude-cli', cost: 4.9346, equiv: true } },
        totalAi: { cost: 6.4855, equiv: true, calls: 2 },
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

  it('失败任务全局只提供从失败处继续,步骤重跑留在步骤详情', () => {
    const wrapper = mount(JobPipelinePanel, {
      props: {
        jobId: 'job-1', steps: [], dagSteps: [], statusByKey: {}, selectedStep: '01_download',
        usageByStep: {}, totalAi: { cost: 0, equiv: false, calls: 0 }, jobStatus: 'failed',
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
        usageByStep: {}, totalAi: { cost: 0, equiv: false, calls: 0 }, jobStatus: 'done',
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
        usageByStep: {}, totalAi: { cost: 0, equiv: false, calls: 0 }, jobStatus: 'failed',
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
