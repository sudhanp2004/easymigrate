import type { ReactNode } from 'react'
import type { ProjectState } from '../api/types'
import { prerequisiteProblems, type StageName } from './stageGating'

interface StageGateProps {
  stage: StageName
  state: ProjectState | null
  children: ReactNode
}

/** Renders `children` when a stage's prerequisites are met, otherwise an explanation of
 * what must happen first. Stages are never hidden — this only decides what shows. */
export function StageGate({ stage, state, children }: StageGateProps) {
  if (!state) return <p>Loading project state…</p>

  const problems = prerequisiteProblems(stage, state)
  if (problems.length === 0) return <>{children}</>

  return (
    <div className="stage-blocked">
      <h2>Not ready yet</h2>
      <ul>
        {problems.map((problem) => (
          <li key={problem}>{problem}</li>
        ))}
      </ul>
    </div>
  )
}
