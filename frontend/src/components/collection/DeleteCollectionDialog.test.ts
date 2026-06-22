import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import DeleteCollectionDialog from './DeleteCollectionDialog.vue'
import type { Collection } from '../../types'

// DeleteCollectionDialog 是纯受控弹窗(props/emit + 本地 ref,无 store/router/请求)。
// 验证渲染、detach/purge 模式切换、purge 二次确认门控,以及 confirm/close 事件。

function makeCollection(over: Partial<Collection> = {}): Collection {
  return {
    id: 'c1',
    name: '我的收藏',
    domain: 'tech',
    description: '',
    tags: [],
    job_count: 7,
    created_at: '2026-01-01',
    subscription: null,
    ...over,
  }
}

describe('DeleteCollectionDialog', () => {
  it('渲染标题、集合名与内容条数', () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    const t = w.text()
    expect(t).toContain('删除集合')
    expect(t).toContain('我的收藏')
    expect(t).toContain('7')
    expect(t).toContain('保留内容')
    expect(t).toContain('全部删除')
  })

  it('默认 detach 模式:不显示二次确认行,主按钮可用且文案为「删除集合」', () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    expect(w.find('.confirm-row').exists()).toBe(false)
    const btn = w.find('.ft .btn:last-child')
    expect((btn.element as HTMLButtonElement).disabled).toBe(false)
    expect(btn.text()).toContain('删除集合')
  })

  it('detach 模式提交触发 confirm(false)', async () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    await w.find('.ft .btn:last-child').trigger('click')
    const ev = w.emitted('confirm')
    expect(ev).toBeTruthy()
    expect(ev![0]).toEqual([false])
  })

  it('切到 purge 模式:出现二次确认行,未勾选时主按钮禁用且文案为「永久删除」', async () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    await w.find('input[value="purge"]').setValue()
    expect(w.find('.confirm-row').exists()).toBe(true)
    const btn = w.find('.ft .btn:last-child')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)
    expect(btn.text()).toContain('永久删除')
  })

  it('purge 未勾确认时点击不触发 confirm', async () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    await w.find('input[value="purge"]').setValue()
    await w.find('.ft .btn:last-child').trigger('click')
    expect(w.emitted('confirm')).toBeFalsy()
  })

  it('purge 勾选确认后:主按钮启用,提交触发 confirm(true)', async () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    await w.find('input[value="purge"]').setValue()
    await w.find('.confirm-row input[type="checkbox"]').setValue(true)
    const btn = w.find('.ft .btn:last-child')
    expect((btn.element as HTMLButtonElement).disabled).toBe(false)
    await btn.trigger('click')
    const ev = w.emitted('confirm')
    expect(ev).toBeTruthy()
    expect(ev![0]).toEqual([true])
  })

  it('deleting prop 时主按钮禁用并显示删除中', () => {
    const w = mount(DeleteCollectionDialog, {
      props: { collection: makeCollection(), deleting: true },
    })
    const btn = w.find('.ft .btn:last-child')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)
    expect(btn.text()).toContain('删除中')
  })

  it('点击遮罩、右上角按钮、取消按钮均触发 close', async () => {
    const w = mount(DeleteCollectionDialog, { props: { collection: makeCollection() } })
    await w.find('.overlay').trigger('click')
    await w.find('.hd .ghost').trigger('click')
    await w.find('.ft .btn:first-child').trigger('click')
    expect(w.emitted('close')?.length).toBe(3)
  })
})
