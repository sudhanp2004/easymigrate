import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { JobRecord, ProjectState } from '../api/types'

const POLL_INTERVAL_MS = 2000

export interface UseProjectResult {
  state: ProjectState | null
  refresh: () => void
  activeJob: JobRecord | null
}

/** Fetches `/api/project` and, while a job is active, polls it so the shell learns when
 * a stage completes (spec §5, §7). The server's files are the source of truth; this hook
 * never guesses at "where am I" from optimistic local state. */
export function useProject(): UseProjectResult {
  const [state, setState] = useState<ProjectState | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const refresh = useCallback(() => {
    api<ProjectState>('/api/project')
      .then((next) => {
        if (mounted.current) setState(next)
      })
      .catch(() => {
        // A transient failure leaves the last known state on screen; the next poll
        // (or a user-triggered refresh) will retry.
      })
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const activeJobId = state?.active_job?.id ?? null
  useEffect(() => {
    if (!activeJobId) return
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [activeJobId, refresh])

  return { state, refresh, activeJob: state?.active_job ?? null }
}
