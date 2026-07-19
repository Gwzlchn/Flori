import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const get = vi.fn()
const post = vi.fn()
const del = vi.fn()
const upload = vi.fn()

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get, post, del, upload }),
}))

import { useJobStore } from './jobs'
import { installSourceCatalog } from '../constants/sources'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  installSourceCatalog({
    subscription_sources: [], job_sources: [],
    content_types: [
      { type: 'video', label: '视频', upload_extensions: [] },
      { type: 'document', label: '文档', upload_extensions: ['.txt'] },
    ],
  })
})

function makeListResponse(n: number, total = 100) {
  return {
    total,
    items: Array.from({ length: n }, (_, i) => ({
      job_id: `job-${i}`,
      content_type: 'video',
      status: 'done',
      created_at: '2026-01-01',
      title: `t${i}`,
      progress_pct: 100,
      source: null,
      domain: 'tech',
      collection_id: null,
    })),
  }
}

describe('useJobStore.fetchList', () => {
  it('默认参数:limit=20 offset=0,无过滤,replace 列表并写 total', async () => {
    get.mockResolvedValue(makeListResponse(2, 42))
    const store = useJobStore()
    await store.fetchList()

    expect(get).toHaveBeenCalledTimes(1)
    const url = get.mock.calls[0][0] as string
    expect(url.startsWith('/api/jobs?')).toBe(true)
    const qs = new URLSearchParams(url.split('?')[1])
    expect(qs.get('limit')).toBe('20')
    expect(qs.get('offset')).toBe('0')
    expect(qs.get('status')).toBeNull()
    expect(qs.get('domain')).toBeNull()
    expect(qs.get('source')).toBeNull()
    expect(qs.get('collection_id')).toBeNull()

    expect(store.list).toHaveLength(2)
    expect(store.total).toBe(42)
  })

  it('带过滤参数全部进 query-string', async () => {
    get.mockResolvedValue(makeListResponse(1))
    const store = useJobStore()
    await store.fetchList({
      status: 'running',
      domain: 'tech',
      source: 'bilibili',
      collection_id: 'col-1',
      limit: 5,
      offset: 10,
    })

    const url = get.mock.calls[0][0] as string
    const qs = new URLSearchParams(url.split('?')[1])
    expect(qs.get('status')).toBe('running')
    expect(qs.get('domain')).toBe('tech')
    expect(qs.get('source')).toBe('bilibili')
    expect(qs.get('collection_id')).toBe('col-1')
    expect(qs.get('limit')).toBe('5')
    expect(qs.get('offset')).toBe('10')
  })

  it('append=false(默认)替换列表', async () => {
    const store = useJobStore()
    get.mockResolvedValueOnce(makeListResponse(2))
    await store.fetchList()
    expect(store.list).toHaveLength(2)

    get.mockResolvedValueOnce(makeListResponse(3))
    await store.fetchList()
    expect(store.list).toHaveLength(3)
  })

  it('append=true 追加到现有列表', async () => {
    const store = useJobStore()
    get.mockResolvedValueOnce(makeListResponse(2))
    await store.fetchList()
    expect(store.list).toHaveLength(2)

    get.mockResolvedValueOnce(makeListResponse(3))
    await store.fetchList({ append: true })
    expect(store.list).toHaveLength(5)
  })

  it('loading 在成功后复位为 false', async () => {
    get.mockResolvedValue(makeListResponse(1))
    const store = useJobStore()
    expect(store.loading).toBe(false)
    await store.fetchList()
    expect(store.loading).toBe(false)
  })

  it('请求抛错时 loading 仍复位为 false 且错误冒泡', async () => {
    get.mockRejectedValue(new Error('boom'))
    const store = useJobStore()
    await expect(store.fetchList()).rejects.toThrow('boom')
    expect(store.loading).toBe(false)
  })
})

