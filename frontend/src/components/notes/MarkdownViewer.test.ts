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

// 术语链接(09 工单 P3):大小写不敏感 + zh_name/aliases 双语命中,统一链到实体主名。
describe('MarkdownViewer term links', () => {
  const ENTITY = { term: 'Kelly Criterion', zh_name: '凯利准则', aliases: ['kelly formula'] }

  function mountTerms(content: string, terms: any[] = [ENTITY]) {
    return mount(MarkdownViewer, { props: { content, jobId: '', terms, domain: 'ml' } })
  }

  it('大小写不敏感命中,data-term 指向主名', () => {
    const w = mountTerms('这篇讲 kelly criterion 的应用。')
    const a = w.find('a.term-link')
    expect(a.exists()).toBe(true)
    expect(a.attributes('data-term')).toBe('Kelly Criterion')
    expect(a.text()).toBe('kelly criterion')   // 展示保留原文写法
  })

  it('zh_name 中文说法同样命中同一实体', () => {
    const w = mountTerms('本文推导了凯利准则的最优下注比例。')
    const a = w.find('a.term-link')
    expect(a.exists()).toBe(true)
    expect(a.attributes('data-term')).toBe('Kelly Criterion')
  })

  it('同一实体的多个变体只链首次出现', () => {
    const w = mountTerms('先讲 Kelly Criterion,再讲凯利准则,最后讲 kelly formula。')
    expect(w.findAll('a.term-link').length).toBe(1)
  })

  it('纯 ASCII 术语按词边界匹配,不命中单词内部', () => {
    const w = mountTerms('shai said something', [{ term: 'AI', zh_name: '', aliases: [] }])
    expect(w.find('a.term-link').exists()).toBe(false)
  })

  it('裸字符串 terms(旧用法)仍可用', () => {
    const w = mountTerms('注意力机制 很重要。', ['注意力机制'])
    expect(w.find('a.term-link').attributes('data-term')).toBe('注意力机制')
  })
})

describe('MarkdownViewer evidence citations', () => {
  it('only eligible ids become buttons and emit navigation', async () => {
    const w = mount(MarkdownViewer, {
      props: { content: '命中 123 [E1]，未知 [E2]。', jobId: 'j', evidenceIds: ['E1'] },
    })
    const button = w.find('button.evidence-citation')
    expect(button.exists()).toBe(true)
    expect(button.attributes('data-evidence-id')).toBe('E1')
    expect(w.findAll('button.evidence-citation')).toHaveLength(1)
    expect(w.text()).toContain('[E2]')
    await button.trigger('click')
    expect(w.emitted('evidenceCitation')).toEqual([['E1']])
  })

  it('legacy or ineligible ids remain plain text', () => {
    const w = mount(MarkdownViewer, { props: { content: '来源 [E1]', jobId: 'j', evidenceIds: [] } })
    expect(w.find('button.evidence-citation').exists()).toBe(false)
    expect(w.text()).toContain('[E1]')
  })
})

describe('MarkdownViewer canonical evidence', () => {
  const valid = {
    evidence_id: `ce_${'6'.repeat(64)}`, status: 'valid' as const, reason: null,
    job_id: 'j', note_type: 'smart', chunk_id: 'j:smart:0', section: '第一节',
    evidence_fingerprint: 'a'.repeat(64), source_fingerprint: 'b'.repeat(64),
    locator: { kind: 'image' as const, bbox: [0, 0, 10, 20] as [number, number, number, number], start_ms: null, end_ms: null, page: 2 },
    link: { kind: 'image' as const, href: `/api/evidence/ce_${'6'.repeat(64)}/open`, label: '第 2 页图像区域' },
    validated_at: '2026-07-14T14:00:00Z',
  }

  it('正文下只展示resolver链接,不暴露或拼接图像路径', () => {
    const w = mount(MarkdownViewer, {
      props: { content: '正文', jobId: 'j', canonicalEvidence: [valid] },
    })
    expect(w.get('.canonical-evidence-list a').attributes('href')).toBe(valid.link.href)
    expect(w.html()).not.toContain('asset_path')
  })

  it('missing 只显示不可用状态', () => {
    const missing = { ...valid, status: 'missing' as const, reason: 'not_found', locator: null, link: null }
    const w = mount(MarkdownViewer, {
      props: { content: '正文', jobId: 'j', canonicalEvidence: [missing] },
    })
    expect(w.find('.canonical-evidence-list a').exists()).toBe(false)
    expect(w.text()).toContain('证据缺失')
  })
})
