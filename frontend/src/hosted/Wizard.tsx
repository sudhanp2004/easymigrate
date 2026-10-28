import type { ReactNode } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { Brand } from '../app/Brand'
import {
  HOSTED_STEPS,
  STEP_LABELS,
  type HostedStep,
  firstIncompleteStep,
  stepComplete,
  stepProblems,
} from './hostedGating'
import { Connect } from './pages/Connect'
import { Decisions } from './pages/Decisions'
import { Download } from './pages/Download'
import { HostedScan } from './pages/HostedScan'
import { Report } from './pages/Report'
import { Review } from './pages/Review'
import type { HostedPageProps, HostedState } from './types'
import { type HostedProject, useHostedProject } from './useHostedProject'

function Shell({ project, children }: { project?: HostedProject; children: ReactNode }) {
  const navigate = useNavigate()

  async function deleteData() {
    if (!project) return
    if (!window.confirm('Delete everything EasyMigrate holds for this session? This cannot be undone.'))
      return
    await project.remove()
    navigate('/')
  }

  return (
    <div className="h-app">
      <header className="h-app-header">
        <Link to="/" className="h-brand-link" aria-label="EasyMigrate home">
          <Brand />
        </Link>
        <nav className="h-app-links">
          <Link to="/guide" target="_blank" rel="noopener">
            Guide
          </Link>
          {project?.state && (
            <button type="button" className="h-link-button" onClick={deleteData}>
              Delete my data
            </button>
          )}
        </nav>
      </header>
      {children}
    </div>
  )
}

function Progress({ state }: { state: HostedState }) {
  return (
    <ol className="h-progress">
      {HOSTED_STEPS.map((step, index) => (
        <li key={step} className={stepComplete(step, state) ? 'done' : ''}>
          <NavLink to={`/app/${step}`} className={({ isActive }) => (isActive ? 'current' : '')}>
            <span className="h-step-number">{stepComplete(step, state) ? '✓' : index + 1}</span>
            {STEP_LABELS[step]}
          </NavLink>
        </li>
      ))}
    </ol>
  )
}

function Gate({ step, state, children }: { step: HostedStep; state: HostedState; children: ReactNode }) {
  const problems = stepProblems(step, state)
  if (problems.length === 0) return <>{children}</>
  const next = firstIncompleteStep(state)
  return (
    <section className="h-card">
      <h2>Not ready yet</h2>
      <ul>
        {problems.map((problem) => (
          <li key={problem}>{problem}</li>
        ))}
      </ul>
      <Link className="h-button" to={`/app/${next}`}>
        Go to {STEP_LABELS[next]}
      </Link>
    </section>
  )
}

export function Wizard() {
  const project = useHostedProject()

  if (project.sessionEnded) {
    return (
      <Shell>
        <section className="h-card">
          <h1>Your session has ended</h1>
          <p>
            EasyMigrate deletes everything 7 days after your last visit, or when you delete it
            yourself. Start again to make a new report. If you created an AWS role for the old
            session, delete its <code>EasyMigrateReadOnly-…</code> stack in CloudFormation.
          </p>
          <button type="button" className="h-button" onClick={() => void project.restart()}>
            Start again
          </button>
        </section>
      </Shell>
    )
  }

  const state = project.state
  if (!state) {
    return (
      <Shell>
        <section className="h-card h-loading">
          {project.error ? (
            <p className="error">{project.error}</p>
          ) : project.waking ? (
            <p>Waking the server up. The free server sleeps when nobody uses it, so this can take up to a minute…</p>
          ) : (
            <p>Loading…</p>
          )}
        </section>
      </Shell>
    )
  }

  const props: HostedPageProps = { project }
  const page = (step: HostedStep, element: ReactNode) => (
    <Gate step={step} state={state}>
      {element}
    </Gate>
  )

  return (
    <Shell project={project}>
      <Progress state={state} />
      <main className="h-main">
        <Routes>
          <Route index element={<Navigate to={firstIncompleteStep(state)} replace />} />
          <Route path="connect" element={<Connect {...props} />} />
          <Route path="scan" element={page('scan', <HostedScan {...props} />)} />
          <Route path="review" element={page('review', <Review {...props} />)} />
          <Route path="decisions" element={page('decisions', <Decisions {...props} />)} />
          <Route path="download" element={page('download', <Download {...props} />)} />
          <Route path="report" element={page('download', <Report />)} />
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Routes>
      </main>
    </Shell>
  )
}
