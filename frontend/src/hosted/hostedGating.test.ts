import { describe, expect, it } from 'vitest'
import { firstIncompleteStep, stepProblems } from './hostedGating'
import type { HostedState } from './types'

function state(overrides: Partial<HostedState> = {}, done: string[] = []): HostedState {
  return {
    root: 'workspace',
    stages: ['scan', 'analyze', 'resolve'].map((name) => ({
      name,
      complete: done.includes(name),
      artifact_at: null,
    })),
    source: null,
    target: null,
    blocked: [],
    active_job: null,
    aws: null,
    suggested_gcp_region: null,
    expires_at: '2026-11-04T00:00:00Z',
    ...overrides,
  }
}

const connected = { aws: { region: 'ap-south-1', role_arn: 'arn', account_id: '123456789012' } }

describe('hosted steps', () => {
  it('starts at connect, then follows the stages', () => {
    expect(firstIncompleteStep(state())).toBe('connect')
    expect(firstIncompleteStep(state(connected))).toBe('scan')
    expect(firstIncompleteStep(state(connected, ['scan']))).toBe('review')
    expect(firstIncompleteStep(state(connected, ['scan', 'analyze']))).toBe('decisions')
    expect(firstIncompleteStep(state(connected, ['scan', 'analyze', 'resolve']))).toBe('download')
  })

  it('blocks a step until the previous one is done', () => {
    expect(stepProblems('scan', state())).toEqual(['Connect your AWS account first.'])
    expect(stepProblems('scan', state(connected))).toEqual([])
  })

  it('blocks download while decisions still need attention', () => {
    const s = state({ ...connected, blocked: ['NET-X needs a decision'] }, [
      'scan',
      'analyze',
      'resolve',
    ])
    expect(stepProblems('download', s)).toEqual(['NET-X needs a decision'])
    expect(firstIncompleteStep(s)).toBe('decisions')
  })
})
