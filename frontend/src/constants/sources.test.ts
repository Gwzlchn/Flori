import { describe, expect, it } from 'vitest'
import {
  CONTENT_TYPE_LABELS,
  JOB_SOURCE_LABELS,
  SOURCE_TYPES,
  installSourceCatalog,
  jobSourceLabel,
  sourceHomeUrl,
  sourceMeta,
  uploadAccept,
} from './sources'


describe('source catalog', () => {
  it('完整保留后端返回的任意来源,不依赖前端枚举', () => {
    installSourceCatalog({
      content_types: [
        { type: 'article', label: '文章', upload_extensions: ['.html', '.md'] },
        { type: 'paper', label: '论文', upload_extensions: ['.pdf'] },
      ],
      job_sources: [{ type: 'future_source', label: '未来来源' }],
      subscription_sources: [{
        type: 'book_toc', label: '在线书目录', group: 'book', icon: 'book-open',
        id_label: '目录页 URL', placeholder: 'https://book.example/index.html',
        hint: '按目录顺序入库。', home_url_template: '{source_id}',
      }, {
        type: 'youtube_playlist', label: 'YouTube 播放列表', group: 'youtube', icon: 'list-video',
        id_label: '播放列表链接 / ID', placeholder: 'https://youtube.com/playlist?list=PL...',
        hint: '逐视频入库。',
        home_url_template: 'https://www.youtube.com/playlist?list={source_id}',
      }],
    })

    expect(SOURCE_TYPES.map((item) => item.type)).toEqual(['book_toc', 'youtube_playlist'])
    expect(sourceMeta('book_toc')?.label).toBe('在线书目录')
    expect(JOB_SOURCE_LABELS.future_source).toBe('未来来源')
    expect(CONTENT_TYPE_LABELS.article).toBe('文章')
    expect(jobSourceLabel('future_source')).toBe('未来来源')
    expect(uploadAccept()).toBe('.html,.md,.pdf')
    expect(sourceHomeUrl({
      source_type: 'book_toc', source_id: 'https://book.example/index.html',
    })).toBe('https://book.example/index.html')
    expect(sourceHomeUrl({
      source_type: 'youtube_playlist', source_id: 'PLabc_123-xyz',
    })).toBe('https://www.youtube.com/playlist?list=PLabc_123-xyz')
  })
})
