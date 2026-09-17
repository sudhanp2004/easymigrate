import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJobStream } from './useJobStream'

type Listener = (event: MessageEvent) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  listeners: Record<string, Listener[]> = {}
  onerror: (() => void) | null = null
  closed = false

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: Listener) {
    ;(this.listeners[type] ??= []).push(listener)
  }

  emit(type: string, data: string) {
    for (const listener of this.listeners[type] ?? []) {
      listener({ data } as MessageEvent)
    }
  }

  close() {
    this.closed = true
  }
}

describe('useJobStream', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('accumulates lines and stops on the terminal event', async () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const source = FakeEventSource.instances[0]

    act(() => {
      source.emit('log', 'starting scan')
      source.emit('log', 'scan complete')
    })
    expect(result.current.lines).toEqual(['starting scan', 'scan complete'])

    act(() => {
      source.emit('terminal', JSON.stringify({ status: 'succeeded', error: null }))
    })

    await waitFor(() => expect(result.current.status).toBe('succeeded'))
    expect(source.closed).toBe(true)
  })

  it('returns no lines and no status when jobId is null', () => {
    const { result } = renderHook(() => useJobStream(null))
    expect(result.current.lines).toEqual([])
    expect(result.current.status).toBeNull()
    expect(FakeEventSource.instances).toHaveLength(0)
  })
})
