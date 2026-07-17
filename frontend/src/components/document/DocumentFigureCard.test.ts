import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import DocumentFigureCard from './DocumentFigureCard.vue'
import type { DocumentFigure } from './types'

const locator = {
  source_fingerprint: `sha256:${'c'.repeat(64)}`,
  pdf: { page: 2, bboxes: [] as [number, number, number, number][] },
}

let wrapper: VueWrapper | null = null

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('DocumentFigureCard', () => {
  it('保留多 panel 图并为每张图提供语义化替代文本', () => {
    const figure: DocumentFigure = {
      figure_id: 'F2', label: '图 2', caption: '消融实验', source_locator: locator,
      media: [
        { media_id: 'F2.left', role: '左图', artifact: 'figures/left.png' },
        { media_id: 'F2.right', role: '右图', artifact: 'figures/right.png', alt: '精度曲线' },
      ],
    }
    wrapper = mount(DocumentFigureCard, {
      props: { figure, assetUrl: (path) => `/assets/${path}` },
    })

    const images = wrapper.findAll('.figure-panel img')
    expect(images).toHaveLength(2)
    expect(images[0].attributes('alt')).toContain('图 2 · 左图 · 消融实验')
    expect(images[1].attributes('alt')).toBe('精度曲线')
  })

  it('预览 dialog 打开后接管焦点，关闭后归还触发按钮', async () => {
    const figure: DocumentFigure = {
      figure_id: 'F1', label: '图 1', caption: '系统架构', source_locator: locator,
      media: [{ media_id: 'F1.main', artifact: 'figures/F1.png' }],
    }
    wrapper = mount(DocumentFigureCard, {
      attachTo: document.body,
      props: { figure, assetUrl: (path) => `/assets/${path}` },
    })
    const trigger = wrapper.get('.figure-zoom')

    await trigger.trigger('click')
    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
    const close = dialog.querySelector<HTMLButtonElement>('[aria-label="关闭图像预览"]')!
    expect(document.activeElement).toBe(close)
    expect(dialog.getAttribute('aria-modal')).toBe('true')

    close.blur()
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(close)

    close.click()
    await wrapper.vm.$nextTick()
    expect(document.activeElement).toBe(trigger.element)
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
  })
})
