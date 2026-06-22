import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'

// CollectionsView 用 useRouter + Pinia(useCollectionStore, id='collections') + inject('showToast')。
// 子弹窗(AddSubscriptionDialog/DeleteCollectionDialog)与 store action 都打桩。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {} }),
}))

import CollectionsView from './CollectionsView.vue'
import { useCollectionStore } from '../stores/collections'

function makeCollection(over: Record<string, any> = {}) {
  return {
    id: 'c1',
    name: '机器学习合集',
    domain: 'ai',
    description: '一个手动集合',
    tags: ['ml', 'dl'],
    job_count: 7,
    created_at: '2026-01-01',
    subscription: null,
    ...over,
  }
}

// 用给定 store 初始状态挂载;返回 wrapper + store 句柄。
function mountWith(initial: Record<string, any>) {
  const pinia = createTestingPinia({
    createSpy: vi.fn,
    stubActions: true,
    initialState: { collections: initial },
  })
  const w = mount(CollectionsView, {
    global: {
      plugins: [pinia],
      stubs: { AddSubscriptionDialog: true, DeleteCollectionDialog: true },
    },
  })
  return { w, store: useCollectionStore() }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('CollectionsView 列表渲染', () => {
  it('渲染集合卡片(名称/条数/域/标签/描述)', async () => {
    const { w } = mountWith({ collections: [makeCollection()], loading: false })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('机器学习合集')
    expect(t).toContain('7 条')
    expect(t).toContain('ai')
    expect(t).toContain('ml')
    expect(t).toContain('一个手动集合')
    expect(t).toContain('手动') // subscription=null → 手动徽标
  })

  it('订阅集合显示来源徽标而非「手动」', async () => {
    const { w } = mountWith({
      collections: [
        makeCollection({
          id: 'c2',
          name: 'B站追更',
          subscription: {
            source_type: 'bilibili_up',
            source_id: '123',
            enabled: true,
            last_synced_at: null,
          },
        }),
      ],
      loading: false,
    })
    await flushPromises()
    const t = w.text()
    expect(t).toContain('bilibili')   // sourceBadge(group) 文案
    expect(t).toContain('从未同步')   // last_synced_at=null
  })
})

describe('CollectionsView 加载/空 态', () => {
  it('loading 且无数据显示加载中', () => {
    const { w } = mountWith({ collections: [], loading: true })
    expect(w.text()).toContain('加载中…')
  })

  it('非 loading 且无数据显示空态与新建按钮', async () => {
    const { w } = mountWith({ collections: [], loading: false })
    await flushPromises()
    expect(w.text()).toContain('还没有集合')
    expect(w.findAll('button').some((b) => b.text().includes('新建集合'))).toBe(true)
  })
})

describe('CollectionsView 知识库筛选', () => {
  it('按 domain 过滤可见卡片', async () => {
    const { w } = mountWith({
      collections: [
        makeCollection({ id: 'a', name: 'AI 集', domain: 'ai' }),
        makeCollection({ id: 'b', name: '历史 集', domain: 'history' }),
      ],
      loading: false,
    })
    await flushPromises()
    // 筛选器有「全部知识库」+ 两个 domain 选项
    const select = w.find('select')
    expect(select.exists()).toBe(true)
    await select.setValue('history')
    const t = w.text()
    expect(t).toContain('历史 集')
    expect(t).not.toContain('AI 集')
  })
})

describe('CollectionsView 交互', () => {
  it('点击卡片 router.push 到集合详情', async () => {
    const { w } = mountWith({ collections: [makeCollection({ id: 'cx' })], loading: false })
    await flushPromises()
    await w.find('.col-card').trigger('click')
    expect(push).toHaveBeenCalledWith('/collections/cx')
  })

  it('点击刷新触发 store.fetchAll', async () => {
    const { w, store } = mountWith({ collections: [makeCollection()], loading: false })
    await flushPromises()
    ;(store.fetchAll as any).mockClear()
    const refreshBtn = w.findAll('button').find((b) => b.text().includes('刷新'))!
    await refreshBtn.trigger('click')
    expect(store.fetchAll).toHaveBeenCalled()
  })

  it('点击新建打开 AddSubscriptionDialog', async () => {
    const { w } = mountWith({ collections: [makeCollection()], loading: false })
    await flushPromises()
    expect(w.findComponent({ name: 'AddSubscriptionDialog' }).exists()).toBe(false)
    const createBtn = w.findAll('button').find((b) => b.text().includes('新建'))!
    await createBtn.trigger('click')
    await flushPromises()
    expect(w.findComponent({ name: 'AddSubscriptionDialog' }).exists()).toBe(true)
  })

  it('onMounted 调用 store.fetchAll 加载列表', async () => {
    const { store } = mountWith({ collections: [], loading: false })
    await flushPromises()
    expect(store.fetchAll).toHaveBeenCalled()
  })
})
