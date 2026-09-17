// Mirrors the server's view models (src/easymigrate/ui/state.py, jobs.py, views.py,
// routes/*.py). Keep field names identical to the Python side so the shapes can be
// grepped across languages.

export type Status = 'running' | 'succeeded' | 'failed'

export interface JobRecord {
  id: string
  stage: string
  status: Status
  started_at: string
  finished_at: string | null
  error: string | null
  pid: number
}

export interface StageState {
  name: string
  complete: boolean
  artifact_at: string | null
}

export interface ProjectState {
  root: string
  stages: StageState[]
  source: Record<string, unknown> | null
  target: Record<string, unknown> | null
  blocked: string[]
  active_job: JobRecord | null
}

export interface ScanRequest {
  region: string
  profile?: string | null
  role_arn?: string | null
}

export interface PlanRequest {
  gcp_project: string
  gcp_region?: string | null
  db_passwords?: Record<string, string>
  create_bucket?: boolean
}

export interface ApplyRequest {
  plan_fingerprint: string
}

export interface Finding {
  level: 'info' | 'unsupported' | 'scan_gap'
  code: string
  message: string
  subject: string | null
  suggestion: string | null
}

export interface AnalyzeResponse {
  counts: Record<string, number>
  findings: Finding[]
}

export type Risk = 'none' | 'low' | 'medium' | 'high'

export interface StrategyView {
  id: string
  title: string
  effect: string
  risk: Risk
  risk_note: string | null
  manual: boolean
  is_default: boolean
}

export interface DifferenceView {
  key: string
  rule_id: string
  family: string
  title: string
  impact: string
  subjects: string[]
  context: Record<string, unknown>
  strategies: StrategyView[]
  param_docs: Record<string, string>
}

export interface DecisionView {
  key: string
  strategy: string
  decided_by: 'default' | 'user'
  acknowledged: boolean
  params: Record<string, unknown>
  needs_acknowledgement: boolean
}

export interface DecisionsResponse {
  decisions: DecisionView[]
  blocked: string[]
}

export interface DecisionWriteRequest {
  strategy: string
  params?: Record<string, unknown> | null
  acknowledged?: boolean | null
}
