import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// useApi mock:控制 /api/mcp/info 与 /api/mcp/token 的返回。
const api = { get: vi.fn() }
vi.mock('../../composables/useApi', () => ({ useApi: () => api }))

import McpConnectCard from './McpConnectCard.vue'

const INFO = {
  enabled: true,
  http_path: '/mcp',
  local_url: 'http://127.0.0.1:8090/mcp',
  token_configured: true,
  tools: [
    { name: 'list_knowledge_bases', description: '列出知识库' },
    { name: 'search', description: '全文检索' },
    { name: 'get_note', description: '取笔记 Markdown' },
  ],
  stats: { total: 7, by_tool: { search: 5, get_note: 2 } },
}

function mountCard() {
  return mount(McpConnectCard, { global: { provide: { showToast: () => {} } } })
}

beforeEach(() => {
  api.get.mockReset()
  api.get.mockImplementation((path: string) => {
    if (path === '/api/mcp/info') return Promise.resolve(INFO)
    if (path === '/api/mcp/token') return Promise.resolve({ token: 'flori-mcp-faketoken' })
    return Promise.resolve({})
  })
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  })
})

describe('McpConnectCard', () => {
  it('渲染工具清单 + 默认本地 tab(HTTP 本地端点) + token 遮掩', async () => {
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('接入 MCP')
    expect(w.text()).toContain('list_knowledge_bases')
    expect(w.text()).toContain('get_note')
    expect(w.text()).toContain('--transport http') // 统一走 HTTP
    expect(w.text()).toContain('http://127.0.0.1:8090/mcp') // 本地端点
    expect(w.text()).toContain('••••') // token 默认遮掩
    expect(w.text()).not.toContain('flori-mcp-faketoken')
  })

  it('切到公网 tab 切换到公网端点(仍 HTTP + curl,不再显示本地 127.0.0.1)', async () => {
    const w = mountCard()
    await flushPromises()
    const httpTab = w.findAll('.seg button').find((b) => b.text().includes('公网'))
    await httpTab!.trigger('click')
    expect(w.text()).toContain('--transport http')
    expect(w.text()).toContain('curl')
    expect(w.text()).not.toContain('127.0.0.1:8090')
  })

  it('点击「显示/复制」取回并展示 token', async () => {
    const w = mountCard()
    await flushPromises()
    const revealBtn = w.findAll('button').find((b) => b.text().includes('显示/复制'))
    await revealBtn!.trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/mcp/token')
    expect(w.text()).toContain('flori-mcp-faketoken')
  })

  it('显示调用统计(总调用 + 按工具)', async () => {
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('调用统计')
    expect(w.text()).toContain('总调用 7')
    expect(w.text()).toContain('search')
  })

  it('stats 缺失时仍渲染总调用 0(不报错)', async () => {
    api.get.mockImplementation((p: string) =>
      p === '/api/mcp/info'
        ? Promise.resolve({ ...INFO, stats: undefined })
        : Promise.resolve({}),
    )
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('总调用 0')
  })

  it('未配置 token 时提示去 .env 设', async () => {
    api.get.mockImplementation((p: string) =>
      p === '/api/mcp/info'
        ? Promise.resolve({ ...INFO, token_configured: false })
        : Promise.resolve({}),
    )
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('未配置')
    expect(w.text()).toContain('FLORI_MCP_TOKEN')
  })
})
