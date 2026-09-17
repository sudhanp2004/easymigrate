import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ResolvePage } from './ResolvePage'
import type { DifferenceView, DecisionView, DecisionsResponse } from '../api/types'
import type { UseProjectResult } from '../app/useProject'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status })
}

function fakeProject(): UseProjectResult {
  return { state: null, refresh: vi.fn(), activeJob: null }
}

const netDiff: DifferenceView = {
  key: 'NET-ZONE-REGIONAL@vpc-1',
  rule_id: 'NET-ZONE-REGIONAL',
  family: 'network',
  title: 'Zonal subnet becomes regional',
  impact: 'GCP subnets are regional; AWS ones are zonal.',
  subjects: ['vpc:vpc-1'],
  context: {},
  strategies: [
    {
      id: 'merge_regional',
      title: 'Merge into one regional subnet',
      effect: 'one regional subnet replaces every zonal one',
      risk: 'low',
      risk_note: null,
      manual: false,
      is_default: true,
    },
  ],
  param_docs: {},
}

const dbDiff: DifferenceView = {
  key: 'DB-PUBLIC-ACCESS@database_instance:db-1',
  rule_id: 'DB-PUBLIC-ACCESS',
  family: 'database',
  title: 'A publicly accessible RDS instance has no direct Cloud SQL equivalent',
  impact: 'Cloud SQL restricts access with an authorized-networks CIDR allow list.',
  subjects: ['database_instance:db-1'],
  context: { open_to_world: true },
  strategies: [
    {
      id: 'public_ip_allow_cidrs',
      title: 'Keep the public IP and allow CIDRs you supply',
      effect: 'authorized networks come from the `cidrs` param',
      risk: 'high',
      risk_note: 'You are choosing who can reach this database over the internet.',
      manual: false,
      is_default: true,
    },
    {
      id: 'manual',
      title: 'Handle manually',
      effect: 'excluded',
      risk: 'low',
      risk_note: null,
      manual: true,
      is_default: false,
    },
  ],
  param_docs: {
    range: 'Network address of the allocated Private Services Access range.',
    cidrs: "CIDRs allowed to reach the public IP, e.g. ['203.0.113.4/32'].",
  },
}

function decisionsResponse(decisions: DecisionView[], blocked: string[] = []): DecisionsResponse {
  return { decisions, blocked }
}

describe('ResolvePage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('renders the strategies the difference itself returns, not a fixed global list', async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/resolve' && init?.method === 'POST') {
        return jsonResponse(
          decisionsResponse([
            {
              key: netDiff.key,
              strategy: 'merge_regional',
              decided_by: 'default',
              acknowledged: false,
              params: {},
              needs_acknowledgement: false,
            },
            {
              key: dbDiff.key,
              strategy: 'public_ip_allow_cidrs',
              decided_by: 'default',
              acknowledged: false,
              params: { range: '10.60.0.0', cidrs: ['203.0.113.4/32'] },
              needs_acknowledgement: false,
            },
          ]),
        )
      }
      if (url === '/api/differences') {
        return jsonResponse([netDiff, dbDiff])
      }
      throw new Error(`unexpected request: ${init?.method ?? 'GET'} ${url}`)
    })

    render(<ResolvePage project={fakeProject()} />)

    await waitFor(() => expect(screen.getByText(netDiff.title)).toBeInTheDocument())

    // Each difference only shows the strategies its own view carries.
    expect(screen.getByText('Merge into one regional subnet')).toBeInTheDocument()
    expect(screen.getByText('Keep the public IP and allow CIDRs you supply')).toBeInTheDocument()
    expect(screen.getByText('Handle manually')).toBeInTheDocument()
    // The network difference must not somehow have picked up the database strategy.
    const networkArticle = screen.getByText(netDiff.title).closest('article')!
    expect(networkArticle).not.toHaveTextContent('Handle manually')
  })

  it('shows a param validation error next to its field, verbatim', async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/resolve' && init?.method === 'POST') {
        return jsonResponse(
          decisionsResponse([
            {
              key: dbDiff.key,
              strategy: 'public_ip_allow_cidrs',
              decided_by: 'default',
              acknowledged: false,
              params: { range: '10.60.0.0', cidrs: ['203.0.113.4/32'] },
              needs_acknowledgement: false,
            },
          ]),
        )
      }
      if (url === '/api/differences') {
        return jsonResponse([dbDiff])
      }
      if (url.startsWith('/api/decisions/') && init?.method === 'PUT') {
        return jsonResponse(
          { detail: 'range: 10.60.0.0/16 overlaps an existing VPC CIDR' },
          400,
        )
      }
      throw new Error(`unexpected request: ${init?.method ?? 'GET'} ${url}`)
    })

    render(<ResolvePage project={fakeProject()} />)

    await waitFor(() => expect(screen.getByText(dbDiff.title)).toBeInTheDocument())

    fireEvent.click(screen.getByText('Save params'))

    const message = await screen.findByText('range: 10.60.0.0/16 overlaps an existing VPC CIDR')
    // The message must land next to the "range" field, not the "cidrs" one.
    const rangeField = screen.getByText(dbDiff.param_docs.range).closest('.param-field')!
    expect(rangeField).toContainElement(message)
    const cidrsField = screen.getByText(dbDiff.param_docs.cidrs).closest('.param-field')!
    expect(cidrsField).not.toContainElement(message)
  })

  it('shows an unacknowledged manual decision as needing attention', async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/resolve' && init?.method === 'POST') {
        return jsonResponse(
          decisionsResponse([
            {
              key: dbDiff.key,
              strategy: 'manual',
              decided_by: 'user',
              acknowledged: false,
              params: {},
              needs_acknowledgement: true,
            },
          ]),
        )
      }
      if (url === '/api/differences') {
        return jsonResponse([dbDiff])
      }
      throw new Error(`unexpected request: ${init?.method ?? 'GET'} ${url}`)
    })

    render(<ResolvePage project={fakeProject()} />)

    await waitFor(() => expect(screen.getByText(dbDiff.title)).toBeInTheDocument())

    const article = screen.getByText(dbDiff.title).closest('article')!
    expect(article.className).toContain('needs-attention')
    expect(screen.getByText(/not acknowledged yet/i)).toBeInTheDocument()
  })
})
