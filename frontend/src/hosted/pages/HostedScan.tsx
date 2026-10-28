import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../../api/client'
import type { JobRecord } from '../../api/types'
import { useJobStream } from '../../api/useJobStream'
import type { HostedPageProps } from '../types'

export function HostedScan({ project }: HostedPageProps) {
  const aws = project.state?.aws
  const scanned = project.state?.stages.find((s) => s.name === 'scan')?.complete ?? false
  const [jobId, setJobId] = useState<string | null>(project.activeJob?.id ?? null)
  const [error, setError] = useState<string | null>(null)
  const { status, error: jobError } = useJobStream(jobId)

  useEffect(() => {
    if (status) project.refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  async function start() {
    setError(null)
    try {
      const job = await api<JobRecord>('/api/scan', { method: 'POST' })
      setJobId(job.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'The scan could not start.')
    }
  }

  const running = jobId !== null && status === null

  return (
    <section className="h-page">
      <h1>Scan your AWS setup</h1>
      <p className="h-lead">
        EasyMigrate reads the configuration of account <strong>{aws?.account_id}</strong> in{' '}
        <strong>{aws?.region}</strong>. Nothing in AWS is changed. A small account takes a minute or
        two; a large one can take longer.
      </p>

      <div className="h-card">
        {running ? (
          <p className="h-working">
            <span className="h-spinner" aria-hidden="true" /> Reading your AWS setup… you can leave this
            page open.
          </p>
        ) : (
          <button type="button" className="h-button" onClick={start}>
            {scanned ? 'Scan again' : 'Start scan'}
          </button>
        )}
        {status === 'succeeded' && <p className="h-ok">Scan finished.</p>}
        {status === 'failed' && <p className="error">{jobError}</p>}
        {error && <p className="error">{error}</p>}
      </div>

      {scanned && !running && (
        <Link className="h-button" to="/app/review">
          Next: review what was found
        </Link>
      )}
    </section>
  )
}
