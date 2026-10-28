import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, apiBlob } from '../../api/client'
import type { HostedPageProps } from '../types'

export function Download({ project }: HostedPageProps) {
  const suggested = project.state?.suggested_gcp_region ?? ''
  const [gcpProject, setGcpProject] = useState('')
  const [gcpRegion, setGcpRegion] = useState(suggested)
  const [saved, setSaved] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function download(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const { blob, filename } = await apiBlob('/api/download', {
        method: 'POST',
        body: JSON.stringify({ gcp_project: gcpProject.trim(), gcp_region: gcpRegion.trim() || null }),
      })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
      setSaved(filename)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'The download failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="h-page">
      <h1>Download your Google Cloud version</h1>
      <p className="h-lead">
        You get a zip with the Terraform code for Google Cloud and the full report. Terraform is a
        standard tool that creates cloud resources from code; the zip's README walks you through
        running it.
      </p>

      <form className="h-card" onSubmit={download}>
        <label className="h-field">
          Google Cloud project ID (optional)
          <input
            value={gcpProject}
            onChange={(e) => setGcpProject(e.target.value)}
            placeholder="my-gcp-project"
            spellCheck={false}
          />
        </label>
        <p className="h-hint">Where the resources will go. Leave it empty to fill it in later.</p>
        <label className="h-field">
          Google Cloud region
          <input
            value={gcpRegion}
            onChange={(e) => setGcpRegion(e.target.value)}
            placeholder="e.g. asia-south1"
            required={!suggested}
            spellCheck={false}
          />
        </label>
        <p className="h-hint">
          {suggested
            ? `The closest match to your AWS region is ${suggested}.`
            : 'EasyMigrate has no automatic match for your AWS region; pick the Google Cloud region nearest to it.'}
        </p>
        <button type="submit" className="h-button" disabled={busy}>
          {busy ? 'Preparing…' : 'Download the zip'}
        </button>
        {error && <p className="error">{error}</p>}
      </form>

      {saved && (
        <div className="h-card h-success">
          <p>
            Saved <strong>{saved}</strong>. Open <code>report.md</code> first, then follow{' '}
            <code>README.md</code> to create everything in Google Cloud.
          </p>
          <p>
            Changed a decision? Download again any time; the zip is rebuilt from your latest choices.
            When you're done, you can delete your data (top right) and the{' '}
            <code>EasyMigrateReadOnly-…</code> stack in AWS.
          </p>
        </div>
      )}
      <p>
        <Link to="/app/report">Read the report in your browser</Link>
      </p>
    </section>
  )
}
