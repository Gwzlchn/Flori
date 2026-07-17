import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const push = vi.fn()
const createJob = vi.fn()
const uploadJob = vi.fn()

vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))
vi.mock('../../stores/jobs', () => ({
  useJobStore: () => ({ createJob, uploadJob }),
}))
vi.mock('../../stores/global', () => ({
  useGlobalStore: () => ({
    profiles: [{ domain: 'general' }],
    styleTags: [],
    fetchProfiles: vi.fn(),
    fetchStyleTags: vi.fn(),
  }),
}))

import { installSourceCatalog } from '../../constants/sources'
import JobSubmitForm from './JobSubmitForm.vue'

describe('JobSubmitForm 多 Part 视频', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    createJob.mockResolvedValue({ job_id: 'job_multi', parts: [] })
    installSourceCatalog({
      content_types: [
        { type: 'video', label: '视频', upload_extensions: [] },
        { type: 'document', label: '文档', upload_extensions: ['.pdf'] },
      ],
      job_sources: [],
      subscription_sources: [],
    })
  })

  it('按界面顺序提交一个Job及多个Part', async () => {
    const wrapper = mount(JobSubmitForm, { props: { bare: true } })
    await wrapper.get('[data-test="multipart-video-mode"]').trigger('click')
    await wrapper.get('[data-test="multipart-title"]').setValue('整场直播')
    await wrapper.get('[data-test="part-url-1"]').setValue('https://example.com/p1')
    await wrapper.get('[data-test="part-title-1"]').setValue('开场')
    await wrapper.get('[data-test="add-part"]').trigger('click')
    await wrapper.get('[data-test="part-url-2"]').setValue('https://example.com/p2')
    await wrapper.get('[data-test="add-part"]').trigger('click')
    await wrapper.get('[data-test="part-url-3"]').setValue('https://example.com/p3')

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(createJob).toHaveBeenCalledWith(expect.objectContaining({
      content_type: 'video',
      title: '整场直播',
      parts: [
        { url: 'https://example.com/p1', title: '开场' },
        { url: 'https://example.com/p2' },
        { url: 'https://example.com/p3' },
      ],
    }))
    expect(push).toHaveBeenCalledWith('/content/job_multi')
  })

  it('存在空Part时保持提交禁用', async () => {
    const wrapper = mount(JobSubmitForm, { props: { bare: true } })
    await wrapper.get('[data-test="multipart-video-mode"]').trigger('click')
    await wrapper.get('[data-test="part-url-1"]').setValue('https://example.com/p1')
    await wrapper.get('[data-test="add-part"]').trigger('click')

    expect(wrapper.get('[data-test="submit-job"]').attributes('disabled')).toBeDefined()
    expect(createJob).not.toHaveBeenCalled()
  })
})
