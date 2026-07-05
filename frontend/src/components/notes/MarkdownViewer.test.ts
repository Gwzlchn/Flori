// KaTeX 数学渲染:论文原文/译文含 $…$/$$…$$,渲染须出 .katex 节点而非裸 $ 文本(线上踩过:公式全裸奔)。
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))
import MarkdownViewer from './MarkdownViewer.vue'

describe('MarkdownViewer math', () => {
  it('renders inline and block LaTeX via KaTeX', () => {
    const w = mount(MarkdownViewer, {
      props: { content: '质量比 $O(N^2)$ 更优。\n\n$$\\mathrm{softmax}(QK^T)V$$\n' },
    })
    expect(w.html()).toContain('katex')
    expect(w.text()).not.toContain('$O(N^2)$')
  })

  it('invalid LaTeX degrades without throwing', () => {
    const w = mount(MarkdownViewer, { props: { content: '$\\unknowncmd{x}$' } })
    expect(w.html().length).toBeGreaterThan(0)
  })
})
