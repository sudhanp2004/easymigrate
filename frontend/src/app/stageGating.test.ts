import { describe, expect, it } from 'vitest'
import { firstIncompleteStage, prerequisiteProblems } from './stageGating'
import type { ProjectState } from '../api/types'

function state(overrides: Partial<ProjectState> = {}): ProjectState {
  return {
    root: '/tmp/project',
    stages: [
      { name: 'scan', complete: false, artifact_at: null },
      { name: 'analyze', complete: false, artifact_at: null },
      { name: 'resolve', complete: false, artifact_at: null },
      { name: 'plan', complete: false, artifact_at: null },
      { name: 'apply', complete: false, artifact_at: null },
    ],
    source: null,
    target: null,
    blocked: [],
    active_job: null,
    ...overrides,
  }
}

describe('firstIncompleteStage', () => {
  it('starts at scan when nothing is complete', () => {
    expect(firstIncompleteStage(state())).toBe('scan')
  })

  it('advances past completed stages', () => {
    const withScan = state({
      stages: state().stages.map((s) => (s.name === 'scan' ? { ...s, complete: true } : s)),
    })
    expect(firstIncompleteStage(withScan)).toBe('analyze')
  })
})

describe('prerequisiteProblems', () => {
  it('has no problems for scan', () => {
    expect(prerequisiteProblems('scan', state())).toEqual([])
  })

  it('blocks analyze until scan completes', () => {
    expect(prerequisiteProblems('analyze', state())).toHaveLength(1)
  })

  it('surfaces server blocked reasons for plan', () => {
    const completeThroughResolve = state({
      stages: state().stages.map((s) =>
        ['scan', 'analyze', 'resolve'].includes(s.name) ? { ...s, complete: true } : s,
      ),
      blocked: ['difference db-engine has no decision'],
    })
    expect(prerequisiteProblems('plan', completeThroughResolve)).toEqual([
      'difference db-engine has no decision',
    ])
  })
})
