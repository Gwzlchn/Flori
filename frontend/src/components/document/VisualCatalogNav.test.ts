import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import VisualCatalogNav from './VisualCatalogNav.vue'

const figures = [{ id: 'F1', kind: 'figure' as const, label: '图 1', caption: '架构图', order: 1 }]
const tables = [{ id: 'T1', kind: 'table' as const, label: '表 1', caption: '结果表', order: 2 }]

let wrapper: VueWrapper | null = null

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('VisualCatalogNav', () => {
  it('桌面目录按图/表分组并标记当前位置', () => {
    wrapper = mount(VisualCatalogNav, { props: { figures, tables, activeId: 'T1' }, global: { stubs: { Teleport: true } } })
    const nav = wrapper.get('.visual-catalog-desktop')
    expect(nav.attributes('aria-label')).toBe('图表目录')
    expect(nav.text()).toContain('图 1')
    expect(nav.text()).toContain('表 1')
    expect(nav.get('[aria-current="location"]').text()).toContain('表 1')
  })

  it('移动端按钮声明展开状态，drawer 支持选择与 Escape 关闭', async () => {
    wrapper = mount(VisualCatalogNav, { attachTo: document.body, props: { figures, tables, activeId: '' }, global: { stubs: { Teleport: true } } })
    const toggle = wrapper.get('.visual-catalog-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('[role="dialog"]').attributes('aria-modal')).toBe('true')

    const mobileTable = wrapper.findAll('.visual-catalog-drawer .visual-nav-item').find((button) => button.text().includes('表 1'))!
    await mobileTable.trigger('click')
    expect(wrapper.emitted('select')?.[0]).toEqual(['T1'])
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    await toggle.trigger('click')
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(toggle.element)
  })

  it('drawer 用 Tab 在首尾焦点之间循环', async () => {
    wrapper = mount(VisualCatalogNav, { attachTo: document.body, props: { figures, tables, activeId: '' }, global: { stubs: { Teleport: true } } })
    await wrapper.get('.visual-catalog-toggle').trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    const focusable = dialog.findAll<HTMLButtonElement>('button')
    const first = focusable[0].element
    const last = focusable[focusable.length - 1].element

    last.focus()
    await dialog.trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    first.focus()
    await dialog.trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })
})
