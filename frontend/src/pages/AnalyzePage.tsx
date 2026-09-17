import { useState } from 'react'
import { ApiError, api } from '../api/client'
import type { AnalyzeResponse } from '../api/types'
import type { UseProjectResult } from '../app/useProject'

interface AnalyzePageProps {
  project: UseProjectResult
}

export function AnalyzePage({ project }: AnalyzePageProps) {
  const [result, setResult] = useState<AnalyzeResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)

  async function runAnalyze() {
    setError(null)
    setRunning(true)
    try {
      const response = await api<AnalyzeResponse>('/api/analyze', { method: 'POST' })
      setResult(response)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'analyze failed')
    } finally {
      setRunning(false)
      project.refresh()
    }
  }

  return (
    <section className="page">
      <h1>Analyze</h1>
      <p>Builds the cloud-neutral IR from the scan and finds every AWS/GCP difference.</p>

      <button type="button" onClick={runAnalyze} disabled={running}>
        {running ? 'Analyzing…' : 'Run analyze'}
      </button>

      {error && <p className="error">{error}</p>}

      {result && (
        <div className="analyze-result">
          <h2>Object counts</h2>
          {Object.keys(result.counts).length === 0 ? (
            <p>No objects found.</p>
          ) : (
            <table>
              <tbody>
                {Object.entries(result.counts).map(([kind, count]) => (
                  <tr key={kind}>
                    <td>{kind}</td>
                    <td>{count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h2>Findings</h2>
          {result.findings.length === 0 ? (
            <p>No findings.</p>
          ) : (
            <ul className="findings">
              {result.findings.map((finding) => (
                <li key={`${finding.code}-${finding.subject ?? ''}`} className={`finding-${finding.level}`}>
                  <strong>{finding.level}</strong> [{finding.code}] {finding.message}
                  {finding.subject && <span> — {finding.subject}</span>}
                  {finding.suggestion && <p className="suggestion">{finding.suggestion}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
