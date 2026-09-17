import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useProject } from './useProject'
import type { ProjectState } from '../api/types'

function projectState(overrides: Partial<ProjectState> = {}): ProjectState {
  return {
    root: '/tmp/project',
    stages: [],
    source: null,
    target: null,
    blocked: [],
    active_job: null,
    ...overrides,
  }
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200 })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('useProject', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('polls while a job is active and stops when it completes', async () => {
    const running = projectState({
      active_job: {
        id: 'job-1',
        stage: 'scan',
        status: 'running',
        started_at: '2026-01-01T00:00:00Z',
        finished_at: null,
        error: null,
        pid: 123,
      },
    })
    const finished = projectState({ active_job: null })

    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(running))
      .mockResolvedValueOnce(jsonResponse(running))
      .mockResolvedValue(jsonResponse(finished))

    const { result } = renderHook(() => useProject())

    await flush()
    expect(result.current.state?.active_job?.id).toBe('job-1')
    expect(fetch).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(fetch).toHaveBeenCalledTimes(2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(result.current.state?.active_job).toBeNull()
    expect(fetch).toHaveBeenCalledTimes(3)

    const callsBeforeWait = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(fetch).toHaveBeenCalledTimes(callsBeforeWait)
  })
})
