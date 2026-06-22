import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const get = vi.fn()
const post = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../composables/useApi', () => ({ useApi: () => ({ get, post, put, del }) }))

import { useDomainStore } from './domains'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('useDomainStore', () => {
  it('初始 state: domains 空、loading false', () => {
    const store = useDomainStore()
    expect(store.domains).toEqual([])
    expect(store.loading).toBe(false)
  })

  it('fetchAll: GET /api/domains 并解包 .domains 写入 state', async () => {
    const list = [{ domain: 'ml' }, { domain: 'web' }]
    get.mockResolvedValueOnce({ domains: list })
    const store = useDomainStore()
    await store.fetchAll()
    expect(get).toHaveBeenCalledWith('/api/domains')
    expect(store.domains).toEqual(list)
    expect(store.loading).toBe(false)
  })

  it('fetchAll: 失败时 loading 仍归位 false', async () => {
    get.mockRejectedValueOnce(new Error('boom'))
    const store = useDomainStore()
    await expect(store.fetchAll()).rejects.toThrow('boom')
    expect(store.loading).toBe(false)
  })

  it('workspace: GET 聚合路径(对 domain 编码)', async () => {
    get.mockResolvedValueOnce({ domain: 'a b' })
    const store = useDomainStore()
    const res = await store.workspace('a b')
    expect(get).toHaveBeenCalledWith('/api/domains/a%20b')
    expect(res).toEqual({ domain: 'a b' })
  })

  it('term: GET 术语详情(domain + term 双重编码)', async () => {
    get.mockResolvedValueOnce({ term: 'x/y' })
    const store = useDomainStore()
    await store.term('ml', 'x/y')
    expect(get).toHaveBeenCalledWith('/api/domains/ml/terms/x%2Fy')
  })

  it('topic: GET 主题页路径', async () => {
    get.mockResolvedValueOnce({ topic: 't' })
    const store = useDomainStore()
    await store.topic('ml', 't t')
    expect(get).toHaveBeenCalledWith('/api/domains/ml/topics/t%20t')
  })

  it('topicConcepts: GET topic-concepts 路径', async () => {
    get.mockResolvedValueOnce([])
    const store = useDomainStore()
    const res = await store.topicConcepts('ml')
    expect(get).toHaveBeenCalledWith('/api/domains/ml/topic-concepts')
    expect(res).toEqual([])
  })

  it('create: POST payload 后刷新列表并返回新建对象', async () => {
    const payload = { domain: 'new', display_name: 'New' } as any
    const created = { domain: 'new' }
    post.mockResolvedValueOnce(created)
    get.mockResolvedValueOnce({ domains: [created] })
    const store = useDomainStore()
    const res = await store.create(payload)
    expect(post).toHaveBeenCalledWith('/api/domains', payload)
    // create 内部触发 fetchAll
    expect(get).toHaveBeenCalledWith('/api/domains')
    expect(res).toEqual(created)
    expect(store.domains).toEqual([created])
  })

  it('conceptTimeline: 默认粒度 month 拼到 query', async () => {
    get.mockResolvedValueOnce({ buckets: [] })
    const store = useDomainStore()
    await store.conceptTimeline('ml')
    expect(get).toHaveBeenCalledWith('/api/domains/ml/concept-timeline?granularity=month')
  })

  it('conceptTimeline: 显式粒度透传', async () => {
    get.mockResolvedValueOnce({ buckets: [] })
    const store = useDomainStore()
    await store.conceptTimeline('a b', 'week')
    expect(get).toHaveBeenCalledWith('/api/domains/a%20b/concept-timeline?granularity=week')
  })
})
