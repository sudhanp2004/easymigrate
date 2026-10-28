import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Connect } from './Connect'

const setup = {
  external_id: 'e'.repeat(32),
  stack_name: 'EasyMigrateReadOnly-abcd1234',
  role_name: 'EasyMigrateReadOnly-abcd1234',
  template_url: 'https://bucket.s3.amazonaws.com/deploy/role.json',
  regions: ['ap-south-1', 'us-east-1'],
  quick_create_url: 'https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/x',
}

describe('Connect', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_HOSTED', '1')
    vi.stubEnv('VITE_API_BASE', '')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.startsWith('/api/aws/setup')) return new Response(JSON.stringify(setup))
        if (url === '/api/aws/connect' && init?.method === 'POST')
          return new Response(JSON.stringify({ account_id: '123456789012' }))
        return new Response('{}', { status: 404 })
      }),
    )
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('links to the quick-create page and sends the trimmed role ARN', async () => {
    const refresh = vi.fn()
    render(
      <MemoryRouter>
        <Connect project={{ state: null, refresh, activeJob: null }} />
      </MemoryRouter>,
    )

    const link = await screen.findByRole('link', { name: /open aws/i })
    expect(link).toHaveAttribute('href', setup.quick_create_url)
    expect(link).toHaveAttribute('target', '_blank')

    fireEvent.change(screen.getByLabelText(/role arn/i), {
      target: { value: '  arn:aws:iam::123456789012:role/EasyMigrateReadOnly-abcd1234 \n' },
    })
    fireEvent.click(screen.getByRole('button', { name: /connect/i }))

    await screen.findByText(/connected to aws account 123456789012/i)
    const post = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/aws/connect')!
    expect(JSON.parse(post[1]!.body as string)).toEqual({
      region: 'us-east-1',
      role_arn: 'arn:aws:iam::123456789012:role/EasyMigrateReadOnly-abcd1234',
    })
    await waitFor(() => expect(refresh).toHaveBeenCalled())
  })
})
