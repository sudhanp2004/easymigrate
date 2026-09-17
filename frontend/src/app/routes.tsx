import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { AnalyzePage } from '../pages/AnalyzePage'
import { ScanPage } from '../pages/ScanPage'
import { StagePlaceholder } from '../pages/StagePlaceholder'
import { StageGate } from './StageGate'
import { STAGE_ORDER, firstIncompleteStage } from './stageGating'
import { useProject } from './useProject'

export function AppRoutes() {
  const project = useProject()

  return (
    <div className="app">
      <nav className="stage-nav">
        {STAGE_ORDER.map((stage) => (
          <NavLink key={stage} to={`/${stage}`} className={({ isActive }) => (isActive ? 'active' : '')}>
            {stage}
          </NavLink>
        ))}
      </nav>

      <Routes>
        <Route
          path="/"
          element={<Navigate to={`/${project.state ? firstIncompleteStage(project.state) : 'scan'}`} replace />}
        />
        <Route
          path="/scan"
          element={
            <StageGate stage="scan" state={project.state}>
              <ScanPage project={project} />
            </StageGate>
          }
        />
        <Route
          path="/analyze"
          element={
            <StageGate stage="analyze" state={project.state}>
              <AnalyzePage project={project} />
            </StageGate>
          }
        />
        <Route
          path="/resolve"
          element={
            <StageGate stage="resolve" state={project.state}>
              <StagePlaceholder title="Resolve" />
            </StageGate>
          }
        />
        <Route
          path="/plan"
          element={
            <StageGate stage="plan" state={project.state}>
              <StagePlaceholder title="Plan" />
            </StageGate>
          }
        />
        <Route
          path="/apply"
          element={
            <StageGate stage="apply" state={project.state}>
              <StagePlaceholder title="Apply" />
            </StageGate>
          }
        />
      </Routes>
    </div>
  )
}
