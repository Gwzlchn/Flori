import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import DocumentPdfViewer from './DocumentPdfViewer.vue'

const mocks = vi.hoisted(() => {
  const destroy = vi.fn()
  const render = vi.fn(() => ({ promise: Promise.resolve() }))
  const getPage = vi.fn(async () => ({
    getViewport: ({ scale }: { scale: number }) => ({ width: 960, height: 1280, scale }),
    render,
  }))
  const getDocument = vi.fn(() => ({
    promise: Promise.resolve({ numPages: 3, getPage, destroy }),
  }))
  return { destroy, render, getPage, getDocument }
})

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: mocks.getDocument,
}))
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/pdf.worker.mjs' }))

describe('DocumentPdfViewer', () => {
  beforeEach(() => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    mocks.getDocument.mockClear()
    mocks.getPage.mockClear()
    mocks.render.mockClear()
    mocks.destroy.mockClear()
  })

  it('用 PDF.js 渲染指定页并按 bbox 叠加证据高亮', async () => {
    const wrapper = mount(DocumentPdfViewer, {
      props: {
        url: '/api/jobs/j/media?path=input%2Fsource.pdf',
        page: 2,
        bboxes: [[96, 128, 288, 256]],
      },
    })
    await vi.waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(2))

    expect(mocks.getDocument).toHaveBeenCalledWith(expect.objectContaining({ withCredentials: true }))
    expect(wrapper.text()).toContain('第 2 / 3 页')
    const highlight = wrapper.get('.pdfjs-highlight')
    expect(highlight.attributes('style')).toContain('left: 16%')
    expect(highlight.attributes('style')).toContain('top: 16%')
    wrapper.unmount()
  })

  it('支持上一页和下一页且不会越界', async () => {
    const wrapper = mount(DocumentPdfViewer, { props: { url: '/document.pdf', page: 1 } })
    await vi.waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(1))
    expect(wrapper.get('[aria-label="上一页"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="下一页"]').trigger('click')
    await vi.waitFor(() => expect(mocks.getPage).toHaveBeenCalledWith(2))
    expect(wrapper.text()).toContain('第 2 / 3 页')
    wrapper.unmount()
  })
})
