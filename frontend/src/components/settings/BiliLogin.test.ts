import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const api = {
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  upload: vi.fn(),
  getText: vi.fn(),
}
vi.mock('../../composables/useApi', () => ({ useApi: () => api }))

import BiliLogin from './BiliLogin.vue'
import type { BiliLoginPoll, BiliLoginStart, BiliLoginState, BiliStatus } from '../../types'

const showToast = vi.fn()
let status = { logged_in: false, uname: null as string | null }
let pollStates: Array<'waiting' | 'scanned' | 'confirmed' | 'expired'> = []

function button(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('button').find((item) => item.text().includes(label))!
}

function mountLogin() {
  return mount(BiliLogin, {
    global: {
      provide: { showToast },
      stubs: { Teleport: true },
    },
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  status = { logged_in: false, uname: null }
  pollStates = []
  api.get.mockImplementation(async (path: string) => {
    if (path === '/api/bili/status') return { ...status }
    if (path.startsWith('/api/bili/login/poll?qrcode_key=')) {
      return { state: pollStates.shift() ?? 'waiting' }
    }
    throw new Error(`unexpected GET: ${path}`)
  })
  api.post.mockImplementation(async (path: string) => {
    if (path === '/api/bili/login/start') {
      return { qr_png: 'data:image/png;base64,AA==', qrcode_key: 'key a', url: 'https://example.invalid' }
    }
    if (path === '/api/bili/logout') return { ok: true }
    throw new Error(`unexpected POST: ${path}`)
  })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('BiliLogin', () => {
  it('扫码登录 DTO 与共享类型契约保持一致', () => {
    const state: BiliLoginState = 'scanned'
    const statusContract: BiliStatus = { logged_in: false, uname: null }
    const startContract: BiliLoginStart = {
      qrcode_key: 'key',
      qr_png: 'data:image/png;base64,AA==',
      url: 'https://example.invalid',
    }
    const pollContract: BiliLoginPoll = { state, logged_in: false, uname: null }

    expect([statusContract.logged_in, startContract.qrcode_key, pollContract.state]).toEqual([
      false,
      'key',
      'scanned',
    ])
  })

  it('挂载读取登录状态并呈现账号', async () => {
    status = { logged_in: true, uname: 'tester' }
    const wrapper = mountLogin()
    await flushPromises()

    expect(api.get).toHaveBeenCalledWith('/api/bili/status')
    expect(wrapper.text()).toContain('已登录')
    expect(wrapper.text()).toContain('tester')
  })

  it('扫码后每 2 秒轮询,并处理 scanned 到 confirmed', async () => {
    pollStates = ['scanned', 'confirmed']
    const wrapper = mountLogin()
    await flushPromises()

    await button(wrapper, '扫码登录').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/bili/login/start')
    expect(wrapper.find('img').attributes('src')).toBe('data:image/png;base64,AA==')

    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/bili/login/poll?qrcode_key=key%20a')
    expect(wrapper.text()).toContain('已扫码,请在手机确认')

    status = { logged_in: true, uname: 'confirmed-user' }
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.text()).toContain('confirmed-user')
    expect(showToast).toHaveBeenCalledWith('B站登录成功', 'success')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('二维码过期后停止轮询并显示重新生成', async () => {
    pollStates = ['expired']
    const wrapper = mountLogin()
    await flushPromises()
    await button(wrapper, '扫码登录').trigger('click')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.text()).toContain('二维码已过期')
    expect(button(wrapper, '重新生成').exists()).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('注销走 POST 并刷新状态', async () => {
    status = { logged_in: true, uname: 'tester' }
    const wrapper = mountLogin()
    await flushPromises()
    status = { logged_in: false, uname: null }

    await button(wrapper, '注销').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/bili/logout')
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('待登录')
    expect(showToast).toHaveBeenCalledWith('已注销', 'success')
  })

  it('卸载组件会清除正在运行的轮询 timer', async () => {
    const wrapper = mountLogin()
    await flushPromises()
    await button(wrapper, '扫码登录').trigger('click')
    await flushPromises()
    expect(vi.getTimerCount()).toBe(1)

    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('生成二维码失败会恢复空闲态并提示', async () => {
    api.post.mockRejectedValueOnce(new Error('failed'))
    const wrapper = mountLogin()
    await flushPromises()

    await button(wrapper, '扫码登录').trigger('click')
    await flushPromises()
    expect(showToast).toHaveBeenCalledWith('生成二维码失败', 'error')
    expect(wrapper.text()).toContain('扫码登录')
  })
})
