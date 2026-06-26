import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// SettingsView 依赖 useApi.get('/api/auth/status')，模板里用 $router.push 跳转。
// 子组件 BiliLogin/CookieUpload/StatusBadge 不在被测范围，stub 掉。
const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
vi.mock('../composables/useApi', () => ({ useApi: () => api }))

import SettingsView from './SettingsView.vue'

const stubs = {
  BiliLogin: true,
  CookieUpload: true,
  StatusBadge: true,
  McpConnectCard: true,  // 自带 onMounted 拉 /api/mcp/info,会抢 mock;非本测目标,stub(它有独立 test)
}
const $router = { push: vi.fn(), replace: vi.fn() }

function mountView() {
  return mount(SettingsView, {
    global: { stubs, mocks: { $router } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SettingsView', () => {
  it('挂载即请求 /api/auth/status', async () => {
    api.get.mockResolvedValue({
      bilibili: { has_cookies: false, status: 'pending' },
      youtube: { has_cookies: false, status: 'pending' },
    })
    mountView()
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/auth/status')
  })

  it('加载成功后渲染平台认证区块（Bilibili / YouTube）', async () => {
    api.get.mockResolvedValue({
      bilibili: { has_cookies: true, status: 'done' },
      youtube: { has_cookies: true, status: 'done' },
    })
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('平台认证')
    expect(t).toContain('Bilibili')
    expect(t).toContain('YouTube')
    expect(t).toContain('已配置 cookies')
  })

  it('youtube 未配置 cookies 时显示提示文案', async () => {
    api.get.mockResolvedValue({
      bilibili: { has_cookies: false, status: 'pending' },
      youtube: { has_cookies: false, status: 'pending' },
    })
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('需提供登录 cookies')
  })

  it('请求失败渲染错误态并提供重试', async () => {
    api.get.mockRejectedValueOnce(new Error('读取认证状态失败'))
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('读取认证状态失败')
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))
    expect(retry).toBeTruthy()
  })

  it('错误态点击重试重新拉取 auth/status', async () => {
    api.get.mockRejectedValueOnce(new Error('boom'))
    const w = mountView()
    await flushPromises()
    api.get.mockResolvedValueOnce({
      bilibili: { has_cookies: true, status: 'done' },
      youtube: { has_cookies: true, status: 'done' },
    })
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))!
    await retry.trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain('YouTube')
  })

  it('渲染运维与关于入口文案', async () => {
    api.get.mockResolvedValue({
      bilibili: { has_cookies: false, status: 'pending' },
      youtube: { has_cookies: false, status: 'pending' },
    })
    const w = mountView()
    await flushPromises()
    const t = w.text()
    expect(t).toContain('运维')
    expect(t).toContain('系统与 Worker')
    expect(t).toContain('关于 Flori')
  })
})
