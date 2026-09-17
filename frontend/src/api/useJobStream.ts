import { useEffect, useState } from 'react'
import type { Status } from './types'

export interface JobStreamState {
  lines: string[]
  status: Status | null
  error: string | null
}

interface TerminalPayload {
  status: Status
  error: string | null
}

/** Subscribes to `/api/jobs/{id}/stream` (spec §4, §5). The server replays the whole
 * log from byte 0 on every connection, so a component mounting late — or a dropped
 * connection reconnecting once — needs no catch-up logic beyond resetting `lines`. */
export function useJobStream(jobId: string | null): JobStreamState {
  const [lines, setLines] = useState<string[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLines([])
    setStatus(null)
    setError(null)
    if (!jobId) return

    let reconnected = false
    let closed = false
    let source: EventSource

    const connect = () => {
      setLines([])
      source = new EventSource(`/api/jobs/${jobId}/stream`)

      source.addEventListener('log', (event) => {
        const data = (event as MessageEvent<string>).data
        setLines((prev) => [...prev, data])
      })

      source.addEventListener('terminal', (event) => {
        const record = JSON.parse((event as MessageEvent<string>).data) as TerminalPayload
        setStatus(record.status)
        setError(record.error)
        closed = true
        source.close()
      })

      source.onerror = () => {
        if (closed) return
        source.close()
        if (!reconnected) {
          reconnected = true
          connect()
        }
      }
    }

    connect()
    return () => {
      closed = true
      source?.close()
    }
  }, [jobId])

  return { lines, status, error }
}
