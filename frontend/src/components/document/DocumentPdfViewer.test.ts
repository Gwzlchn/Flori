import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import DocumentPdfViewer from './DocumentPdfViewer.vue'

const mocks = vi.hoisted(() => {
  const destroy = vi.fn()
  const renderCancel = vi.fn()
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel: renderCancel }))
  const getTextContent = vi.fn(async (page: number) => ({ items: [{ str: `Selectable PDF text ${page}` }], styles: {} }))
  const getPage = vi.fn(async (page: number) => ({
    getViewport: ({ scale }: { scale: number }) => ({ width: 960, height: 1280, scale }),
    getTextContent: () => getTextContent(page),
    render: () => render(page),
    page,
  }))
  const getDocument = vi.fn(() => ({
    promise: Promise.resolve({ numPages: 3, getPage, destroy }),
  }))
  const textLayerCancel = vi.fn()
  const textLayerRender = vi.fn(async function (this: { container: HTMLElement; textContentSource: { items: { str: string }[] } }) {
    for (const item of this.textContentSource.items) {
      const span = document.createElement('span')
      span.textContent = item.str
      this.container.append(span)
    }
  })
  const TextLayer = vi.fn(function (this: object, options: object) {
    return { ...options, render: textLayerRender, cancel: textLayerCancel }
  })
  return { destroy, render, renderCancel, getTextContent, getPage, getDocument, TextLayer, textLayerRender, textLayerCancel }
})

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: mocks.getDocument,
  TextLayer: mocks.TextLayer,
}))
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/pdf.worker.mjs' }))

interface ObserverRecord {
  callback: IntersectionObserverCallback
  targets: Set<Element>
}

let observers: ObserverRecord[] = []

function intersect(page: number, isIntersecting: boolean): void {
  const record = observers.find(observer => [...observer.targets].some(
    target => target.getAttribute('data-page-number') === String(page),
  ))
  const target = record && [...record.targets].find(
    element => element.getAttribute('data-page-number') === String(page),
  )
  if (!record || !target) throw new Error(`page ${page} is not observed`)
  record.callback([{ isIntersecting, target } as IntersectionObserverEntry], {} as IntersectionObserver)
}

describe('DocumentPdfViewer', () => {
  beforeEach(() => {
    observers = []
    vi.stubGlobal('IntersectionObserver', class {
      readonly record: ObserverRecord
      constructor(callback: IntersectionObserverCallback) {
        this.record = { callback, targets: new Set() }
        observers.push(this.record)
      }
      observe = (target: Element) => { this.record.targets.add(target) }
      unobserve = (target: Element) => { this.record.targets.delete(target) }
      disconnect = () => { this.record.targets.clear() }
    })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D)
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
      configurable: true,
      value: vi.fn(),
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    mocks.getDocument.mockClear()
    mocks.getPage.mockClear()
    mocks.render.mockClear()
    mocks.renderCancel.mockClear()
    mocks.destroy.mockClear()
    mocks.getTextContent.mockClear()
    mocks.TextLayer.mockClear()
    mocks.textLayerRender.mockClear()
    mocks.textLayerCancel.mockClear()
  })

  it('建立连续页流并优先渲染证据页的文字和 bbox', async () => {
    const wrapper = mount(DocumentPdfViewer, {
      props: {
        url: '/api/jobs/j/media?path=input%2Fsource.pdf',
        page: 2,
        bboxes: [[96, 128, 288, 256]],
      },
    })
    await vi.waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(2))

    expect(mocks.getDocument).toHaveBeenCalledWith(expect.objectContaining({ withCredentials: true }))
    expect(wrapper.findAll('.pdfjs-page-shell')).toHaveLength(3)
    expect(wrapper.find('[aria-label="上一页"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="下一页"]').exists()).toBe(false)
    expect(wrapper.findAll('a')).toHaveLength(1)
    expect(wrapper.get('a').text()).toBe('新窗口打开')
    expect(mocks.getTextContent).toHaveBeenCalledWith(2)
    expect(mocks.TextLayer).toHaveBeenCalledWith(expect.objectContaining({
      container: expect.any(HTMLElement),
      viewport: expect.objectContaining({ width: 960, height: 1280, scale: 1.6 }),
    }))
    expect(wrapper.get('[data-page-number="2"] .textLayer').text()).toBe('Selectable PDF text 2')
    expect(wrapper.text()).toContain('第 2 / 3 页')
    const highlight = wrapper.get('[data-page-number="2"] .pdfjs-highlight')
    expect(highlight.attributes('style')).toContain('left: 16%')
    expect(highlight.attributes('style')).toContain('top: 16%')
    wrapper.unmount()
  })

  it('滚到相邻页时懒渲染并释放离开预取窗口的旧页', async () => {
    const wrapper = mount(DocumentPdfViewer, { props: { url: '/document.pdf', page: 1 } })
    await vi.waitFor(() => expect(wrapper.get('[data-page-number="1"] .textLayer').text()).toBe('Selectable PDF text 1'))

    intersect(2, true)
    await vi.waitFor(() => expect(wrapper.get('[data-page-number="2"] .textLayer').text()).toBe('Selectable PDF text 2'))
    await wrapper.setProps({ page: 2 })
    intersect(1, false)
    await vi.waitFor(() => expect(wrapper.get('[data-page-number="1"] .textLayer').text()).toBe(''))
    expect(mocks.textLayerCancel).toHaveBeenCalled()
    expect(mocks.renderCancel).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('扫描版页面没有文本项时仍保留画布阅读', async () => {
    mocks.getTextContent.mockResolvedValueOnce({ items: [], styles: {} })
    const wrapper = mount(DocumentPdfViewer, { props: { url: '/scanned.pdf', page: 1 } })
    await vi.waitFor(() => expect(mocks.TextLayer).toHaveBeenCalledOnce())

    expect(wrapper.find('canvas').exists()).toBe(true)
    expect(wrapper.get('.textLayer').text()).toBe('')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
