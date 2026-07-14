import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useApi, setToken, clearToken } from './useApi'

// 构造一个最小的 Response-like 对象,覆盖 request() 用到的字段。
function mockResp(opts: {
  status?: number
  ok?: boolean
  json?: any
  text?: string
}) {
  const status = opts.status ?? 200
  return {
    status,
    ok: opts.ok ?? (status >= 200 && status < 300),
    json: vi.fn().mockResolvedValue(opts.json ?? {}),
    text: vi.fn().mockResolvedValue(opts.text ?? ''),
  }
}

function lastFetchCall() {
  const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
  return fetchMock.mock.calls[fetchMock.mock.calls.length - 1]
}

describe('useApi', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    // 每个用例从无 token 起步(模块级 authToken 在 import 时为空)。
    clearToken()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    clearToken()
  })

  describe('get', () => {
    it('用 GET 方法、正确 URL、无 body、解析 JSON', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: { hello: 'world' } }))

      const { get } = useApi()
      const out = await get<{ hello: string }>('/api/jobs')

      expect(out).toEqual({ hello: 'world' })
      const [url, init] = lastFetchCall()
      expect(url).toBe('/api/jobs')
      expect(init.method).toBe('GET')
      expect(init.body).toBeUndefined()
    })

    it('无 token 时不发 Authorization 头,也不发 Content-Type', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: {} }))

      const { get } = useApi()
      await get('/api/x')

      const [, init] = lastFetchCall()
      expect(init.headers).not.toHaveProperty('Authorization')
      expect(init.headers).not.toHaveProperty('Content-Type')
    })

    it('把 AbortSignal 原样传给 fetch', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: {} }))
      const controller = new AbortController()

      const { get } = useApi()
      await get('/api/x', controller.signal)

      const [, init] = lastFetchCall()
      expect(init.signal).toBe(controller.signal)
    })
  })

  describe('post', () => {
    it('用 POST 方法、JSON.stringify body、带 Content-Type: application/json', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: { id: 1 } }))

      const { post } = useApi()
      const payload = { name: 'foo', n: 2 }
      const out = await post<{ id: number }>('/api/jobs', payload)

      expect(out).toEqual({ id: 1 })
      const [url, init] = lastFetchCall()
      expect(url).toBe('/api/jobs')
      expect(init.method).toBe('POST')
      expect(init.headers['Content-Type']).toBe('application/json')
      expect(init.body).toBe(JSON.stringify(payload))
    })

    it('无 body 的 POST → body 为 undefined、不带 Content-Type', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: {} }))

      const { post } = useApi()
      await post('/api/action')

      const [, init] = lastFetchCall()
      expect(init.method).toBe('POST')
      expect(init.body).toBeUndefined()
      expect(init.headers).not.toHaveProperty('Content-Type')
    })

    it('FormData body → 原样透传、不设 Content-Type、不 JSON.stringify', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: { ok: true } }))

      const fd = new FormData()
      fd.append('file', 'data')

      const { post } = useApi()
      await post('/api/upload', fd)

      const [, init] = lastFetchCall()
      expect(init.body).toBe(fd)
      expect(init.headers).not.toHaveProperty('Content-Type')
    })
  })

  describe('put', () => {
    it('用 PUT 方法、JSON body、解析 JSON 返回', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: { updated: true } }))

      const { put } = useApi()
      const out = await put<{ updated: boolean }>('/api/jobs/1', { title: 't' })

      expect(out).toEqual({ updated: true })
      const [url, init] = lastFetchCall()
      expect(url).toBe('/api/jobs/1')
      expect(init.method).toBe('PUT')
      expect(init.headers['Content-Type']).toBe('application/json')
      expect(init.body).toBe(JSON.stringify({ title: 't' }))
    })
  })

  describe('del', () => {
    it('用 DELETE 方法、无 body', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: {} }))

      const { del } = useApi()
      await del('/api/jobs/9')

      const [url, init] = lastFetchCall()
      expect(url).toBe('/api/jobs/9')
      expect(init.method).toBe('DELETE')
      expect(init.body).toBeUndefined()
    })
  })

  describe('Authorization 头', () => {
    it('设置了 token → 发 Bearer 头', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ json: {} }))

      setToken('abc123')
      const { get } = useApi()
      await get('/api/secure')

      const [, init] = lastFetchCall()
      expect(init.headers['Authorization']).toBe('Bearer abc123')
    })
  })

  describe('错误处理', () => {
    it('204 → 返回 null(不调用 json)', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      const resp = mockResp({ status: 204, ok: true })
      fetchMock.mockResolvedValue(resp)

      const { del } = useApi()
      const out = await del('/api/jobs/1')

      expect(out).toBeNull()
      expect(resp.json).not.toHaveBeenCalled()
    })

    it('401 → 清 token 并抛 ApiError(401, "unauthorized")', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(mockResp({ status: 401, ok: false }))

      // 先设一个 token,验证 401 之后被清掉(后续请求不再带 Bearer)。
      setToken('will-be-cleared')
      const { get } = useApi()

      await expect(get('/api/secure')).rejects.toMatchObject({
        status: 401,
        body: 'unauthorized',
      })

      // token 已被 clearToken() 清掉:下一次请求无 Authorization 头。
      fetchMock.mockResolvedValue(mockResp({ json: {} }))
      await get('/api/next')
      const [, init] = lastFetchCall()
      expect(init.headers).not.toHaveProperty('Authorization')
    })

    it('非 2xx(非 401) → 抛 ApiError,携带 status 与响应文本 body', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(
        mockResp({ status: 500, ok: false, text: 'boom' }),
      )

      const { get } = useApi()
      await expect(get('/api/x')).rejects.toMatchObject({
        status: 500,
        body: 'boom',
      })
    })

    it('统一解析 ErrorResponse 并暴露 error code/message', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      const body = JSON.stringify({ error: 'conflict', message: '版本已变化' })
      fetchMock.mockResolvedValue(mockResp({ status: 409, ok: false, text: body }))

      const { put } = useApi()
      await expect(put('/api/glossary/ml/gradient', {})).rejects.toMatchObject({
        status: 409,
        body,
        error: 'conflict',
        message: 'API 409: 版本已变化',
      })
    })

    it('401 ErrorResponse 清 token 且保留服务端消息', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      const body = JSON.stringify({ error: 'unauthorized', message: '登录已失效' })
      fetchMock.mockResolvedValue(mockResp({ status: 401, ok: false, text: body }))
      setToken('will-be-cleared')

      const { get } = useApi()
      await expect(get('/api/secure')).rejects.toMatchObject({
        status: 401, error: 'unauthorized', message: 'API 401: 登录已失效',
      })
      fetchMock.mockResolvedValue(mockResp({ json: {} }))
      await get('/api/next')
      const [, init] = lastFetchCall()
      expect(init.headers).not.toHaveProperty('Authorization')
    })

    it('ApiError.message 包含 status 与 body', async () => {
      const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
      fetchMock.mockResolvedValue(
        mockResp({ status: 404, ok: false, text: 'not found' }),
      )

      const { get } = useApi()
      await expect(get('/api/missing')).rejects.toThrow('API 404: not found')
    })
  })
})
