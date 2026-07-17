import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const navigation = vi.hoisted(() => ({
  route: { query: {} as Record<string, string> },
  replace: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => navigation.route,
  useRouter: () => ({ replace: navigation.replace }),
}))

import DocumentVisualsPanel from './DocumentVisualsPanel.vue'
import type { DocumentFigure, DocumentTable } from './types'

class IntersectionObserverMock {
  static instances: IntersectionObserverMock[] = []
  callback: IntersectionObserverCallback
  targets = new Set<Element>()

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
    IntersectionObserverMock.instances.push(this)
  }

  observe = vi.fn((target: Element) => this.targets.add(target))
  unobserve = vi.fn((target: Element) => this.targets.delete(target))
  disconnect = vi.fn(() => this.targets.clear())
  takeRecords = vi.fn(() => [])

  trigger(target: Element, ratio = 1): void {
    this.callback([{
      target,
      isIntersecting: ratio > 0,
      intersectionRatio: ratio,
      boundingClientRect: target.getBoundingClientRect(),
      intersectionRect: target.getBoundingClientRect(),
      rootBounds: null,
      time: 0,
    } as IntersectionObserverEntry], this as any)
  }
}

const locator = {
  source_fingerprint: `sha256:${'a'.repeat(64)}`,
  pdf: { page: 1, bboxes: [] as [number, number, number, number][] },
}
const figures: DocumentFigure[] = [{
  figure_id: 'F1', label: '图 1', caption: '无可用图片', source_locator: locator, order: 4, media: [],
  extraction: { status: 'degraded', reasons: ['asset_missing'] },
}, {
  figure_id: 'F2', label: '图 2', caption: '左右两个子图', source_locator: locator, order: 2,
  media: [
    { media_id: 'F2.left', role: '左图', artifact: 'left.png' },
    { media_id: 'F2.right', role: '右图', artifact: 'right.png' },
  ],
}]
const tables: DocumentTable[] = [{
  table_id: 'T1', label: '表 1', caption: '结构化结果', source_locator: locator, order: 3,
  cells: [
    { cell_id: 'h', row: 0, col: 0, role: 'column_header', text: '模型' },
    { cell_id: 'd', row: 1, col: 0, role: 'data', text: 'Flori' },
  ],
  representations: [{ kind: 'structured', artifact: null }],
  extraction: { status: 'complete' },
}]

function mountPanel() {
  return mount(DocumentVisualsPanel, {
    props: {
      figures,
      tables,
      quality: { status: 'degraded' as const, reasons: ['table_crop_fallback'] },
      assetUrl: (artifact: string) => `/assets/${artifact}`,
      sourceUrl: (id: string) => `/source?visual=${id}`,
    },
    global: { stubs: { Teleport: true } },
  })
}

beforeEach(() => {
  navigation.route.query = {}
  navigation.replace.mockReset()
  IntersectionObserverMock.instances = []
  vi.stubGlobal('IntersectionObserver', IntersectionObserverMock as any)
  HTMLElement.prototype.scrollIntoView = vi.fn()
  HTMLElement.prototype.getBoundingClientRect = vi.fn(function (this: HTMLElement) {
    const order = this.dataset.visualId === 'F1' ? 100 : this.dataset.visualId === 'F2' ? 200 : 300
    return { x: 0, y: order, top: order, left: 0, right: 600, bottom: order + 100, width: 600, height: 100, toJSON: () => ({}) }
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('DocumentVisualsPanel', () => {
  it('图表分组完整保留零 media Figure、多 panel Figure 与 Table', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.get('.document-visuals-head').text()).toContain('图 2 · 表 1')
    expect(wrapper.findAll('.figure-card')).toHaveLength(2)
    expect(wrapper.findAll('.table-card')).toHaveLength(1)
    expect(wrapper.get('.figure-card .visual-missing').text()).toContain('原始图像不可用')
    expect(wrapper.get('[data-visual-id="F2"]').findAll('img')).toHaveLength(2)
    expect(wrapper.get('.visual-catalog-desktop').text()).toContain('图 2')
    expect(wrapper.get('.visual-catalog-desktop').text()).toContain('表 1')
    expect(wrapper.get('.document-quality-reasons').text()).toContain('table_crop_fallback')
    expect(wrapper.findAll('.document-visual-list > [data-visual-id]').map(item => item.attributes('data-visual-id')))
      .toEqual(['F2', 'F1', 'T1'])
    expect(wrapper.findAll('.visual-catalog-desktop .visual-nav-item').map(item => item.get('b').text()))
      .toEqual(['图 2', '图 1', '表 1'])
  })

  it('稳定 query 深链等待 registry 后定位且目录点击只 replace 当前 URL', async () => {
    navigation.route.query = { tab: 'figures', visual: 'T1', from: 'ask' }
    const wrapper = mountPanel()
    await flushPromises()

    const table = wrapper.get('[data-visual-id="T1"]')
    expect(table.element.scrollIntoView).toHaveBeenCalled()
    expect(wrapper.get('.visual-catalog-desktop [aria-current="location"]').text()).toContain('表 1')

    const figureButton = wrapper.findAll('.visual-catalog-desktop .visual-nav-item')
      .find((button) => button.text().includes('图 2'))!
    await figureButton.trigger('click')
    await flushPromises()
    expect(navigation.replace).toHaveBeenCalledWith({
      query: { tab: 'figures', visual: 'F2', from: 'ask' },
    })
    expect(wrapper.get('[data-visual-id="F2"]').element.scrollIntoView).toHaveBeenCalled()
  })

  it('scroll-spy 只接受 registry 内元素并更新 aria-current', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const observer = IntersectionObserverMock.instances[0]
    const figure = wrapper.get('[data-visual-id="F2"]').element
    observer.trigger(figure, 1)
    await flushPromises()

    expect(navigation.replace).toHaveBeenCalledWith({ query: { tab: 'figures', visual: 'F2' } })
    expect(wrapper.get('.visual-catalog-desktop [aria-current="location"]').text()).toContain('图 2')
  })

  it('目录平滑滚动期间不被途经的图表抢占选中状态', async () => {
    vi.useFakeTimers()
    const wrapper = mountPanel()
    await flushPromises()
    const observer = IntersectionObserverMock.instances[0]
    const tableButton = wrapper.findAll('.visual-catalog-desktop .visual-nav-item')
      .find((button) => button.text().includes('表 1'))!

    await tableButton.trigger('click')
    await flushPromises()
    observer.trigger(wrapper.get('[data-visual-id="F2"]').element, 1)
    await flushPromises()

    expect(navigation.replace).toHaveBeenLastCalledWith({ query: { tab: 'figures', visual: 'T1' } })
    expect(wrapper.get('.visual-catalog-desktop [aria-current="location"]').text()).toContain('表 1')
    wrapper.unmount()
  })
})
