import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const get = vi.fn()
const post = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../composables/useApi', () => ({ useApi: () => ({ get, post, put, del }) }))

import { useWorkerStore } from './workers'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('useWorkerStore', () => {
  it('初始 state: workers 空、loading false', () => {
    const store = useWorkerStore()
    expect(store.workers).toEqual([])
    expect(store.loading).toBe(false)
  })

  it('fetchAll: GET /api/workers 写入 state', async () => {
    const list = [{ id: 'w1' }, { id: 'w2' }]
    get.mockResolvedValueOnce(list)
    const store = useWorkerStore()
    await store.fetchAll()
    expect(get).toHaveBeenCalledWith('/api/workers')
    expect(store.workers).toEqual(list)
    expect(store.loading).toBe(false)
  })

  it('fetchAll: 失败时 loading 归位 false', async () => {
    get.mockRejectedValueOnce(new Error('boom'))
    const store = useWorkerStore()
    await expect(store.fetchAll()).rejects.toThrow('boom')
    expect(store.loading).toBe(false)
  })

  it('pause: PUT status=paused 后刷新', async () => {
    put.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.pause('w1')
    expect(put).toHaveBeenCalledWith('/api/workers/w1', { status: 'paused' })
    expect(get).toHaveBeenCalledWith('/api/workers')
  })

  it('resume: PUT status=active 后刷新', async () => {
    put.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.resume('w1')
    expect(put).toHaveBeenCalledWith('/api/workers/w1', { status: 'active' })
    expect(get).toHaveBeenCalledWith('/api/workers')
  })

  it('updateNote: PUT admin_note 后刷新', async () => {
    put.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.updateNote('w1', 'hello')
    expect(put).toHaveBeenCalledWith('/api/workers/w1', { admin_note: 'hello' })
    expect(get).toHaveBeenCalledWith('/api/workers')
  })

  it('updateTags: PUT tags 后刷新', async () => {
    put.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.updateTags('w1', ['gpu', 'fast'])
    expect(put).toHaveBeenCalledWith('/api/workers/w1', { tags: ['gpu', 'fast'] })
    expect(get).toHaveBeenCalledWith('/api/workers')
  })

  it('remove: 默认 DELETE 无 force 查询', async () => {
    del.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.remove('w1')
    expect(del).toHaveBeenCalledWith('/api/workers/w1')
    expect(get).toHaveBeenCalledWith('/api/workers')
  })

  it('remove: force=true 拼 ?force=true', async () => {
    del.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.remove('w1', true)
    expect(del).toHaveBeenCalledWith('/api/workers/w1?force=true')
  })

  it('setConfig: 只提交 concurrency 后刷新', async () => {
    put.mockResolvedValueOnce(undefined)
    get.mockResolvedValueOnce([])
    const store = useWorkerStore()
    await store.setConfig('w1', { concurrency: 3 })
    expect(put).toHaveBeenCalledWith('/api/workers/w1/config', { concurrency: 3 })
    expect(get).toHaveBeenCalledWith('/api/workers')
  })

  it('mintToken: POST registration-token 返回 token 和有效期', async () => {
    const sampleToken = 'tok-' + '123'
    post.mockResolvedValueOnce({ token: sampleToken, expires_in_sec: 3600 })
    const store = useWorkerStore()
    const res = await store.mintToken()
    expect(post).toHaveBeenCalledWith('/api/workers/registration-token', {})
    expect(res).toEqual({ token: sampleToken, expires_in_sec: 3600 })
  })

  it('fetchTasks: GET worker tasks 返回数组(不写 state)', async () => {
    const tasks = [{ job_id: 'j1', step: 's1' }]
    get.mockResolvedValueOnce(tasks)
    const store = useWorkerStore()
    const res = await store.fetchTasks('w1')
    expect(get).toHaveBeenCalledWith('/api/workers/w1/tasks')
    expect(res).toEqual(tasks)
    expect(store.workers).toEqual([])
  })
})
