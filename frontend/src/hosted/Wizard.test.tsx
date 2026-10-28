import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { Wizard } from './Wizard'
import { STARTED_KEY } from './useHostedProject'

describe('Wizard', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_HOSTED', '1')
    vi.stubEnv('VITE_API_BASE', '')
    sessionStorage.clear()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('shows the start-again notice when an existing session has ended', async () => {
    sessionStorage.setItem(STARTED_KEY, '1')
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: 'Your session has ended. Start again.' }), {
            status: 401,
          }),
      ),
    )
    render(
      <MemoryRouter initialEntries={['/app']}>
        <Routes>
          <Route path="/app/*" element={<Wizard />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByText(/your session has ended/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start again/i })).toBeInTheDocument()
  })

  it('creates a workspace on a first visit', async () => {
    const calls: string[] = []
    let created = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? 'GET'} ${url}`)
        if (url === '/api/workspaces') {
          created = true
          return new Response('{"ws8":"abcd1234"}', { status: 201 })
        }
        if (!created) return new Response('{"detail":"no"}', { status: 401 })
        return new Response(
          JSON.stringify({
            root: 'workspace',
            stages: [],
            source: null,
            target: null,
            blocked: [],
            active_job: null,
            aws: null,
            suggested_gcp_region: null,
            expires_at: '2026-11-04T00:00:00Z',
          }),
        )
      }),
    )
    render(
      <MemoryRouter initialEntries={['/app/connect']}>
        <Routes>
          <Route path="/app/*" element={<Wizard />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByText(/connect your aws account/i, { selector: 'h1' })).toBeInTheDocument()
    expect(calls.slice(0, 3)).toEqual([
      'GET /api/project',
      'POST /api/workspaces',
      'GET /api/project',
    ])
  })

  it('says the server is waking up when it is slow', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
    render(
      <MemoryRouter initialEntries={['/app']}>
        <Routes>
          <Route path="/app/*" element={<Wizard />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.queryByText(/waking the server up/i)).not.toBeInTheDocument()
    await act(async () => {
      vi.advanceTimersByTime(3100)
    })
    expect(screen.getByText(/waking the server up/i)).toBeInTheDocument()
  })

  it('shows the start-again notice when any action gets a 401', async () => {
    let expired = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (expired) return new Response('{"detail":"Your session has ended."}', { status: 401 })
        if (url === '/api/project')
          return new Response(
            JSON.stringify({
              root: 'workspace',
              stages: [],
              source: null,
              target: null,
              blocked: [],
              active_job: null,
              aws: null,
              suggested_gcp_region: null,
              expires_at: '2026-11-04T00:00:00Z',
            }),
          )
        return new Response('{}')
      }),
    )
    render(
      <MemoryRouter initialEntries={['/app/connect']}>
        <Routes>
          <Route path="/app/*" element={<Wizard />} />
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByText(/connect your aws account/i, { selector: 'h1' })
    expired = true
    await act(async () => {
      await api('/api/scan', { method: 'POST' }).catch(() => undefined)
    })
    expect(await screen.findByRole('button', { name: /start again/i })).toBeInTheDocument()
  })
})
