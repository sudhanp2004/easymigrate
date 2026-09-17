import type { ProjectState } from '../api/types'

export const STAGE_ORDER = ['scan', 'analyze', 'resolve', 'plan', 'apply'] as const
export type StageName = (typeof STAGE_ORDER)[number]

const PREREQUISITE: Record<StageName, StageName | null> = {
  scan: null,
  analyze: 'scan',
  resolve: 'analyze',
  plan: 'resolve',
  apply: 'plan',
}

/** The first stage that is not yet complete, for `/` to redirect to. Defaults to
 * "apply" (the last stage) once every stage is complete. */
export function firstIncompleteStage(state: ProjectState): StageName {
  for (const name of STAGE_ORDER) {
    const stage = state.stages.find((candidate) => candidate.name === name)
    if (!stage?.complete) return name
  }
  return 'apply'
}

/** Reasons a stage cannot be worked on yet, taken from the server's own state: the
 * immediately preceding stage's completion, and — for plan/apply, which need every
 * difference decided — the server's `blocked` readiness problems (spec §5, §7). Stages
 * are never hidden; this only decides whether to show the stage or an explanation. */
export function prerequisiteProblems(stage: StageName, state: ProjectState): string[] {
  const problems: string[] = []
  const requires = PREREQUISITE[stage]
  if (requires) {
    const required = state.stages.find((candidate) => candidate.name === requires)
    if (!required?.complete) {
      problems.push(`Run "${requires}" first — it has not completed yet.`)
    }
  }
  if ((stage === 'plan' || stage === 'apply') && state.blocked.length > 0) {
    problems.push(...state.blocked)
  }
  return problems
}
