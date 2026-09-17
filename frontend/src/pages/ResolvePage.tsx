import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import type {
  DecisionsResponse,
  DecisionView,
  DecisionWriteRequest,
  DifferenceView,
  Risk,
} from '../api/types'
import type { UseProjectResult } from '../app/useProject'

interface ResolvePageProps {
  project: UseProjectResult
}

/** String-editable form of a decision's params: every value round-trips through a plain
 * text input, then is parsed back to the JSON type it started as (spec §5: `params` is
 * `dict[str, Any]`, and the three param kinds in use today are a string, a number, and a
 * list of strings — see `DB-PRIVATE-IP`/`DB-PUBLIC-ACCESS` in database.py). */
function toFieldStrings(params: Record<string, unknown>): Record<string, string> {
  const fields: Record<string, string> = {}
  for (const [key, value] of Object.entries(params)) {
    fields[key] = Array.isArray(value) ? value.join(', ') : String(value)
  }
  return fields
}

function parseField(original: unknown, text: string): unknown {
  if (Array.isArray(original)) {
    return text
      .split(',')
      .map((item) => item.trim())
      .filter((item) => item.length > 0)
  }
  if (typeof original === 'number') {
    const parsed = Number(text)
    return Number.isNaN(parsed) ? text : parsed
  }
  return text
}

function riskLabel(risk: Risk): string {
  return risk === 'none' ? 'no risk' : `${risk} risk`
}

function needsAttention(diff: DifferenceView, decision: DecisionView | null): boolean {
  if (!decision) return true
  const strategy = diff.strategies.find((s) => s.id === decision.strategy)
  if (strategy?.manual && !decision.acknowledged) return true
  if (strategy?.risk === 'high') return true
  return false
}

/** The server's param validation errors name the param and the reason (e.g. "range:
 * 10.0.0.0/16 overlaps an existing VPC CIDR"), so a message can usually be pinned to
 * the field it is about by checking whether it mentions that param's name. Falls back
 * to showing the whole message once, under the params block, when it does not. */
function paramForError(paramNames: string[], message: string): string | null {
  return paramNames.find((name) => message.includes(name)) ?? null
}

