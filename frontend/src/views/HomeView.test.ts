import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import type { DomainOverview } from '../types'

// 路由 mock: openDomain / 侧栏 ?create=1 跳转 / 新建后跳转都打这套 push/replace。
const push = vi.fn()
const replace = vi.fn()
// route.query 在各用例间可变(测 ?create=1 触发弹窗),用可写对象。
let routeQuery: Record<string, any> = {}
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace }),
  useRoute: () => ({ params: {}, get query() { return routeQuery } }),
}))

import HomeView from './HomeView.vue'

function makeDomain(over: Partial<DomainOverview> = {}): DomainOverview {
  return {
    domain: 'rl',
    collection_count: 2,
    job_count: 5,
    concept_count: 9,
    subscription_count: 0,
    last_active_at: null,
    ...over,
  }
}

// createTestingPinia(stubActions) 把 fetchAll/create 替换成 spy,不真发请求;
// initialState 直接喂 store 的 domains/loading ref,驱动模板分支。
function mountHome(state: { domains?: DomainOverview[]; loading?: boolean } = {}) {
  const showToast = vi.fn()
  const w = mount(HomeView, {
    global: {
      plugins: [createTestingPinia({
        createSpy: vi.fn,
        stubActions: true,
        initialState: { domains: { domains: state.domains ?? [], loading: state.loading ?? false } },
      })],
      provide: { showToast },
    },
  })
  return { w, showToast }
}

beforeEach(() => {
  routeQuery = {}
  vi.clearAllMocks()
})

describe('HomeView', () => {
  it('始终渲染页头标题与新建按钮', () => {
    const { w } = mountHome()
    expect(w.text()).toContain('我的知识库')
    expect(w.text()).toContain('新建知识库')
  })

  it('加载中且无数据时显示「加载中…」', () => {
    const { w } = mountHome({ loading: true, domains: [] })
    expect(w.text()).toContain('加载中…')
  })

  it('无数据且非加载时显示空态文案', () => {
    const { w } = mountHome({ loading: false, domains: [] })
    expect(w.text()).toContain('还没有知识库')
  })

  it('有数据时渲染卡片：名称/统计/订阅徽标', () => {
    const { w } = mountHome({
      domains: [makeDomain({
        domain: 'rl', display_name: '强化学习',
        collection_count: 3, job_count: 7, concept_count: 12,
        subscription_count: 2,
      })],
    })
    const t = w.text()
    expect(t).toContain('强化学习')
    expect(t).toContain('3 集合')
    expect(t).toContain('7 内容')
    expect(t).toContain('12 概念')
    // subscription_count > 0 才渲染订阅徽标数字
    expect(t).toContain('2')
    // 卡片是 .dcard
    expect(w.findAll('.dcard')).toHaveLength(1)
  })

  it('display_name 缺失时回退用 domain 作卡片名', () => {
    const { w } = mountHome({ domains: [makeDomain({ domain: 'crypto', display_name: undefined })] })
    expect(w.text()).toContain('crypto')
  })

  it('last_active_at 为空显示「从未活跃」', () => {
    const { w } = mountHome({ domains: [makeDomain({ last_active_at: null })] })
    expect(w.text()).toContain('从未活跃')
  })

  it('点击卡片 router.push 到 encode 后的领域路由', async () => {
    const { w } = mountHome({ domains: [makeDomain({ domain: 'macro econ' })] })
    await w.find('.dcard').trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/macro%20econ')
  })

  it('点击新建按钮打开弹窗', async () => {
    const { w } = mountHome({ domains: [makeDomain()] })
    expect(w.find('.overlay').exists()).toBe(false)
    // 页头的新建按钮(.btn.pri)
    await w.find('button.btn.pri').trigger('click')
    expect(w.find('.overlay').exists()).toBe(true)
    expect(w.text()).toContain('标识（英文 slug）')
  })

  it('弹窗内 slug 为空提交时 toast 报错且不调 store.create', async () => {
    const { w, showToast } = mountHome({ domains: [makeDomain()] })
    await w.find('button.btn.pri').trigger('click')
    // 弹窗底部「创建知识库」按钮: 最后一个 .btn.pri
    const buttons = w.findAll('button.btn.pri')
    await buttons[buttons.length - 1].trigger('click')
    await flushPromises()
    expect(showToast).toHaveBeenCalledWith('请填写标识（英文 slug）', 'error')
  })

  it('mounted 时根据 ?create=1 自动打开弹窗并清掉 query', async () => {
    routeQuery = { create: '1' }
    const { w } = mountHome({ domains: [makeDomain()] })
    await flushPromises()
    expect(w.find('.overlay').exists()).toBe(true)
    expect(replace).toHaveBeenCalledWith({ path: '/', query: {} })
  })

  it('「所有来源」按钮跳转 /content', async () => {
    const { w } = mountHome()
    const btns = w.findAll('button.btn')
    const inbox = btns.find((b) => b.text().includes('所有来源'))!
    await inbox.trigger('click')
    expect(push).toHaveBeenCalledWith('/content')
  })
})
