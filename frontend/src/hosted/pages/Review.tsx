import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../../api/client'
import type { AnalyzeResponse } from '../../api/types'
import type { HostedPageProps } from '../types'

const KIND_NAMES: Record<string, string> = {
  network: 'Networks (VPCs)',
  subnet: 'Subnets',
  route_table: 'Route tables',
  firewall_policy: 'Security groups',
  network_acl: 'Network ACLs',
  nat_gateway: 'NAT gateways',
  internet_gateway: 'Internet gateways',
  database_instance: 'Databases',
  database_cluster: 'Database clusters',
  compute_instance: 'Servers (EC2)',
  disk: 'Disks',
  static_address: 'Fixed IP addresses',
  instance_template: 'Launch templates',
  instance_group: 'Auto Scaling groups',
  kubernetes_cluster: 'Kubernetes clusters',
  node_pool: 'Kubernetes node groups',
  bucket: 'Storage buckets',
  load_balancer: 'Load balancers',
  iam_role: 'IAM roles',
}

const LEVEL_NAMES: Record<string, string> = {
  scan_gap: 'Could not read',
  unsupported: 'Not migrated',
  info: 'Note',
}

export function Review({ project }: HostedPageProps) {
  const [result, setResult] = useState<AnalyzeResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api<AnalyzeResponse>('/api/analyze', { method: 'POST' })
      .then((response) => {
        setResult(response)
        project.refresh()
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : 'Could not read the scan.'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <section className="h-page">
      <h1>What EasyMigrate found</h1>
      <p className="h-lead">
        These are the resources EasyMigrate will recreate in Google Cloud, and anything it could not
        read or does not migrate.
      </p>
      {error && <p className="error">{error}</p>}
      {!result && !error && <p>Reading the scan…</p>}
      {result && (
        <>
          <div className="h-card">
            <h2>Resources</h2>
            {Object.keys(result.counts).length === 0 ? (
              <p>No resources were found in this region. Check you picked the right region.</p>
            ) : (
              <ul className="h-counts">
                {Object.entries(result.counts)
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([kind, count]) => (
                    <li key={kind}>
                      <span className="h-count">{count}</span> {KIND_NAMES[kind] ?? kind}
                    </li>
                  ))}
              </ul>
            )}
          </div>
          {result.findings.length > 0 && (
            <div className="h-card">
              <h2>Worth knowing</h2>
              <ul className="h-findings">
                {result.findings.map((finding) => (
                  <li key={`${finding.code}-${finding.subject ?? ''}`}>
                    <strong>{LEVEL_NAMES[finding.level] ?? finding.level}:</strong> {finding.message}
                    {finding.subject && <span className="h-subject"> ({finding.subject})</span>}
                    {finding.suggestion && <p className="h-hint">{finding.suggestion}</p>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <Link className="h-button" to="/app/decisions">
            Next: decisions
          </Link>
        </>
      )}
    </section>
  )
}
