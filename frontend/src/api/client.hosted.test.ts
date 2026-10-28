import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, TOKEN_HEADER, api, apiBlob, setToken } from './client'

describe('api() in the hosted build', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_HOSTED', '1')
    vi.stubEnv('VITE_API_BASE', 'https://api.example')
    vi.stubGlobal('fetch', vi.fn())
    sessionStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('calls the API host with credentials and the X-EasyMigrate header, never a token', async () => {
    setToken('local-token')
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ok: 1 }), { status: 202 }))

    await api('/api/scan', { method: 'POST' })

    const [url, init] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('https://api.example/api/scan')
    expect(init?.credentials).toBe('include')
    const headers = new Headers(init?.headers)
    expect(headers.get('X-EasyMigrate')).toBe('1')
    expect(headers.has(TOKEN_HEADER)).toBe(false)
  })

  it('sends credentials but no header on GET', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response('{}', { status: 200 }))
    await api('/api/project')
    const [, init] = vi.mocked(fetch).mock.calls[0]
    expect(init?.credentials).toBe('include')
    expect(new Headers(init?.headers).has('X-EasyMigrate')).toBe(false)
  })

  it('keeps the server detail for 401 and 403', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Your session has ended. Start again.' }), {
        status: 401,
      }),
    )
    const error = (await api('/api/project').catch((e: unknown) => e)) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(401)
    expect(error.detail).toBe('Your session has ended. Start again.')
  })

  it('apiBlob returns the blob and the filename from Content-Disposition', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response('zipbytes', {
        status: 200,
        headers: { 'Content-Disposition': 'attachment; filename="easymigrate-1-ap.zip"' },
      }),
    )
    const result = await apiBlob('/api/download', { method: 'POST', body: '{}' })
    expect(result.filename).toBe('easymigrate-1-ap.zip')
    expect(await result.blob.text()).toBe('zipbytes')
  })
})
