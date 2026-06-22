import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { CheckCircle, XCircle, Info } from 'lucide-vue-next'
import Toast from './Toast.vue'

describe('Toast', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('渲染传入的 message 文本', () => {
    const w = mount(Toast, { props: { message: '保存成功' } })
    expect(w.text()).toContain('保存成功')
  })

  it('默认 type=info → 蓝色类 + Info 图标', () => {
    const w = mount(Toast, { props: { message: 'hi' } })
    const div = w.get('div')
    expect(div.classes()).toContain('bg-blue-50')
    expect(div.classes()).toContain('border-blue-200')
    expect(div.classes()).toContain('text-blue-800')
    expect(w.findComponent(Info).exists()).toBe(true)
  })

  it('type=success → 绿色类 + CheckCircle 图标', () => {
    const w = mount(Toast, { props: { message: 'ok', type: 'success' } })
    const div = w.get('div')
    expect(div.classes()).toContain('bg-green-50')
    expect(div.classes()).toContain('border-green-200')
    expect(div.classes()).toContain('text-green-800')
    expect(w.findComponent(CheckCircle).exists()).toBe(true)
  })

  it('type=error → 红色类 + XCircle 图标', () => {
    const w = mount(Toast, { props: { message: 'bad', type: 'error' } })
    const div = w.get('div')
    expect(div.classes()).toContain('bg-red-50')
    expect(div.classes()).toContain('border-red-200')
    expect(div.classes()).toContain('text-red-800')
    expect(w.findComponent(XCircle).exists()).toBe(true)
  })

  it('挂载即可见(watch immediate)', () => {
    const w = mount(Toast, { props: { message: 'visible' } })
    expect(w.find('div').exists()).toBe(true)
  })

  it('默认 3000ms 后 emit close 并隐藏', async () => {
    const w = mount(Toast, { props: { message: 'auto' } })
    expect(w.emitted('close')).toBeUndefined()

    vi.advanceTimersByTime(2999)
    expect(w.emitted('close')).toBeUndefined()

    vi.advanceTimersByTime(1)
    expect(w.emitted('close')).toHaveLength(1)

    await w.vm.$nextTick()
    expect(w.find('div').exists()).toBe(false)
  })

  it('尊重自定义 duration', () => {
    const w = mount(Toast, { props: { message: 'fast', duration: 500 } })
    vi.advanceTimersByTime(499)
    expect(w.emitted('close')).toBeUndefined()
    vi.advanceTimersByTime(1)
    expect(w.emitted('close')).toHaveLength(1)
  })

  it('message 变化重置可见性并再次安排 close', async () => {
    const w = mount(Toast, { props: { message: 'first', duration: 1000 } })

    // 首条到期 → close + 隐藏
    vi.advanceTimersByTime(1000)
    await w.vm.$nextTick()
    expect(w.emitted('close')).toHaveLength(1)
    expect(w.find('div').exists()).toBe(false)

    // 改 message → 重新可见,并在新的 duration 后再次 emit
    await w.setProps({ message: 'second' })
    expect(w.find('div').exists()).toBe(true)
    expect(w.text()).toContain('second')

    vi.advanceTimersByTime(1000)
    expect(w.emitted('close')).toHaveLength(2)
  })
})
