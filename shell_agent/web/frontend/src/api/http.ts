export type QueryValue = string | number | boolean | null | undefined

export class ApiError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(message: string, status = 0, payload: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  query?: Record<string, QueryValue>
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(path, window.location.origin)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value))
      }
    }
  }
  return `${url.pathname}${url.search}`
}

function messageFromPayload(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object') {
    const error = (payload as { error?: unknown; detail?: unknown }).error
      ?? (payload as { detail?: unknown }).detail
    if (typeof error === 'string' && error.trim()) return error
    if (error && typeof error === 'object') {
      const message = (error as { message?: unknown }).message
      if (typeof message === 'string' && message.trim()) return message
    }
  }
  return fallback
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers)
  let body: BodyInit | undefined

  if (options.body instanceof FormData || typeof options.body === 'string' || options.body instanceof Blob) {
    body = options.body
  } else if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json')
    body = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), { ...options, headers, body })
  } catch (error) {
    throw new ApiError(error instanceof Error ? error.message : '网络请求失败')
  }

  const contentType = response.headers.get('content-type') ?? ''
  const payload: unknown = response.status === 204
    ? null
    : contentType.includes('application/json')
      ? await response.json()
      : await response.text()

  if (!response.ok) {
    throw new ApiError(messageFromPayload(payload, `请求失败 (${response.status})`), response.status, payload)
  }

  if (payload && typeof payload === 'object' && 'error' in payload) {
    const error = (payload as { error?: unknown }).error
    if (typeof error === 'string' && error.trim()) {
      throw new ApiError(error, response.status, payload)
    }
  }

  return payload as T
}

export const http = {
  get<T>(path: string, query?: Record<string, QueryValue>) {
    return request<T>(path, { method: 'GET', query })
  },
  post<T>(path: string, body?: unknown) {
    return request<T>(path, { method: 'POST', body })
  },
  put<T>(path: string, body?: unknown) {
    return request<T>(path, { method: 'PUT', body })
  },
  patch<T>(path: string, body?: unknown) {
    return request<T>(path, { method: 'PATCH', body })
  },
  delete<T>(path: string) {
    return request<T>(path, { method: 'DELETE' })
  },
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error || '未知错误')
}
