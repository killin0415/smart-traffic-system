import { useAppStore } from '../store'

export class ApiError extends Error {
  status: number
  payload: unknown

  constructor(status: number, payload: unknown, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

interface ApiCallOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
  query?: Record<string, string | number | undefined>
  suppressToast?: boolean
}

function buildUrl(path: string, query?: ApiCallOptions['query']): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue
    params.append(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

function extractErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object' && 'error' in payload) {
    const err = (payload as { error?: unknown }).error
    if (typeof err === 'string' && err.length > 0) return err
  }
  return fallback
}

export async function apiCall<T>(
  path: string,
  options: ApiCallOptions = {},
): Promise<T> {
  const { method = 'GET', body, signal, query, suppressToast = false } = options
  const url = buildUrl(path, query)
  const init: RequestInit = {
    method,
    headers: { Accept: 'application/json' },
    signal,
  }
  if (body !== undefined) {
    init.body = JSON.stringify(body)
    ;(init.headers as Record<string, string>)['Content-Type'] =
      'application/json'
  }

  let response: Response
  try {
    response = await fetch(url, init)
  } catch (err) {
    if (signal?.aborted) throw err
    const msg = '無法連線後端，請檢查網路或服務狀態'
    console.warn(`[api] network error on ${method} ${url}:`, err)
    if (!suppressToast) useAppStore.getState().showError(msg)
    throw new ApiError(0, null, msg)
  }

  const text = await response.text()
  let payload: unknown = null
  if (text.length > 0) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const fallback =
      response.status >= 500 ? '伺服器發生錯誤' : '請求格式錯誤'
    const message = extractErrorMessage(payload, fallback)
    console.warn(
      `[api] ${method} ${url} → ${response.status}:`,
      payload ?? message,
    )
    if (!suppressToast) useAppStore.getState().showError(message)
    throw new ApiError(response.status, payload, message)
  }

  return payload as T
}
