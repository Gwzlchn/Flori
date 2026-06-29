import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// PromptEditor 直接调 useApi(get 读默认+覆盖、put 存、del 删/恢复默认)。
const get = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get, post: vi.fn(), put, del, upload: vi.fn(), getText: vi.fn() }),
}))

import PromptEditor from './PromptEditor.vue'

beforeEach(() => {
  vi.clearAllMocks()
  // 默认:有覆盖 → 预填覆盖。
  get.mockResolvedValue({
    default_template: 'DEFAULT TEMPLATE BODY',
    default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
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

const taVal = (w: any) => (w.find('textarea').element as HTMLTextAreaElement).value

describe('PromptEditor', () => {
  it('预填:有覆盖 → textarea 填覆盖内容', async () => {
    const w = await mountEditor()
    expect(get).toHaveBeenCalledWith('/api/prompts/video/11_smart?scope=global')
    expect(taVal(w)).toBe('EXISTING OVERRIDE')
    expect(w.text()).toContain('video')
    expect(w.text()).toContain('智能笔记')
  })

  it('预填:无覆盖 → textarea 填默认模板内容,状态标"当前为默认"', async () => {
    get.mockResolvedValue({
      default_template: 'DEFAULT TEMPLATE BODY',
      default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
      override: null,
    })
    const w = await mountEditor()
    expect(taVal(w)).toBe('DEFAULT TEMPLATE BODY')
    expect(w.text()).toContain('当前为默认')
  })

  it('改后保存(内容 != 默认)→ PUT 存覆盖,不调 DELETE', async () => {
    get.mockResolvedValue({
      default_template: 'DEFAULT TEMPLATE BODY',
      default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
      override: null,
    })
    const w = await mountEditor()
    await w.find('textarea').setValue('NEW PROMPT')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/video/11_smart', {
      scope: 'global',
      domain: undefined,
      content: 'NEW PROMPT',
    })
    expect(del).not.toHaveBeenCalled()
    expect(w.emitted('saved')).toBeTruthy()
  })

  it('保存(内容 == 默认)→ DELETE 删覆盖,不调 PUT', async () => {
    // 加载覆盖后,把内容改回默认值 → 保存应删覆盖。
    const w = await mountEditor()
    await w.find('textarea').setValue('DEFAULT TEMPLATE BODY')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(del).toHaveBeenCalledWith('/api/prompts/video/11_smart?scope=global')
    expect(put).not.toHaveBeenCalled()
    expect(w.emitted('saved')).toBeTruthy()
  })

  it('恢复默认 → textarea 重置为默认内容;随后保存调 DELETE', async () => {
    const w = await mountEditor() // 预填 EXISTING OVERRIDE
    expect(taVal(w)).toBe('EXISTING OVERRIDE')
    const restoreBtn = w.findAll('button').find((b) => b.text().includes('恢复默认'))!
    await restoreBtn.trigger('click')
    await flushPromises()
    expect(taVal(w)).toBe('DEFAULT TEMPLATE BODY')
    const saveBtn = w.findAll('button').find((b) => b.text().includes('保存'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(del).toHaveBeenCalledWith('/api/prompts/video/11_smart?scope=global')
    expect(put).not.toHaveBeenCalled()
  })

  it('领域作用域:显示领域输入,PUT 带 domain', async () => {
    get.mockResolvedValue({
      default_template: 'DEFAULT TEMPLATE BODY',
      default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
      override: null,
    })
    const w = await mountEditor()
    const domainRadio = w.findAll('input[type="radio"]').find(
      (r) => (r.element as HTMLInputElement).value === 'domain',
    )!
    await domainRadio.setValue()
    await flushPromises()
    const domInput = w.find('input.input')
    expect(domInput.exists()).toBe(true)
    await domInput.setValue('finance')
    await domInput.trigger('change')
    await flushPromises()
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

  it('领域作用域未填领域 → 阻止保存(不调 PUT/DELETE)', async () => {
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
    expect(del).not.toHaveBeenCalled()
  })

  it('多模板步:其余变体只读展示,不混进可编辑 textarea', async () => {
    get.mockResolvedValue({
      default_template: 'MAIN BODY',
      default_templates: [
        { name: '11_smart', content: 'MAIN BODY' },
        { name: '11_smart.vision', content: 'VISION BODY' },
      ],
      default_system: 'SYS DEFAULT',
      override: null,
    })
    const w = await mountEditor()
    // 主模板进可编辑框
    expect(taVal(w)).toBe('MAIN BODY')
    // 变体只读展示(在文本里,但不在 textarea)
    expect(w.text()).toContain('VISION BODY')
    expect(w.text()).toContain('11_smart.vision')
    expect(w.text()).toContain('SYS DEFAULT')
    expect(taVal(w)).not.toContain('VISION BODY')
  })
})
