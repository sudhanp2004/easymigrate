import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Download } from './Download'
import type { HostedState } from '../types'

const state: HostedState = {
  root: 'workspace',
  stages: [],
  source: null,
  target: null,
  blocked: [],
  active_job: null,
  aws: { region: 'ap-south-1', role_arn: 'arn', account_id: '123456789012' },
  suggested_gcp_region: 'asia-south1',
  expires_at: '2026-11-04T00:00:00Z',
}

describe('Download', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_HOSTED', '1')
    vi.stubEnv('VITE_API_BASE', '')
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('zip', {
            headers: { 'Content-Disposition': 'attachment; filename="easymigrate-x.zip"' },
          }),
      ),
    )
    URL.createObjectURL = vi.fn(() => 'blob:fake')
    URL.revokeObjectURL = vi.fn()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('posts the Google Cloud project and region and saves the zip', async () => {
    render(
      <MemoryRouter>
        <Download project={{ state, refresh: vi.fn(), activeJob: null }} />
      </MemoryRouter>,
    )

    expect(screen.getByLabelText(/google cloud region/i)).toHaveValue('asia-south1')
    fireEvent.change(screen.getByLabelText(/google cloud project id/i), {
      target: { value: 'my-proj-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: /download/i }))

    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled())
    const [url, init] = vi.mocked(fetch).mock.calls[0]
    expect(url).toBe('/api/download')
    expect(JSON.parse(init!.body as string)).toEqual({
      gcp_project: 'my-proj-1',
      gcp_region: 'asia-south1',
    })
  })
})
