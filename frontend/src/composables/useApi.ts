import { ref } from 'vue'

class ApiError extends Error {
  constructor(public status: number, public body: string) {
    super(`API ${status}: ${body}`)
  }
}

// 一键免输登录:支持 https://IP/#token=xxx,把 token 存入 localStorage 后清掉 hash。
function readHashToken(): string {
  const h = window.location.hash || ''
  const m = h.match(/[#&]token=([^&]+)/)
  if (m) {
    const t = decodeURIComponent(m[1])
    localStorage.setItem('auth_token', t)
    // 清掉 URL 里的 token,避免泄漏 / 被书签保存。
    history.replaceState(null, '', window.location.pathname + window.location.search)
    return t
  }
  return ''
}

const authToken = ref(readHashToken() || localStorage.getItem('auth_token') || '')

// 登录策略:默认走 Caddy 全站 Basic Auth,浏览器自动带凭证。
// 应用层不主动发 Authorization,否则会覆盖浏览器的 Basic 头。
// 仅当用户显式设置了 app token(回退方案)时才发 Bearer。
export function setToken(token: string) {
  authToken.value = token
  localStorage.setItem('auth_token', token)
}

export function clearToken() {
  authToken.value = ''
  localStorage.removeItem('auth_token')
}

// 供 WS composable 拼 ?token= 用(契约要求 WS 经 query 传 token)。
export function getAuthToken(): string {
  return authToken.value
}

export function useApi() {
  // 鉴权头 + 401 处理为 request/getText 共用,换 header 名只改一处。
  function buildHeaders(body?: any): Record<string, string> {
    const headers: Record<string, string> = {}
    if (authToken.value) headers['Authorization'] = `Bearer ${authToken.value}`
    if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json'
    return headers
  }
  async function checkResp(resp: Response): Promise<void> {
    if (resp.status === 401) {
      clearToken()
      throw new ApiError(401, 'unauthorized')
    }
    if (!resp.ok) throw new ApiError(resp.status, await resp.text())
  }

  async function request<T>(method: string, path: string, body?: any): Promise<T> {
    const resp = await fetch(path, {
      method,
      headers: buildHeaders(body),
      body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
    })
    await checkResp(resp)
    if (resp.status === 204) return null as T
    return resp.json()
  }

  async function getText(path: string): Promise<string> {
    const resp = await fetch(path, { headers: buildHeaders() })
    await checkResp(resp)
    return resp.text()
  }

  return {
    get: <T>(path: string) => request<T>('GET', path),
    post: <T>(path: string, body?: any) => request<T>('POST', path, body),
    put: <T>(path: string, body?: any) => request<T>('PUT', path, body),
    del: (path: string) => request<void>('DELETE', path),
    upload: <T>(path: string, formData: FormData) => request<T>('POST', path, formData),
    getText,
  }
}