describe('useJobStore 其它 action', () => {
  it('fetchDetail 命中 /api/jobs/:id 并透传返回', async () => {
    get.mockResolvedValue({ job_id: 'j1' })
    const store = useJobStore()
    const res = await store.fetchDetail('j1')
    expect(get).toHaveBeenCalledWith('/api/jobs/j1')
    expect(res).toEqual({ job_id: 'j1' })
  })

  it('createJob POST /api/jobs 带 payload', async () => {
    post.mockResolvedValue({ job_id: 'new' })
    const store = useJobStore()
    const payload = {
      content_type: 'video' as const,
      parts: [{ url: 'http://x' }],
      domain: 'tech',
    }
    const res = await store.createJob(payload)
    expect(post).toHaveBeenCalledWith('/api/jobs', payload)
    expect(res).toEqual({ job_id: 'new' })
  })

  it('uploadJob 走 upload 且透传 Document 子类,FormData 含 file/domain/style_tags(JSON)', async () => {
    upload.mockResolvedValue({ job_id: 'up' })
    const store = useJobStore()
    const file = new File(['x'], 'a.txt', { type: 'text/plain' })
    await store.uploadJob(file, 'tech', ['a', 'b'], 'whitepaper')

    expect(upload).toHaveBeenCalledTimes(1)
    const [path, form] = upload.mock.calls[0]
    const parsed = new URL(`http://local${path}`)
    expect(parsed.pathname).toBe('/api/jobs/upload')
    expect(parsed.searchParams.get('content_type')).toBe('document')
    expect(parsed.searchParams.get('document_kind')).toBe('whitepaper')
    expect(form).toBeInstanceOf(FormData)
    expect((form as FormData).get('domain')).toBe('tech')
    expect((form as FormData).get('style_tags')).toBe(JSON.stringify(['a', 'b']))
    expect((form as FormData).get('file')).toBeInstanceOf(File)
  })

  it('uploadJob 纯机械模式写入 query', async () => {
    upload.mockResolvedValue({ job_id: 'up' })
    const store = useJobStore()
    const file = new File(['x'], 'a.txt', { type: 'text/plain' })

    await store.uploadJob(file, 'tech', [], undefined, true)

    const path = upload.mock.calls[0][0] as string
    expect(new URL(`http://local${path}`).searchParams.get('mechanical_only')).toBe('true')
  })

  it('uploadJob 不接受目录中意外残留的视频扩展名', async () => {
    installSourceCatalog({
      subscription_sources: [], job_sources: [],
      content_types: [{ type: 'video', label: '视频', upload_extensions: ['.mp4'] }],
    })
    const file = new File(['x'], 'a.mp4', { type: 'video/mp4' })

    await expect(useJobStore().uploadJob(file, 'tech', [])).rejects.toThrow(
      '视频请使用单/多 Part 投递',
    )
    expect(upload).not.toHaveBeenCalled()
  })

  it('retryJob POST /api/jobs/:id/retry', async () => {
    post.mockResolvedValue({})
    const store = useJobStore()
    await store.retryJob('j1')
    expect(post).toHaveBeenCalledWith('/api/jobs/j1/retry')
  })

  it('activateJob POST /api/jobs/:id/activate', async () => {
    post.mockResolvedValue({ job_id: 'j1', status: 'pending' })
    const store = useJobStore()
    await store.activateJob('j1')
    expect(post).toHaveBeenCalledWith('/api/jobs/j1/activate')
  })

  it('rerunJob POST /api/jobs/:id/rerun 带 from_step', async () => {
    post.mockResolvedValue({})
    const store = useJobStore()
    await store.rerunJob('j1', 'extract')
    expect(post).toHaveBeenCalledWith('/api/jobs/j1/rerun', { from_step: 'extract' })
  })

  it('continueAi POST /api/jobs/:id/continue-ai', async () => {
    post.mockResolvedValue({})
    const store = useJobStore()
    await store.continueAi('j1')
    expect(post).toHaveBeenCalledWith('/api/jobs/j1/continue-ai')
  })

  it('rebuildJob 可显式切纯机械并传幂等键和分叉步', async () => {
    post.mockResolvedValue({ job_id: 'j2' })
    const store = useJobStore()
    const options = {
      mechanical_only: true, from_step: '02_parse', idempotency_key: 'u18-paper',
    }
    await store.rebuildJob('j1', options)
    expect(post).toHaveBeenCalledWith('/api/jobs/j1/rebuild', options)
  })

  it('deleteJob DELETE /api/jobs/:id', async () => {
    del.mockResolvedValue(undefined)
    const store = useJobStore()
    await store.deleteJob('j1')
    expect(del).toHaveBeenCalledWith('/api/jobs/j1')
  })

  it('fetchFacets GET /api/jobs/facets', async () => {
    get.mockResolvedValue({})
    const store = useJobStore()
    await store.fetchFacets()
    expect(get).toHaveBeenCalledWith('/api/jobs/facets')
  })

  it('fetchConcepts 对 jobId 做 encodeURIComponent', async () => {
    get.mockResolvedValue([])
    const store = useJobStore()
    await store.fetchConcepts('a/b c')
    expect(get).toHaveBeenCalledWith('/api/jobs/a%2Fb%20c/concepts')
  })
})
