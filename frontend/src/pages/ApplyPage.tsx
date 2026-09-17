import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useJobStream } from '../api/useJobStream'
import type { JobRecord } from '../api/types'
import type { UseProjectResult } from '../app/useProject'

interface ApplyPageProps {
  project: UseProjectResult
}

interface DatabaseRow {
  id: string
  tier: string
  tier_source: string
  connectivity: string
  open_to_world: boolean
  backups: string
  pitr: string
  dropped_parameters: Record<string, string>
}

interface PlanSummary {
  add: number
  change: number
  destroy: number
  changed_or_destroyed: string[]
  plan_text_path: string
  fingerprint: string | null
}

interface ReportResponse {
  source: string | null
  target: string | null
  coverage: Record<string, number>
  needs_action: string[]
  databases: DatabaseRow[]
  plan: PlanSummary | null
}

export function ApplyPage({ project }: ApplyPageProps) {
  const [report, setReport] = useState<ReportResponse | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [jobId, setJobId] = useState<string | null>(
    project.activeJob?.stage === 'apply' ? project.activeJob.id : null,
  )
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  const { lines, status, error: jobError } = useJobStream(jobId)

  useEffect(() => {
    api<ReportResponse>('/api/report')
      .then(setReport)
      .catch((err) => setLoadError(err instanceof ApiError ? err.detail : 'could not load the report'))
  }, [])

  // A closed and reopened tab must find the same running job rather than lose it
  // (spec: apply can run over fifteen minutes) — `useProject` polls `/api/project`
  // regardless of this page, so its `activeJob` catches up on the next tick.
  useEffect(() => {
    if (project.activeJob?.stage === 'apply' && jobId === null) {
      setJobId(project.activeJob.id)
    }
  }, [project.activeJob, jobId])

  async function startApply() {
    if (!report?.plan?.fingerprint) {
      setError('no reviewed plan to apply; run plan again')
      return
    }
    setError(null)
    setStarting(true)
    try {
      const job = await api<JobRecord>('/api/apply', {
        method: 'POST',
        body: JSON.stringify({ plan_fingerprint: report.plan.fingerprint }),
      })
      setJobId(job.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'the apply could not be started')
    } finally {
      setStarting(false)
      project.refresh()
    }
  }

  return (
    <section className="page">
      <h1>Apply</h1>
      <p>
        Creates and configures the real GCP infrastructure. A migration with database
        instances can easily run <strong>over fifteen minutes</strong> — this page is safe
        to close and reopen; the run keeps going on the server and picks back up here.
      </p>

      {loadError && <p className="error">{loadError}</p>}

      {report && !jobId && (
        <div className="report">
          <h2>Report</h2>
          <p>
            {report.source ?? '?'} → {report.target ?? '?'}
          </p>

          {report.needs_action.length > 0 && (
            <div className="blocked-banner">
              <h3>Needs your action</h3>
              <ul>
                {report.needs_action.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {report.databases.length > 0 && (
            <div className="databases">
              <h3>Databases</h3>
              {report.databases.map((db) => (
                <div key={db.id} className={db.open_to_world ? 'database-row world-open' : 'database-row'}>
                  <h4>{db.id}</h4>
                  <p>
                    Tier: {db.tier} (from {db.tier_source})
                  </p>
                  <p>Connectivity: {db.connectivity}</p>
                  {db.open_to_world && (
                    <p className="warning">
                      This database is reachable from the whole internet (0.0.0.0/0), as it
                      was on AWS.
                    </p>
                  )}
                  <p>Backups: {db.backups}</p>
                  <p>Point-in-time recovery: {db.pitr}</p>
                </div>
              ))}
            </div>
          )}

          {report.plan ? (
            <div className="plan-summary">
              <h3>Plan summary</h3>
              <p>
                add: {report.plan.add}, change: {report.plan.change}, destroy:{' '}
                {report.plan.destroy}
              </p>
              {report.plan.changed_or_destroyed.length > 0 && (
                <ul>
                  {report.plan.changed_or_destroyed.map((addr) => (
                    <li key={addr}>{addr}</li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <p className="error">No reviewed plan is available; run plan again.</p>
          )}

          <label className="acknowledge">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I have read this report and want to apply it to GCP now.
          </label>

          <button type="button" onClick={startApply} disabled={!confirmed || starting}>
            {starting ? 'Starting…' : 'Apply'}
          </button>
        </div>
      )}

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
