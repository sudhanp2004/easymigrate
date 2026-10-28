// Mirrors src/easymigrate/web/routes.py (WebState, AwsInfo, /api/aws/setup).
import type { ProjectState } from '../api/types'

export interface AwsInfo {
  region: string | null
  role_arn: string | null
  account_id: string | null
}

export interface HostedState extends ProjectState {
  aws: AwsInfo | null
  suggested_gcp_region: string | null
  expires_at: string
}

export interface AwsSetup {
  external_id: string
  stack_name: string
  role_name: string
  template_url: string
  regions: string[]
  quick_create_url: string
}

export interface HostedPageProps {
  project: {
    state: HostedState | null
    refresh: () => void
    activeJob: ProjectState['active_job']
  }
}
