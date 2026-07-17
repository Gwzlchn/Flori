import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const fetchPipelines = vi.fn().mockResolvedValue([
  { name: 'video', key: 'video', label: '视频', content_types: ['video'], document_kinds: [], source_profiles: [], steps: [] },
  {
    name: 'document', key: 'document', label: '文档', content_types: ['document'],
    document_kinds: ['research_paper', 'article', 'whitepaper'],
    source_profiles: ['scholarly_html', 'generic_html', 'digital_pdf', 'scanned_pdf'],
    steps: [{ key: '02_parse', label: '结构化解析', pool: 'cpu', needs: [] }],
  },
  { name: 'audio', key: 'audio', label: '音频', content_types: ['audio'], document_kinds: [], source_profiles: [], steps: [] },
])
vi.mock('../stores/workers', () => ({ useWorkerStore: () => ({ fetchPipelines }) }))
vi.mock('../constants/sources', async (original) => {
  const actual = await original<typeof import('../constants/sources')>()
  return {
    ...actual,
    ensureSourceCatalog: vi.fn().mockResolvedValue(undefined),
    DOCUMENT_KIND_LABELS: { research_paper: '论文', article: '文章', whitepaper: '白皮书' },
    SOURCE_PROFILE_LABELS: {
      scholarly_html: '学术 HTML', generic_html: '网页 HTML', digital_pdf: '数字 PDF', scanned_pdf: '扫描 PDF',
    },
  }
})
import AboutView from './AboutView.vue'

describe('AboutView', () => {
  it('按 metadata 渲染三条内容族与 Document 体裁/来源能力', async () => {
    setActivePinia(createPinia())
    const w = mount(AboutView)
    await flushPromises()
    const t = w.text()
    expect(t).toContain('关于 Flori')
    expect(t).toContain('原始 / 机械材料')
    expect(t).toContain('智能版')
    expect(t).toContain('核心循环')
    expect(t).toContain('内容处理流水线')
    expect(t).toContain('视频')
    expect(t).toContain('文档')
    expect(t).toContain('音频')
    expect(t).toContain('论文、文章、白皮书')
    expect(t).toContain('学术 HTML、网页 HTML、数字 PDF、扫描 PDF')
    expect(t).not.toContain('四条内容流水线')
    expect(t).toContain('能力成熟度')
    expect(t).toContain('first-pass')
    expect(t).toContain('未开始')
    expect(t).toContain('证据型自动卡片')
    expect(t).not.toContain('以上为后续里程碑，尚未构建')
  })
})
