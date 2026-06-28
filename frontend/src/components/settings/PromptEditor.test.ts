import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// PromptEditor 直接调 useApi(get 读默认+覆盖、put 存、del 恢复默认)。
const get = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get, post: vi.fn(), put, del, upload: vi.fn(), getText: vi.fn() }),
}))

import PromptEditor from './PromptEditor.vue'

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue({
    default_template: 'DEFAULT TEMPLATE BODY',
    override: { scope: 'global', domain: '', content: 'EXISTING OVERRIDE', updated_at: 't' },
  })
  put.mockResolvedValue({ status: 'saved' })
  del.mockResolvedValue(null)
})

async function mountEditor(props = {}) {
  const w = mount(PromptEditor, {
    props: { pipeline: 'video', step: '11_smart', label: '智能笔记', ...props },
  })
  await flushPromises()
  return w
}

describe('PromptEditor', () => {
  it('loads default template + existing override on mount', async () => {
    const w = await mountEditor()
    expect(get).toHaveBeenCalledWith('/api/prompts/video/11_smart?scope=global')
    // 覆盖正文进入 textarea
    expect((w.find('textarea').element as HTMLTextAreaElement).value).toBe('EXISTING OVERRIDE')
    // 标题含 pipeline + label
    expect(w.text()).toContain('video')
    expect(w.text()).toContain('智能笔记')
  })

  it('saves override via PUT with global scope', async () => {
    const w = await mountEditor()
    await w.find('textarea').setValue('NEW SYSTEM PROMPT')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/video/11_smart', {
      scope: 'global',
      domain: undefined,
      content: 'NEW SYSTEM PROMPT',
    })
    expect(w.emitted('saved')).toBeTruthy()
  })

  it('restore default calls DELETE and clears content', async () => {
    const w = await mountEditor()
    const restoreBtn = w.findAll('button').find((b) => b.text().includes('恢复默认'))!
    await restoreBtn.trigger('click')
    await flushPromises()
    expect(del).toHaveBeenCalledWith('/api/prompts/video/11_smart?scope=global')
    expect((w.find('textarea').element as HTMLTextAreaElement).value).toBe('')
    expect(w.emitted('saved')).toBeTruthy()
  })

  it('domain scope: shows domain input and PUT includes domain', async () => {
    const w = await mountEditor()
    // 切到领域作用域
    const domainRadio = w.findAll('input[type="radio"]').find(
      (r) => (r.element as HTMLInputElement).value === 'domain',
    )!
    await domainRadio.setValue() // 选中 domain
    await flushPromises()
    // 领域输入框出现
    const domInput = w.find('input.input')
    expect(domInput.exists()).toBe(true)
    await domInput.setValue('finance')
    await w.find('textarea').setValue('FIN PROMPT')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/video/11_smart', {
      scope: 'domain',
      domain: 'finance',
      content: 'FIN PROMPT',
    })
  })

  it('domain scope without domain blocks save (no PUT)', async () => {
    const w = await mountEditor()
    const domainRadio = w.findAll('input[type="radio"]').find(
      (r) => (r.element as HTMLInputElement).value === 'domain',
    )!
    await domainRadio.setValue()
    await flushPromises()
    await w.find('textarea').setValue('X')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(put).not.toHaveBeenCalled()
  })
})
