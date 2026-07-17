import { describe, expect, it } from 'vitest'
import type {
  ErrorWire,
  JobCreatedWire,
  PromptDetailWire,
  SourceCatalogWire,
} from './wire'

const sourceCatalog = {
  content_types: [{ type: 'video', label: '视频', pipeline: 'video', upload_extensions: ['.mp4'] }],
  job_sources: [{
    type: 'upload', label: '上传', content_types: ['video', 'document'],
    document_kinds: ['unknown'], default_document_kind: 'unknown',
    default_source_profile: null, creatable: true,
  }],
  subscription_sources: [{
    type: 'rss', label: 'RSS', group: 'rss', icon: 'rss', id_label: 'URL',
    placeholder: 'https://example.com/feed.xml', hint: 'RSS feed', home_url_template: null,
  }],
  document_kinds: [{
    kind: 'unknown', label: '待分类', description: '无法可靠判定的文档',
    note_profile: 'generic', review_profile: 'generic',
  }],
  source_profiles: [{
    profile: 'generic_html', label: '通用 HTML', capabilities: ['html'],
  }],
} satisfies SourceCatalogWire

const created = {
  job_id: 'job_video_demo', content_type: 'video', document_kind: null,
  pipeline: 'video', status: 'pending',
  created_at: '2026-07-15T00:00:00Z',
} satisfies JobCreatedWire

const error = { error: 'invalid_request', message: 'bad input' } satisfies ErrorWire

const prompt = {
  pipeline: 'video', step: '11_smart', label: '智能笔记', pool: 'ai', is_ai: true,
  locked: false,
  default_template: null, default_templates: [], default_system: null, override: null,
  active_version: '9223372036854775807',
  versions: [{ version: '9223372036854775807', note: null, created_at: '2026-07-15T00:00:00Z' }],
} satisfies PromptDetailWire

describe('generated wire typed fixtures', () => {
  it('keeps selected domain fixtures type checked', () => {
    expect(sourceCatalog.content_types[0].pipeline).toBe('video')
    expect(created.status).toBe('pending')
    expect(error.error).toBe('invalid_request')
    expect(prompt.active_version).toBe('9223372036854775807')
  })
})
