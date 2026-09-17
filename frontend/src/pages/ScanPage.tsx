import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useJobStream } from '../api/useJobStream'
import type { JobRecord, ScanRequest } from '../api/types'
import type { UseProjectResult } from '../app/useProject'

interface ScanPageProps {
  project: UseProjectResult
}

export function ScanPage({ project }: ScanPageProps) {
  const [region, setRegion] = useState('')
  const [profile, setProfile] = useState('')
  const [roleArn, setRoleArn] = useState('')
  const [jobId, setJobId] = useState<string | null>(project.activeJob?.id ?? null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  const { lines, status, error: jobError } = useJobStream(jobId)

  async function startScan(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setStarting(true)
    const body: ScanRequest = {
      region,
      profile: profile || null,
      role_arn: roleArn || null,
    }
    try {
      const job = await api<JobRecord>('/api/scan', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setJobId(job.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'the scan could not be started')
    } finally {
      setStarting(false)
      project.refresh()
    }
  }

  const running = status === null && jobId !== null

  return (
    <section className="page">
      <h1>Scan</h1>
      <p>Reads a live AWS region into a snapshot for analysis.</p>

      <form onSubmit={startScan} className="form">
        <label>
          Region
          <input
            value={region}
            onChange={(e) => setRegion(e.target.value)}
            placeholder="us-east-1"
            required
            disabled={running}
          />
        </label>
        <label>
          AWS profile
          <input
            value={profile}
            onChange={(e) => setProfile(e.target.value)}
            placeholder="default"
            disabled={running}
          />
        </label>
        <label>
          Role ARN
          <input
            value={roleArn}
            onChange={(e) => setRoleArn(e.target.value)}
            placeholder="arn:aws:iam::123456789012:role/EasyMigrate"
            disabled={running}
          />
        </label>
        <button type="submit" disabled={starting || running}>
          {running ? 'Scanning…' : 'Start scan'}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {jobId && (
        <div className="job-log">
          <h2>Log</h2>
          <pre>{lines.join('\n')}</pre>
          {status && <p className={`status status-${status}`}>Status: {status}</p>}
          {jobError && <p className="error">{jobError}</p>}
        </div>
      )}
    </section>
  )
}
