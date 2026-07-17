import { describe, expect, it } from 'vitest'
import { FileText, Newspaper, Play } from 'lucide-vue-next'
import { installSourceCatalog } from '../constants/sources'
import {
  contentTypeIcon,
  contentTypeLabel,
  contentTypePill,
  documentKindIcon,
  documentKindLabel,
} from './contentType'

describe('内容族与文档体裁展示', () => {
  it('顶层只把 document 作为内容族', () => {
    installSourceCatalog({
      content_types: [
        { type: 'video', label: '视频', pipeline: 'video', upload_extensions: ['.mp4'] },
        { type: 'document', label: '文档', pipeline: 'document', upload_extensions: ['.pdf'] },
        { type: 'audio', label: '音频', pipeline: 'audio', upload_extensions: ['.mp3'] },
      ],
      job_sources: [],
      subscription_sources: [],
      document_kinds: [
        { kind: 'research_paper', label: '论文', description: '学术论文', note_profile: 'research', review_profile: 'research' },
        { kind: 'article', label: '文章', description: '网页文章', note_profile: 'article', review_profile: 'article' },
        { kind: 'unknown', label: '未分类文档', description: '未知', note_profile: 'document', review_profile: 'document' },
      ],
      source_profiles: [],
    } as any)

    expect(contentTypeIcon('video')).toBe(Play)
    expect(contentTypeIcon('document')).toBe(FileText)
    expect(contentTypeLabel('document')).toBe('文档')
    expect(contentTypePill('document')).toBe('t-document')
    expect(documentKindIcon('article')).toBe(Newspaper)
    expect(documentKindLabel('research_paper')).toBe('论文')
    expect(documentKindLabel('unknown')).toBe('未分类文档')
  })

  it('未知值使用中性文档外观且保留原始标签', () => {
    expect(contentTypeIcon('future')).toBe(FileText)
    expect(contentTypePill('future')).toBe('t-document')
    expect(contentTypeLabel('future')).toBe('future')
    expect(documentKindIcon('future_kind')).toBe(FileText)
    expect(documentKindLabel('future_kind')).toBe('future_kind')
  })
})
