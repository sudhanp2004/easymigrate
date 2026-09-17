import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, TOKEN_HEADER, api, setToken } from './client'

describe('api()', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('attaches the token header on POST', async () => {
    setToken('secret-token')
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    )

    await api('/api/scan', { method: 'POST', body: JSON.stringify({}) })

    const [, init] = vi.mocked(fetch).mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(headers.get(TOKEN_HEADER)).toBe('secret-token')
  })

  it('omits the token header on GET', async () => {
    setToken('secret-token')
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))

    await api('/api/project')

    const [, init] = vi.mocked(fetch).mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(headers.has(TOKEN_HEADER)).toBe(false)
  })

  it('carries the server detail verbatim on a non-2xx response', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'no scan found; run `easymigrate scan` first' }), {
        status: 400,
      }),
    )

    await expect(api('/api/analyze', { method: 'POST' })).rejects.toMatchObject({
      detail: 'no scan found; run `easymigrate scan` first',
      status: 400,
    })
  })

  it('produces a relaunch message, not a generic failure, on 403', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'forbidden' }), { status: 403 }),
    )

    try {
      await api('/api/scan', { method: 'POST' })
      expect.fail('expected api() to throw')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      const apiError = error as ApiError
      expect(apiError.status).toBe(403)
      expect(apiError.detail).toMatch(/relaunch/i)
      expect(apiError.detail).not.toBe('forbidden')
    }
  })
})
