import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const get = vi.fn()
const post = vi.fn()
const put = vi.fn()
const del = vi.fn()

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ get, post, put, del }),
}))

import { useCollectionStore } from './collections'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

function makeCollection(id: string) {
  return {
    id,
    name: `c-${id}`,
    domain: 'tech',
    description: '',
    tags: [],
    job_count: 0,
    created_at: '2026-01-01',
    subscription: null,
  }
}

describe('useCollectionStore.fetchAll', () => {
  it('无 domain:GET /api/collections(无 query),写入 collections', async () => {
    get.mockResolvedValue([makeCollection('a'), makeCollection('b')])
    const store = useCollectionStore()
    await store.fetchAll()
    expect(get).toHaveBeenCalledWith('/api/collections')
    expect(store.collections).toHaveLength(2)
  })

  it('带 domain:作为 encodeURIComponent 后的 query', async () => {
    get.mockResolvedValue([])
    const store = useCollectionStore()
    await store.fetchAll('a/b')
    expect(get).toHaveBeenCalledWith('/api/collections?domain=a%2Fb')
  })

  it('loading 成功后复位 false', async () => {
    get.mockResolvedValue([])
    const store = useCollectionStore()
    expect(store.loading).toBe(false)
    await store.fetchAll()
    expect(store.loading).toBe(false)
  })

  it('出错时 loading 仍复位且错误冒泡', async () => {
    get.mockRejectedValue(new Error('boom'))
    const store = useCollectionStore()
    await expect(store.fetchAll()).rejects.toThrow('boom')
    expect(store.loading).toBe(false)
  })
})

describe('useCollectionStore.get', () => {
  it('GET /api/collections/:id 并透传', async () => {
    const c = makeCollection('x')
    get.mockResolvedValue(c)
    const store = useCollectionStore()
    const res = await store.get('x')
    expect(get).toHaveBeenCalledWith('/api/collections/x')
    expect(res).toEqual(c)
  })
})

describe('useCollectionStore.create', () => {
  it('POST /api/collections 带 payload,随后 fetchAll 刷新列表', async () => {
    const created = makeCollection('new')
    post.mockResolvedValue(created)
    get.mockResolvedValue([created])
    const store = useCollectionStore()
    const payload = { name: 'n', domain: 'tech' }
    const res = await store.create(payload)

    expect(post).toHaveBeenCalledWith('/api/collections', payload)
    // create 后会调用 fetchAll() -> GET /api/collections
    expect(get).toHaveBeenCalledWith('/api/collections')
    expect(res).toEqual(created)
    expect(store.collections).toHaveLength(1)
  })
})

describe('useCollectionStore.update', () => {
  it('PUT /api/collections/:id 带 payload,随后 fetchAll', async () => {
    const updated = makeCollection('x')
    put.mockResolvedValue(updated)
    get.mockResolvedValue([updated])
    const store = useCollectionStore()
    const res = await store.update('x', { name: 'renamed' })

    expect(put).toHaveBeenCalledWith('/api/collections/x', { name: 'renamed' })
    expect(get).toHaveBeenCalledWith('/api/collections')
    expect(res).toEqual(updated)
  })
})

describe('useCollectionStore.remove', () => {
  it('DELETE /api/collections/:id,随后 fetchAll', async () => {
    del.mockResolvedValue(undefined)
    get.mockResolvedValue([])
    const store = useCollectionStore()
    await store.remove('x')

    expect(del).toHaveBeenCalledWith('/api/collections/x')
    expect(get).toHaveBeenCalledWith('/api/collections')
  })
})

describe('useCollectionStore.fetchJobs', () => {
  it('默认 limit=20 offset=0 拼到 jobs query', async () => {
    get.mockResolvedValue({ total: 0, items: [] })
    const store = useCollectionStore()
    await store.fetchJobs('x')
    expect(get).toHaveBeenCalledWith('/api/collections/x/jobs?limit=20&offset=0')
  })

  it('自定义 limit/offset', async () => {
    const resp = { total: 5, items: [] }
    get.mockResolvedValue(resp)
    const store = useCollectionStore()
    const res = await store.fetchJobs('x', 50, 100)
    expect(get).toHaveBeenCalledWith('/api/collections/x/jobs?limit=50&offset=100')
    expect(res).toEqual(resp)
  })
})
