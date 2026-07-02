import { describe, it, expect } from 'vitest'
import { mount, config } from '@vue/test-utils'
import AddSubscriptionDialog from './AddSubscriptionDialog.vue'
import { SOURCE_TYPES } from '../../constants/sources'

// 弹窗已 <Teleport to="body">(逃出侧栏 .side 的 sticky 层叠上下文);测试里让 teleport 就地渲染,
// 这样 w.find/.findAll 仍能查到弹窗内部节点(否则内容被传送到 body、不在 wrapper 内)。
config.global.stubs = { ...config.global.stubs, teleport: true }

// AddSubscriptionDialog 是纯受控弹窗(props/emit + 本地 ref,无 store/router/请求)。
// 这里验证渲染、来源类型切换的条件分支、提交校验与 create/close 事件的 payload。

describe('AddSubscriptionDialog', () => {
  it('默认手动模式:渲染标题、名称字段,且不显示订阅专属字段', () => {
    const w = mount(AddSubscriptionDialog)
    const t = w.text()
    expect(t).toContain('新建集合 / 订阅')
    expect(t).toContain('来源类型')
    expect(t).toContain('手动集合')
    expect(t).toContain('名称')
    // 手动态没有创建后立即同步的复选项
    expect(t).not.toContain('创建后立即同步一次')
  })

  it('渲染全部来源类型选项(手动 + SOURCE_TYPES)', () => {
    const w = mount(AddSubscriptionDialog)
    const opts = w.findAll('.src-opt')
    expect(opts.length).toBe(SOURCE_TYPES.length + 1)
    const t = w.text()
    for (const s of SOURCE_TYPES) expect(t).toContain(s.label)
  })

  it('defaultDomain prop 预填知识库输入框', () => {
    const w = mount(AddSubscriptionDialog, { props: { defaultDomain: '机器学习' } })
    const domainInput = w.find('input[placeholder="如 机器学习"]')
    expect((domainInput.element as HTMLInputElement).value).toBe('机器学习')
  })

  it('点击订阅来源后切到订阅态:显示来源ID输入与立即同步,隐藏名称', async () => {
    const w = mount(AddSubscriptionDialog)
    // 第二个 src-opt 是首个真实来源(SOURCE_TYPES[0])
    await w.findAll('.src-opt')[1].trigger('click')
    const t = w.text()
    expect(t).toContain('创建后立即同步一次')
    // 订阅态展示 idLabel,而不展示手动「名称」字段
    expect(t).toContain(SOURCE_TYPES[0].idLabel)
    expect(w.find('input[placeholder="如 手动收藏"]').exists()).toBe(false)
  })

  it('error prop 渲染为错误文案', () => {
    const w = mount(AddSubscriptionDialog, { props: { error: '后端报错啦' } })
    expect(w.text()).toContain('后端报错啦')
  })

  it('saving prop 时主按钮禁用并显示创建中', () => {
    const w = mount(AddSubscriptionDialog, { props: { saving: true } })
    const pri = w.find('.btn.pri')
    expect((pri.element as HTMLButtonElement).disabled).toBe(true)
    expect(pri.text()).toContain('创建中')
  })

  it('点击遮罩与右上角按钮均触发 close', async () => {
    const w = mount(AddSubscriptionDialog)
    await w.find('.overlay').trigger('click')
    await w.find('.hd .ghost').trigger('click')
    expect(w.emitted('close')?.length).toBe(2)
  })

  it('取消按钮触发 close', async () => {
    const w = mount(AddSubscriptionDialog)
    await w.find('.ft .btn:not(.pri)').trigger('click')
    expect(w.emitted('close')).toBeTruthy()
  })

  it('手动态:domain 为空时本地校验报错,不触发 create', async () => {
    const w = mount(AddSubscriptionDialog)
    await w.find('.btn.pri').trigger('click')
    expect(w.text()).toContain('请填写知识库')
    expect(w.emitted('create')).toBeFalsy()
  })

  it('手动态:有 domain 但无名称时报错', async () => {
    const w = mount(AddSubscriptionDialog, { props: { defaultDomain: 'tech' } })
    await w.find('.btn.pri').trigger('click')
    expect(w.text()).toContain('手动集合需填写名称')
    expect(w.emitted('create')).toBeFalsy()
  })

  it('手动态:填好 domain+名称+标签后提交,create payload 含 name 且解析标签', async () => {
    const w = mount(AddSubscriptionDialog, { props: { defaultDomain: 'tech' } })
    await w.find('input[placeholder="如 手动收藏"]').setValue('手动收藏')
    await w.find('input[placeholder="逗号分隔，如 paper-reading, lecture"]').setValue('a, b , ,c')
    await w.find('.btn.pri').trigger('click')
    const ev = w.emitted('create')
    expect(ev).toBeTruthy()
    const payload = ev![0][0] as Record<string, unknown>
    expect(payload.domain).toBe('tech')
    expect(payload.name).toBe('手动收藏')
    expect(payload.tags).toEqual(['a', 'b', 'c'])
    // 手动态不应带订阅字段
    expect(payload.source_type).toBeUndefined()
  })

  it('订阅态:domain=general 被拒绝', async () => {
    const w = mount(AddSubscriptionDialog, { props: { defaultDomain: 'general' } })
    await w.findAll('.src-opt')[1].trigger('click')
    await w.find('.btn.pri').trigger('click')
    expect(w.text()).toContain('订阅集合不能用 general')
    expect(w.emitted('create')).toBeFalsy()
  })

  it('订阅态:缺来源ID时报错,不触发 create', async () => {
    const w = mount(AddSubscriptionDialog, { props: { defaultDomain: 'tech' } })
    await w.findAll('.src-opt')[1].trigger('click')
    await w.find('.btn.pri').trigger('click')
    expect(w.emitted('create')).toBeFalsy()
    // 提示包含 idLabel 或兜底文案 "来源"
    expect(w.text()).toContain('请填写')
  })

  it('订阅态:填好 domain+来源ID 提交,create payload 含 source_type/source_id/sync_now', async () => {
    const w = mount(AddSubscriptionDialog, { props: { defaultDomain: 'tech' } })
    await w.findAll('.src-opt')[1].trigger('click')
    // 订阅态此时唯一带 placeholder 的 .input 文本框是来源ID
    await w.find(`input[placeholder="${SOURCE_TYPES[0].placeholder}"]`).setValue('247209804')
    await w.find('.btn.pri').trigger('click')
    const ev = w.emitted('create')
    expect(ev).toBeTruthy()
    const payload = ev![0][0] as Record<string, unknown>
    expect(payload.domain).toBe('tech')
    expect(payload.source_type).toBe(SOURCE_TYPES[0].type)
    expect(payload.source_id).toBe('247209804')
    expect(payload.sync_now).toBe(true)
    expect(payload.name).toBeUndefined()
  })
})
