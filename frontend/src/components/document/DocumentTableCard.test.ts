import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import DocumentTableCard from './DocumentTableCard.vue'
import type { DocumentTable } from './types'

const locator = {
  source_fingerprint: `sha256:${'b'.repeat(64)}`,
  html: { dom_path: 'article > table' },
}

describe('DocumentTableCard', () => {
  it('渲染可复制语义表、表头 scope 与 rowspan/colspan', () => {
    const table: DocumentTable = {
      table_id: 'T1', label: '表 1', caption: '模型结果', source_locator: locator,
      cells: [
        { cell_id: 'h1', row: 0, col: 0, role: 'column_header', text: '模型', rowspan: 2 },
        { cell_id: 'h2', row: 0, col: 1, role: 'column_header', text: '指标', colspan: 2 },
        { cell_id: 'r1', row: 2, col: 0, role: 'row_header', text: 'Flori' },
        { cell_id: 'd1', row: 2, col: 1, role: 'data', text: '98.2' },
      ],
      representations: [{ kind: 'structured' }],
      extraction: { status: 'complete' },
    }
    const wrapper = mount(DocumentTableCard, { props: { table, assetUrl: (path) => path } })

    expect(wrapper.get('caption').text()).toBe('模型结果')
    expect(wrapper.get('th[scope="col"]').attributes('rowspan')).toBe('2')
    expect(wrapper.findAll('th[scope="col"]')[1].attributes('colspan')).toBe('2')
    expect(wrapper.get('th[scope="row"]').text()).toBe('Flori')
    expect(wrapper.get('td').text()).toBe('98.2')
  })

  it('degraded 表可在结构和原始区域间切换并读出原因', async () => {
    const table: DocumentTable = {
      table_id: 'T2', label: '表 2', caption: '扫描结果', source_locator: locator,
      cells: [{ cell_id: 'd', row: 0, col: 0, role: 'data', text: '低置信文本' }],
      representations: [{ kind: 'structured' }, { kind: 'source_crop', artifact: 'tables/T2.png' }],
      extraction: { status: 'degraded', reasons: ['ocr_low_confidence'] },
    }
    const wrapper = mount(DocumentTableCard, { props: { table, assetUrl: (path) => `/assets/${path}` } })

    expect(wrapper.get('.quality-degraded').text()).toBe('degraded')
    expect(wrapper.get('.quality-reasons').text()).toContain('ocr_low_confidence')
    const sourceButton = wrapper.findAll('.table-view-switch button').find((button) => button.text() === '原始区域')!
    await sourceButton.trigger('click')
    expect(wrapper.get('.table-crop img').attributes('src')).toBe('/assets/tables/T2.png')
    expect(sourceButton.attributes('aria-pressed')).toBe('true')
  })

  it('rejected 表没有 crop 时显式展示不可用而非静默删除', () => {
    const table: DocumentTable = {
      table_id: 'T3', label: '表 3', caption: '', source_locator: locator,
      cells: [], representations: [], extraction: { status: 'rejected', reasons: ['no_table_region'] },
    }
    const wrapper = mount(DocumentTableCard, { props: { table, assetUrl: (path) => path } })
    expect(wrapper.get('.visual-missing').text()).toContain('均不可用')
    expect(wrapper.get('.quality-rejected').exists()).toBe(true)
  })
})
