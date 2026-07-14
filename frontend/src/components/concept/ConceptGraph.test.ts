import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

// router:固定路由,共享 push 间谍断言跳转。
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useRoute: () => ({ params: { domain: 'tech' }, query: {} }),
}))

// useApi:真 Pinia + 假 useApi,让真 store.conceptGraph 跑通(数据流入组件本地 ref)。
const get = vi.fn()
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get, post: vi.fn(), del: vi.fn(), put: vi.fn(), upload: vi.fn(), getText: vi.fn() }),
}))

// vis-network 在 jsdom 无 canvas 渲染:stub 掉,组件 render() 的 catch 会兜住缺失;
// 这里给一个最小可构造的桩,确保即便 import 成功也不真渲染、不报错。
vi.mock('vis-network/standalone', () => ({
  Network: class {
    on() {}
    destroy() {}
    selectNodes() {}
    focus() {}
  },
  DataSet: class {
    constructor(public items: any[] = []) {}
    update() {}
  },
}))

import ConceptGraph from './ConceptGraph.vue'

function graph(over: Partial<any> = {}) {
  return {
    nodes: [
      { id: '通胀', term: '通胀', definition: '物价普涨。', status: 'accepted', is_topic: true, occurrence_count: 3 },
      { id: '利率', term: '利率', definition: '资金价格。', status: 'accepted', is_topic: false, occurrence_count: 2 },
      { id: '孤立词', term: '孤立词', definition: '', status: 'suggested', is_topic: false, occurrence_count: 0 },
    ],
    edges: [
      { source: '通胀', target: '利率', weight: 2 },
    ],
    stats: { node_count: 3, edge_count: 1, isolated_count: 1 },
    ...over,
  }
}

function conceptDetail(term: string, definition: string, occurrenceTotal = 3) {
  const current = {
    definition_version_id: `cdv-${term}`, domain: 'tech', term, version: 1, definition,
    source_evidence_ids: [], source_set_fingerprint: '', strategy: 'manual_edit',
    provider: null, model: null, prompt_hash: null, input_hash: null,
    supersedes_version_id: null, actor: 'test', created_at: '2026-07-15T00:00:00Z',
  }
  return {
    domain: 'tech', term, definition, zh_name: '', aliases: [], occurrences: [],
    occurrence_total: occurrenceTotal, occurrence_limit: 100, related: [], status: 'accepted',
    watched: false, is_topic: false, definition_locked: false,
    current_definition_version_id: current.definition_version_id, lock_revision: 0,
    created_at: null, updated_at: null, current_definition: current,
    definition_history: [current], definition_history_total: 1, definition_history_limit: 100,
    attestation: {
      domain: 'tech', term, level: 'none', evidence_count: 0, job_count: 0,
      source_fingerprint_count: 0, content_type_count: 0, source_set_fingerprint: '',
      included: [], excluded: [],
    },
  }
}

async function mountGraph() {
  const w = mount(ConceptGraph, { props: { domain: 'tech' } })
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  push.mockReset()
})

describe('ConceptGraph 加载与渲染', () => {
  it('挂载时按 domain 拉取 concept-graph', async () => {
    get.mockResolvedValue(graph())
    await mountGraph()
    expect(get).toHaveBeenCalledWith('/api/domains/tech/concept-graph')
  })

  it('渲染节点/边统计(节点数、关联数、孤立数)', async () => {
    get.mockResolvedValue(graph())
    const w = await mountGraph()
    const t = w.text()
    expect(t).toContain('3 概念')   // node_count
    expect(t).toContain('1 关联')   // edge_count
    expect(t).toContain('1 孤立')   // isolated_count
  })

  it('空领域显示空态', async () => {
    get.mockResolvedValue(graph({ nodes: [], edges: [], stats: { node_count: 0, edge_count: 0, isolated_count: 0 } }))
    const w = await mountGraph()
    expect(w.text()).toContain('暂无概念图谱数据')
  })

  it('加载失败显示错误与重试', async () => {
    get.mockRejectedValue(new Error('网络炸了'))
    const w = await mountGraph()
    expect(w.text()).toContain('网络炸了')
    expect(w.text()).toContain('重试')
  })
})

describe('ConceptGraph 选中与侧栏', () => {
  it('选中节点打开侧栏:定义/主题徽标/相连概念/打开概念详情', async () => {
    get.mockImplementation((path: string) => path.endsWith('/concept-graph')
      ? Promise.resolve(graph())
      : Promise.resolve(conceptDetail('通胀', '物价普涨。')))
    const w = await mountGraph()
    // 侧栏初始不显示。
    expect(w.find('[data-test="panel"]').exists()).toBe(false)
    // 模拟点节点:图谱点击发生在 canvas,经暴露的 selectNode 驱动。
    ;(w.vm as any).selectNode('通胀')
    await flushPromises()
    const panel = w.find('[data-test="panel"]')
    expect(panel.exists()).toBe(true)
    const t = panel.text()
    expect(t).toContain('通胀')
    expect(t).toContain('物价普涨。')   // 定义
    expect(t).toContain('主题')          // is_topic 徽标
    expect(t).toContain('利率')          // 相连概念(共现边)
    expect(t).toContain('打开概念详情')
    expect(panel.find('[data-test="concept-definition-panel"]').exists()).toBe(true)
    expect(get.mock.calls.map((call) => call[0])).toContain('/api/glossary/tech/%E9%80%9A%E8%83%80')
  })

  it('点「打开概念详情」跳转术语页(含 encodeURIComponent)', async () => {
    get.mockImplementation((path: string) => path.endsWith('/concept-graph')
      ? Promise.resolve(graph({
        nodes: [{ id: 'A B', term: 'A B', definition: 'x', status: 'accepted', is_topic: false, occurrence_count: 1 }],
        edges: [],
        stats: { node_count: 1, edge_count: 0, isolated_count: 1 },
      }))
      : Promise.resolve(conceptDetail('A B', 'x', 1)))
    const w = await mountGraph()
    ;(w.vm as any).selectNode('A B')
    await flushPromises()
    await w.find('[data-test="panel"] .btn.pri').trigger('click')
    expect(push).toHaveBeenCalledWith('/kb/tech/concepts/A%20B')
  })

  it('孤立节点侧栏提示无关联', async () => {
    get.mockImplementation((path: string) => path.endsWith('/concept-graph')
      ? Promise.resolve(graph())
      : Promise.resolve(conceptDetail('孤立词', '', 0)))
    const w = await mountGraph()
    ;(w.vm as any).selectNode('孤立词')
    await flushPromises()
    expect(w.find('[data-test="panel"]').text()).toContain('孤立节点')
  })
})

describe('ConceptGraph 筛选', () => {
  it('「只看有关联」切换隐藏孤立(过滤态计数变化)', async () => {
    get.mockResolvedValue(graph())
    const w = await mountGraph()
    // 找到「只看有关联」按钮并点击,断言切到 on 态(过滤逻辑就地生效,不抛错)。
    const btn = w.findAll('.cg-controls .chip').find(b => b.text() === '只看有关联')!
    expect(btn).toBeTruthy()
    await btn.trigger('click')
    await flushPromises()
    expect(btn.classes()).toContain('on')
  })
})