export function ResolvePage({ project }: ResolvePageProps) {
  const [differences, setDifferences] = useState<DifferenceView[] | null>(null)
  const [decisions, setDecisions] = useState<Record<string, DecisionView>>({})
  const [blocked, setBlocked] = useState<string[]>([])
  const [paramFields, setParamFields] = useState<Record<string, Record<string, string>>>({})
  const [loadError, setLoadError] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [savingKey, setSavingKey] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoadError(null)
    try {
      const [decisionsResponse, diffs] = await Promise.all([
        api<DecisionsResponse>('/api/resolve', { method: 'POST' }),
        api<DifferenceView[]>('/api/differences'),
      ])
      applyDecisionsResponse(decisionsResponse)
      setDifferences(diffs)
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.detail : 'could not load differences')
    }
  }, [])

  function applyDecisionsResponse(response: DecisionsResponse) {
    const byKey: Record<string, DecisionView> = {}
    const fields: Record<string, Record<string, string>> = {}
    for (const decision of response.decisions) {
      byKey[decision.key] = decision
      fields[decision.key] = toFieldStrings(decision.params)
    }
    setDecisions(byKey)
    setParamFields((prev) => ({ ...prev, ...fields }))
    setBlocked(response.blocked)
  }

  useEffect(() => {
    load()
  }, [load])

  async function writeDecision(key: string, body: DecisionWriteRequest) {
    setSavingKey(key)
    setErrors((prev) => ({ ...prev, [key]: '' }))
    try {
      await api<DecisionView>(`/api/decisions/${encodeURIComponent(key)}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      })
      const response = await api<DecisionsResponse>('/api/resolve', { method: 'POST' })
      applyDecisionsResponse(response)
    } catch (err) {
      setErrors((prev) => ({
        ...prev,
        [key]: err instanceof ApiError ? err.detail : 'could not save this decision',
      }))
    } finally {
      setSavingKey(null)
      project.refresh()
    }
  }

  function selectStrategy(diff: DifferenceView, strategyId: string) {
    writeDecision(diff.key, { strategy: strategyId })
  }

  function saveParams(diff: DifferenceView, decision: DecisionView) {
    const fields = paramFields[diff.key] ?? {}
    const params: Record<string, unknown> = {}
    for (const [name, original] of Object.entries(decision.params)) {
      params[name] = parseField(original, fields[name] ?? '')
    }
    writeDecision(diff.key, { strategy: decision.strategy, params })
  }

  function setAcknowledged(diff: DifferenceView, decision: DecisionView, acknowledged: boolean) {
    writeDecision(diff.key, { strategy: decision.strategy, acknowledged })
  }

  function setParamField(key: string, name: string, value: string) {
    setParamFields((prev) => ({ ...prev, [key]: { ...prev[key], [name]: value } }))
  }

  const families = useMemo(() => {
    if (!differences) return []
    const byFamily = new Map<string, DifferenceView[]>()
    for (const diff of differences) {
      const list = byFamily.get(diff.family) ?? []
      list.push(diff)
      byFamily.set(diff.family, list)
    }
    for (const list of byFamily.values()) {
      list.sort((a, b) => {
        const attentionDelta =
          Number(needsAttention(b, decisions[b.key] ?? null)) -
          Number(needsAttention(a, decisions[a.key] ?? null))
        return attentionDelta !== 0 ? attentionDelta : a.key.localeCompare(b.key)
      })
    }
    return Array.from(byFamily.entries()).sort(([a], [b]) => a.localeCompare(b))
  }, [differences, decisions])

  return (
    <section className="page">
      <h1>Resolve</h1>
      <p>
        Every difference AWS/GCP found, and how each will be handled. Pick a strategy per
        difference, fill in any params it needs, and acknowledge manual ones.
      </p>

      {loadError && <p className="error">{loadError}</p>}

      {blocked.length > 0 && (
        <div className="blocked-banner">
          <h2>Blocks plan</h2>
          <ul>
            {blocked.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      )}

      {differences && differences.length === 0 && <p>No differences were found.</p>}

      {families.map(([family, diffs]) => (
        <div className="family-group" key={family}>
          <h2>{family}</h2>
          {diffs.map((diff) => {
            const decision = decisions[diff.key] ?? null
            const attention = needsAttention(diff, decision)
            const selectedStrategy = decision
              ? diff.strategies.find((s) => s.id === decision.strategy)
              : null
            const fields = paramFields[diff.key] ?? {}
            const error = errors[diff.key]
            const paramNames = decision ? Object.keys(decision.params) : []
            const errorParam = error ? paramForError(paramNames, error) : null

            return (
              <article
                key={diff.key}
                className={`difference${attention ? ' needs-attention' : ''}`}
              >
                <header>
                  <h3>{diff.title}</h3>
                  <code className="difference-key">{diff.key}</code>
                </header>
                <p className="impact">{diff.impact}</p>
                {diff.subjects.length > 0 && (
                  <p className="subjects">Affects: {diff.subjects.join(', ')}</p>
                )}

                <fieldset className="strategies" disabled={savingKey === diff.key}>
                  <legend>Strategy</legend>
                  {diff.strategies.map((strategy) => (
                    <label key={strategy.id} className={`strategy-option risk-${strategy.risk}`}>
                      <input
                        type="radio"
                        name={`strategy-${diff.key}`}
                        checked={decision?.strategy === strategy.id}
                        onChange={() => selectStrategy(diff, strategy.id)}
                      />
                      <span className="strategy-title">
                        {strategy.title}
                        {strategy.is_default && <em> (default)</em>}
                        {strategy.manual && <em className="manual-tag"> manual</em>}
                      </span>
                      <span className={`risk-badge risk-badge-${strategy.risk}`}>
                        {riskLabel(strategy.risk)}
                      </span>
                      <p className="strategy-effect">{strategy.effect}</p>
                      {strategy.risk_note && (
                        <p className="risk-note">{strategy.risk_note}</p>
                      )}
                    </label>
                  ))}
                </fieldset>

                {decision && paramNames.length > 0 && (
                  <div className="params">
                    <h4>Params</h4>
                    {paramNames.map((name) => (
                      <div key={name} className="param-field">
                        <label>
                          {diff.param_docs[name] ?? name}
                          <input
                            value={fields[name] ?? ''}
                            onChange={(e) => setParamField(diff.key, name, e.target.value)}
                            disabled={savingKey === diff.key}
                          />
                        </label>
                        {errorParam === name && <p className="error">{error}</p>}
                      </div>
                    ))}
                    <button
                      type="button"
                      onClick={() => saveParams(diff, decision)}
                      disabled={savingKey === diff.key}
                    >
                      Save params
                    </button>
                  </div>
                )}

                {selectedStrategy?.manual && decision && (
                  <label className="acknowledge">
                    <input
                      type="checkbox"
                      checked={decision.acknowledged}
                      onChange={(e) => setAcknowledged(diff, decision, e.target.checked)}
                      disabled={savingKey === diff.key}
                    />
                    I will handle this manually
                  </label>
                )}

                {decision?.needs_acknowledgement && (
                  <p className="warning">This manual decision is not acknowledged yet.</p>
                )}

                {error && !errorParam && <p className="error">{error}</p>}
              </article>
            )
          })}
        </div>
      ))}
    </section>
  )
}
