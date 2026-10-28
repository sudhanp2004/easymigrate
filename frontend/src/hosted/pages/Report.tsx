import { useEffect, useState } from 'react'
import { ApiError, api } from '../../api/client'

// Plain text on purpose: the report quotes AWS names and tags, which visitors' accounts
// control; rendering it as HTML would need a sanitizer. The zip has the formatted file.
export function Report() {
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api<{ markdown: string }>('/api/report-markdown')
      .then((body) => setText(body.markdown))
      .catch((err) => setError(err instanceof ApiError ? err.detail : 'Could not load the report.'))
  }, [])

  return (
    <section className="h-page">
      <h1>Report</h1>
      {error && <p className="error">{error}</p>}
      {text === null && !error && <p>Loading…</p>}
      {text !== null && <pre className="h-report">{text}</pre>}
    </section>
  )
}
