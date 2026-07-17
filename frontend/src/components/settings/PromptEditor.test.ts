import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// PromptEditor 直接调 useApi:get 读详情与历史版本,put 存 overwrite/new,post 切激活指针 activate,del 彻底删除。
const get = vi.fn()
const post = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get, post, put, del, upload: vi.fn(), getText: vi.fn() }),
}))

import PromptEditor from './PromptEditor.vue'

// 默认详情:有覆盖,激活 v2,两版历史。get 按 URL 分派(单步详情 vs 历史版本全文)。
const DETAIL = {
  default_template: 'DEFAULT TEMPLATE BODY',
  default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
  default_system: null,
  override: { scope: 'global', domain: '', content: 'V2 CONTENT', version: '2', updated_at: 't' },
  active_version: '2',
  versions: [
    { version: '1', note: '首版', created_at: 't1' },
    { version: '2', note: '第二版', created_at: 't2' },
  ],
}

function mockGet(detail: any = DETAIL) {
  get.mockImplementation((url: string) => {
    const m = url.match(/\/versions\/(\d+)/)
    if (m) {
      const ver = m[1]
      return Promise.resolve({ version: ver, content: `V${ver} HISTORICAL`, note: '', created_at: 't' })
    }
    return Promise.resolve(detail)
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockGet()
  put.mockResolvedValue({ status: 'saved', active_version: '3' })
  post.mockResolvedValue({ status: 'activated', active_version: '1' })
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
const btn = (w: any, label: string) => w.findAll('button').find((b: any) => b.text().includes(label))!

describe('PromptEditor 版本管理', () => {
  it('预填:有覆盖 → textarea 填激活版本内容 + 标当前激活 v2', async () => {
    const w = await mountEditor()
    expect(get).toHaveBeenCalledWith('/api/prompts/video/11_smart?scope=global')
    expect(taVal(w)).toBe('V2 CONTENT')
    expect(w.text()).toContain('当前激活 v2')
  })

  it('版本下拉渲染:默认 + v1 + v2(标当前激活),默认选中激活版本', async () => {
    const w = await mountEditor()
    const opts = w.find('[data-test="version-select"]').findAll('option')
    expect(opts.length).toBe(3)
    expect(opts[0].text()).toContain('默认')
    expect(opts[1].text()).toContain('v1')
    expect(opts[2].text()).toContain('v2')
    expect(opts[2].text()).toContain('当前激活')
    // 选中项 = 激活版本 2
    expect((w.find('[data-test="version-select"]').element as HTMLSelectElement).value).toBe('2')
  })

  it('选历史版本 → 调 GET versions 接口并把该版本内容载入 textarea', async () => {
    const w = await mountEditor()
    const opts = w.find('[data-test="version-select"]').findAll('option')
    await opts[1].setSelected() // 选 v1
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/prompts/video/11_smart/versions/1?scope=global')
    expect(taVal(w)).toBe('V1 HISTORICAL')
  })

  it('选「默认」→ 载入默认模板内容(不调版本接口)', async () => {
    const w = await mountEditor()
    get.mockClear()
    const opts = w.find('[data-test="version-select"]').findAll('option')
    await opts[0].setSelected() // 默认
    await flushPromises()
    expect(taVal(w)).toBe('DEFAULT TEMPLATE BODY')
    expect(get).not.toHaveBeenCalled()
  })

  it('「覆盖当前版本」→ PUT mode=overwrite', async () => {
    const w = await mountEditor()
    await w.find('textarea').setValue('EDITED')
    await btn(w, '覆盖当前版本').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/video/11_smart', {
      scope: 'global',
      domain: undefined,
      content: 'EDITED',
      mode: 'overwrite',
      note: undefined,
    })
    expect(w.emitted('saved')).toBeTruthy()
  })

  it('「另存为新版本」→ PUT mode=new 带 note', async () => {
    const w = await mountEditor()
    await w.find('textarea').setValue('NEW VER')
    await w.find('[data-test="version-note"]').setValue('加了配图要求')
    await btn(w, '另存为新版本').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/video/11_smart', {
      scope: 'global',
      domain: undefined,
      content: 'NEW VER',
      mode: 'new',
      note: '加了配图要求',
    })
  })

  it('「回到内置默认」→ POST activate{version:null}(非破坏,不 DELETE,emit changed)', async () => {
    const w = await mountEditor()
    await btn(w, '回到内置默认').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/prompts/video/11_smart/activate', {
      scope: 'global',
      domain: undefined,
      version: null,
    })
    expect(del).not.toHaveBeenCalled() // 只停用,不删历史
    expect(w.emitted('changed')).toBeTruthy()
  })

  it('「设为当前激活」选历史版本 vN → POST activate{version:vN}', async () => {
    const w = await mountEditor()
    const opts = w.find('[data-test="version-select"]').findAll('option')
    await opts[1].setSelected() // 选 v1(当前激活是 v2 → 可设激活)
    await flushPromises()
    await w.find('[data-test="set-active"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/prompts/video/11_smart/activate', {
      scope: 'global',
      domain: undefined,
      version: '1',
    })
    expect(w.emitted('changed')).toBeTruthy()
  })

  it('「设为当前激活」选默认 → POST activate{version:null}(停用回内置默认)', async () => {
    const w = await mountEditor()
    const opts = w.find('[data-test="version-select"]').findAll('option')
    await opts[0].setSelected() // 默认(当前有激活 v2 → 可停用)
    await flushPromises()
    await w.find('[data-test="set-active"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/prompts/video/11_smart/activate', {
      scope: 'global',
      domain: undefined,
      version: null,
    })
  })

  it('「设为当前激活」对已激活版本禁用(选中激活 v2 时不可点)', async () => {
    const w = await mountEditor()
    // 初始选中即激活版本 v2,已是激活态,按钮禁用
    expect((w.find('[data-test="set-active"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('无覆盖:预填默认 + 标默认 + 回到内置默认禁用 + 设为当前激活禁用(默认已是激活态)', async () => {
    mockGet({
      default_template: 'DEFAULT TEMPLATE BODY',
      default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
      override: null,
      active_version: null,
      versions: [],
    })
    const w = await mountEditor()
    expect(taVal(w)).toBe('DEFAULT TEMPLATE BODY')
    expect(w.text()).toContain('当前为默认')
    expect((btn(w, '回到内置默认').element as HTMLButtonElement).disabled).toBe(true)
    // 默认已是激活态 → 设为当前激活禁用
    expect((w.find('[data-test="set-active"]').element as HTMLButtonElement).disabled).toBe(true)
    // 首次保存:覆盖按钮文案为「保存为覆盖」
    expect(btn(w, '保存为覆盖')).toBeTruthy()
  })

  it('无覆盖但有历史(已 deactivate):选 v1 可重新激活 → POST activate{version:1}', async () => {
    mockGet({
      default_template: 'DEFAULT TEMPLATE BODY',
      default_templates: [{ name: '11_smart', content: 'DEFAULT TEMPLATE BODY' }],
      override: null,
      active_version: null,
      versions: [{ version: '1', note: '首版', created_at: 't1' }],
    })
    const w = await mountEditor()
    const opts = w.find('[data-test="version-select"]').findAll('option')
    await opts[1].setSelected() // 选 v1(历史还在,可重新激活)
    await flushPromises()
    await w.find('[data-test="set-active"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/prompts/video/11_smart/activate', {
      scope: 'global',
      domain: undefined,
      version: '1',
    })
  })

  it('领域作用域:显示领域输入,PUT 带 domain', async () => {
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
    await btn(w, '另存为新版本').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/video/11_smart', {
      scope: 'domain',
      domain: 'finance',
      content: 'FIN PROMPT',
      mode: 'new',
      note: undefined,
    })
  })

  it('领域作用域未填领域 → 阻止保存(不调 PUT)', async () => {
    const w = await mountEditor()
    const domainRadio = w.findAll('input[type="radio"]').find(
      (r) => (r.element as HTMLInputElement).value === 'domain',
    )!
    await domainRadio.setValue()
    await flushPromises()
    await w.find('textarea').setValue('X')
    await btn(w, '另存为新版本').trigger('click')
    await flushPromises()
    expect(put).not.toHaveBeenCalled()
  })

  it('Document 子类作用域贯穿读取、保存和激活请求', async () => {
    const w = await mountEditor({ pipeline: 'document', step: '05_smart', documentKind: 'whitepaper' })
    expect(get).toHaveBeenCalledWith(
      '/api/prompts/document/05_smart?scope=global&document_kind=whitepaper',
    )

    await w.find('textarea').setValue('WHITEPAPER PROMPT')
    await btn(w, '另存为新版本').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith('/api/prompts/document/05_smart', {
      scope: 'global',
      domain: undefined,
      document_kind: 'whitepaper',
      content: 'WHITEPAPER PROMPT',
      mode: 'new',
      note: undefined,
    })

    const opts = w.find('[data-test="version-select"]').findAll('option')
    await opts[1].setSelected()
    await flushPromises()
    await w.find('[data-test="set-active"]').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/prompts/document/05_smart/activate', {
      scope: 'global',
      domain: undefined,
      document_kind: 'whitepaper',
      version: '1',
    })
  })

  it('多模板步:其余变体只读展示,不混进可编辑 textarea', async () => {
    mockGet({
      default_template: 'MAIN BODY',
      default_templates: [
        { name: '11_smart', content: 'MAIN BODY' },
        { name: '11_smart.vision', content: 'VISION BODY' },
      ],
      default_system: 'SYS DEFAULT',
      override: null,
      active_version: null,
      versions: [],
    })
    const w = await mountEditor()
    expect(taVal(w)).toBe('MAIN BODY')
    expect(w.text()).toContain('VISION BODY')
    expect(w.text()).toContain('11_smart.vision')
    expect(w.text()).toContain('SYS DEFAULT')
    expect(taVal(w)).not.toContain('VISION BODY')
  })
})
