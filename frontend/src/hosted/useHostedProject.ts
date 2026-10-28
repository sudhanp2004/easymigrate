import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { HostedState } from './types'

export const STARTED_KEY = 'easymigrate.started'
const POLL_INTERVAL_MS = 2000

function started(): boolean {
  try {
    return sessionStorage.getItem(STARTED_KEY) === '1'
  } catch {
    return false
  }
}

function setStarted(value: boolean): void {
  try {
    if (value) sessionStorage.setItem(STARTED_KEY, '1')
    else sessionStorage.removeItem(STARTED_KEY)
  } catch {
    // storage blocked: a first visit is then simply assumed each time
  }
}

export interface HostedProject {
  state: HostedState | null
  refresh: () => void
  activeJob: HostedState['active_job']
  waking: boolean
  sessionEnded: boolean
  error: string | null
  restart: () => Promise<void>
  remove: () => Promise<void>
}

/** The visitor's workspace state. A first visit (no 401 seen in this tab yet) creates the
 * workspace; a 401 after that means the session ended and the page says so. */
export function useHostedProject(wakeAfterMs = 3000): HostedProject {
  const [state, setState] = useState<HostedState | null>(null)
  const [waking, setWaking] = useState(false)
  const [sessionEnded, setSessionEnded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const accept = useCallback((next: HostedState) => {
    setStarted(true)
    if (!mounted.current) return
    setState(next)
    setSessionEnded(false)
    setError(null)
  }, [])

  const load = useCallback(async () => {
    try {
      accept(await api<HostedState>('/api/project'))
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        if (started()) {
          if (mounted.current) setSessionEnded(true)
          return
        }
        try {
          await api('/api/workspaces', { method: 'POST' })
          accept(await api<HostedState>('/api/project'))
        } catch (inner) {
          if (mounted.current)
            setError(inner instanceof ApiError ? inner.detail : 'Could not reach EasyMigrate.')
        }
        return
      }
      if (mounted.current)
        setError(err instanceof ApiError ? err.detail : 'Could not reach EasyMigrate.')
    }
  }, [accept])

  useEffect(() => {
    const timer = setTimeout(() => setWaking(true), wakeAfterMs)
    void load().finally(() => {
      clearTimeout(timer)
      if (mounted.current) setWaking(false)
    })
    return () => clearTimeout(timer)
  }, [load, wakeAfterMs])

  const activeJobId = state?.active_job?.id ?? null
  useEffect(() => {
    if (!activeJobId) return
    const interval = setInterval(() => void load(), POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [activeJobId, load])

  const restart = useCallback(async () => {
    setStarted(false)
    setState(null)
    await load()
  }, [load])

  const remove = useCallback(async () => {
    await api('/api/workspace', { method: 'DELETE' })
    setStarted(false)
    setState(null)
  }, [])

  return {
    state,
    refresh: () => void load(),
    activeJob: state?.active_job ?? null,
    waking,
    sessionEnded,
    error,
    restart,
    remove,
  }
}
