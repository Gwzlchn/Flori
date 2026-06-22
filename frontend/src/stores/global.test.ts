import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const get = vi.fn()
const post = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../composables/useApi', () => ({ useApi: () => ({ get, post, put, del }) }))

import { useGlobalStore } from './global'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('useGlobalStore', () => {
  it('初始 state', () => {
    const store = useGlobalStore()
    expect(store.profiles).toEqual([])
    expect(store.styleTags).toEqual([])
    expect(store.crumbOverride).toBeNull()
    expect(store.submitOpen).toBe(false)
  })

  it('setCrumbs: 设置面包屑覆盖，可置 null 清空', () => {
    const store = useGlobalStore()
    const segs = [{ t: '领域' }, { t: 'ML', to: '/d/ml' }]
    store.setCrumbs(segs)
    expect(store.crumbOverride).toEqual(segs)
    store.setCrumbs(null)
    expect(store.crumbOverride).toBeNull()
  })

  it('openSubmit / closeSubmit: 切换投递弹窗开关', () => {
    const store = useGlobalStore()
    store.openSubmit()
    expect(store.submitOpen).toBe(true)
    store.closeSubmit()
    expect(store.submitOpen).toBe(false)
  })

  it('fetchProfiles: GET /api/profiles 写入 state', async () => {
    const list = [{ name: 'default' }]
    get.mockResolvedValueOnce(list)
    const store = useGlobalStore()
    await store.fetchProfiles()
    expect(get).toHaveBeenCalledWith('/api/profiles')
    expect(store.profiles).toEqual(list)
  })

  it('fetchStyleTags: 成功时写入返回的标签', async () => {
    const tags = ['animated', 'lecture']
    get.mockResolvedValueOnce(tags)
    const store = useGlobalStore()
    await store.fetchStyleTags()
    expect(get).toHaveBeenCalledWith('/api/config/styles')
    expect(store.styleTags).toEqual(tags)
  })

  it('fetchStyleTags: 失败时回退到内置默认标签列表', async () => {
    get.mockRejectedValueOnce(new Error('500'))
    const store = useGlobalStore()
    await store.fetchStyleTags()
    expect(store.styleTags).toEqual([
      'animated', 'lecture', 'code-tutorial', 'talk', 'case-study', 'math-visual',
    ])
  })
})
