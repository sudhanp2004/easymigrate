import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useJobStream } from '../api/useJobStream'
import type { DifferenceView, JobRecord, PlanRequest } from '../api/types'
import type { UseProjectResult } from '../app/useProject'

interface PlanPageProps {
  project: UseProjectResult
}

/** Every primary database instance a decision could apply to (spec §3: `apply` needs one
 * password per non-excluded primary `database_instance`, never a replica). There is no
 * endpoint that lists this directly, so it is derived from `/api/differences`: any
 * subject id in the `database_instance:` namespace that a difference mentions. In
 * practice at least one database rule (e.g. `DB-STORAGE-MINIMUM`, `DB-USERS`) fires for
 * every real primary instance, so this covers the instances that matter; an instance
 * with truly no differences at all would be missed here and would surface instead as a
 * named-and-explained failure in the plan job's log, never silently. */
function databaseInstanceIds(differences: DifferenceView[]): string[] {
  const ids = new Set<string>()
  for (const diff of differences) {
    for (const subject of diff.subjects) {
      if (subject.startsWith('database_instance:')) ids.add(subject)
    }
  }
  return Array.from(ids).sort()
}

export function PlanPage({ project }: PlanPageProps) {
  const [gcpProject, setGcpProject] = useState('')
  const [gcpRegion, setGcpRegion] = useState('')
  // On by default: the first plan in a fresh project always needs the bucket, and leaving
  // this off made that run fail with a message that read like GCP refusing.
  const [createBucket, setCreateBucket] = useState(true)
  const [instanceIds, setInstanceIds] = useState<string[]>([])
  const [passwords, setPasswords] = useState<Record<string, string>>({})
  const [jobId, setJobId] = useState<string | null>(project.activeJob?.id ?? null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  const { lines, status, error: jobError } = useJobStream(jobId)

  useEffect(() => {
    api<DifferenceView[]>('/api/differences')
      .then((diffs) => setInstanceIds(databaseInstanceIds(diffs)))
      .catch(() => {
        // Non-fatal: the password fields simply stay empty; `plan` still names any
        // instance it cannot resolve a password for, in its own error.
      })
  }, [])

  async function startPlan(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setStarting(true)
    const body: PlanRequest = {
      gcp_project: gcpProject,
      gcp_region: gcpRegion || null,
      db_passwords: Object.fromEntries(
        Object.entries(passwords).filter(([, value]) => value.length > 0),
      ),
      create_bucket: createBucket,
    }
    try {
      const job = await api<JobRecord>('/api/plan', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setJobId(job.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'the plan could not be started')
    } finally {
      setStarting(false)
      project.refresh()
    }
  }

  const running = status === null && jobId !== null

  return (
    <section className="page">
      <h1>Plan</h1>
      <p>Generates the Terraform plan for the GCP side of the migration.</p>

      <form onSubmit={startPlan} className="form">
        <label>
          GCP project
          <input
            value={gcpProject}
            onChange={(e) => setGcpProject(e.target.value)}
            required
            disabled={running}
          />
        </label>
        <label>
          GCP region (optional)
          <input
            value={gcpRegion}
            onChange={(e) => setGcpRegion(e.target.value)}
            placeholder="us-central1"
            disabled={running}
          />
        </label>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={createBucket}
            onChange={(e) => setCreateBucket(e.target.checked)}
            disabled={running}
          />
          Create the Terraform state bucket if it does not exist
        </label>

        {instanceIds.length > 0 && (
          <fieldset>
            <legend>Database passwords</legend>
            {instanceIds.map((id) => (
              <label key={id}>
                {id}
                <input
                  type="password"
                  autoComplete="new-password"
                  value={passwords[id] ?? ''}
                  onChange={(e) => setPasswords((prev) => ({ ...prev, [id]: e.target.value }))}
                  disabled={running}
                />
              </label>
            ))}
          </fieldset>
        )}

        <button type="submit" disabled={starting || running}>
          {running ? 'Planning…' : 'Start plan'}
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
