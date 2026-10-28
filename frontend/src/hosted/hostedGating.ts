import type { HostedState } from './types'

export const HOSTED_STEPS = ['connect', 'scan', 'review', 'decisions', 'download'] as const
export type HostedStep = (typeof HOSTED_STEPS)[number]

export const STEP_LABELS: Record<HostedStep, string> = {
  connect: 'Connect AWS',
  scan: 'Scan',
  review: 'Review',
  decisions: 'Decisions',
  download: 'Download',
}

const STAGE_FOR: Partial<Record<HostedStep, string>> = {
  scan: 'scan',
  review: 'analyze',
  decisions: 'resolve',
}

const BEFORE_MESSAGE: Record<HostedStep, string> = {
  connect: '',
  scan: 'Connect your AWS account first.',
  review: 'Run the scan first.',
  decisions: 'Review what the scan found first.',
  download: 'Go through the decisions first.',
}

export function stepComplete(step: HostedStep, state: HostedState): boolean {
  if (step === 'connect') return Boolean(state.aws?.account_id)
  if (step === 'download') return false
  const stage = state.stages.find((s) => s.name === STAGE_FOR[step])
  if (!stage?.complete) return false
  return step !== 'decisions' || state.blocked.length === 0
}

export function firstIncompleteStep(state: HostedState): HostedStep {
  for (const step of HOSTED_STEPS) {
    if (!stepComplete(step, state)) return step
  }
  return 'download'
}

export function stepProblems(step: HostedStep, state: HostedState): string[] {
  const index = HOSTED_STEPS.indexOf(step)
  if (index === 0) return []
  const previous = HOSTED_STEPS[index - 1]
  if (step === 'download') {
    const resolved = state.stages.find((s) => s.name === 'resolve')?.complete
    if (!resolved) return [BEFORE_MESSAGE.download]
    return [...state.blocked]
  }
  return stepComplete(previous, state) ? [] : [BEFORE_MESSAGE[step]]
}
