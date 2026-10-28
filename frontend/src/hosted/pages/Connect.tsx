import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../../api/client'
import type { AwsSetup, HostedPageProps } from '../types'

export function Connect({ project }: HostedPageProps) {
  const connected = project.state?.aws
  const [region, setRegion] = useState(connected?.region ?? 'us-east-1')
  const [setup, setSetup] = useState<AwsSetup | null>(null)
  const [roleArn, setRoleArn] = useState('')
  const [accountId, setAccountId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    api<AwsSetup>(`/api/aws/setup?region=${encodeURIComponent(region)}`)
      .then((next) => live && setSetup(next))
      .catch((err) => live && setError(err instanceof ApiError ? err.detail : 'Could not load the setup.'))
    return () => {
      live = false
    }
  }, [region])

  async function connect(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const result = await api<{ account_id: string }>('/api/aws/connect', {
        method: 'POST',
        body: JSON.stringify({ region, role_arn: roleArn.trim() }),
      })
      setAccountId(result.account_id)
      project.refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Could not connect to AWS.')
    } finally {
      setBusy(false)
    }
  }

  const doneAccount = accountId ?? connected?.account_id ?? null

  return (
    <section className="h-page">
      <h1>Connect your AWS account</h1>
      <p className="h-lead">
        EasyMigrate needs to <strong>read</strong> your AWS setup. You give it access by creating a
        small read-only role in your own AWS account. You never type a password or an access key
        here, and you can remove the role whenever you like.
      </p>

      <div className="h-card">
        <h2>
          <span className="h-num">1</span> Choose the AWS region to scan
        </h2>
        <label className="h-field">
          AWS region to scan
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            {(setup?.regions ?? [region]).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <p className="h-hint">The region your servers and databases run in, e.g. ap-south-1 (Mumbai).</p>
      </div>

      <div className="h-card">
        <h2>
          <span className="h-num">2</span> Create the read-only role in AWS
        </h2>
        <a
          className="h-button"
          href={setup?.quick_create_url}
          target="_blank"
          rel="noopener noreferrer"
          aria-disabled={!setup}
        >
          Open AWS and create the role ↗
        </a>
        <ol className="h-steps">
          <li>Sign in to the AWS account you want to move. The page opens with everything filled in.</li>
          <li>
            Scroll to the bottom and tick <em>“I acknowledge that AWS CloudFormation might create IAM
            resources with custom names.”</em>
          </li>
          <li>
            Click <strong>Create stack</strong> and wait about a minute until it says{' '}
            <strong>CREATE_COMPLETE</strong>.
          </li>
          <li>
            Open the <strong>Outputs</strong> tab and copy the <strong>RoleArn</strong> value.
          </li>
        </ol>
        <details className="h-details">
          <summary>What does this role allow?</summary>
          <p>
            It can only <em>read</em> configuration: networks, servers, databases, load balancers,
            Kubernetes clusters, storage bucket settings and IAM roles. It cannot change, create or
            delete anything, and it cannot read your data (files in buckets, database rows).
          </p>
          <p>
            Only EasyMigrate can use it, and only with a secret code (an “external ID”) made for
            this browser session. CloudFormation is AWS's tool for creating resources from a
            template; you can{' '}
            {setup ? (
              <a href={setup.template_url} target="_blank" rel="noopener noreferrer">
                read the exact template
              </a>
            ) : (
              'read the exact template'
            )}
            . To remove access later, delete the <code>{setup?.stack_name ?? 'EasyMigrateReadOnly'}</code>{' '}
            stack in CloudFormation.
          </p>
        </details>
      </div>

      <form className="h-card" onSubmit={connect}>
        <h2>
          <span className="h-num">3</span> Paste the role ARN
        </h2>
        <label className="h-field">
          Role ARN
          <input
            value={roleArn}
            onChange={(e) => setRoleArn(e.target.value)}
            placeholder="arn:aws:iam::123456789012:role/EasyMigrateReadOnly-…"
            spellCheck={false}
            autoComplete="off"
            required
          />
        </label>
        <p className="h-hint">An ARN is AWS's full name for something. This one names your new role.</p>
        <button type="submit" className="h-button" disabled={busy || !roleArn.trim()}>
          {busy ? 'Checking…' : 'Connect'}
        </button>
        {error && <p className="error">{error}</p>}
      </form>

      {doneAccount && (
        <div className="h-card h-success">
          <p>
            Connected to AWS account {doneAccount}
            {connected?.region && !accountId ? ` (${connected.region})` : ''}.
          </p>
          <Link className="h-button" to="/app/scan">
            Next: scan
          </Link>
        </div>
      )}
    </section>
  )
}
