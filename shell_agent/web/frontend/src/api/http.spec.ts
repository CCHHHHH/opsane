import { afterEach, describe, expect, it, vi } from 'vitest'

import { http } from './http'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('HTTP client', () => {
  it('serializes query values and returns JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ records: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await expect(http.get('/api/audit', { target: 'prod-1', limit: 50 })).resolves.toEqual({ records: [] })
    expect(fetchMock).toHaveBeenCalledWith('/api/audit?target=prod-1&limit=50', expect.objectContaining({ method: 'GET' }))
  })

  it('normalizes legacy error payloads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ ok: false, error: '保存失败' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(http.post('/api/config', {})).rejects.toEqual(expect.objectContaining({
      name: 'ApiError',
      message: '保存失败',
      status: 200,
    }))
  })

  it('uses a structured FastAPI detail message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { message: '候选与最新服务画像存在冲突' } }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    )))

    await expect(http.post('/api/candidate/rebase', {})).rejects.toEqual(expect.objectContaining({
      name: 'ApiError',
      message: '候选与最新服务画像存在冲突',
      status: 409,
    }))
  })
})
