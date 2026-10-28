import { Link } from 'react-router-dom'
import { ResolvePage } from '../../pages/ResolvePage'
import type { HostedPageProps } from '../types'

export function Decisions({ project }: HostedPageProps) {
  const ready = project.state ? project.state.blocked.length === 0 : false
  return (
    <section className="h-page">
      <div className="h-intro">
        <h1>Decide how each difference is handled</h1>
        <p className="h-lead">
          AWS and Google Cloud don't work exactly the same way. Each difference below already has a
          safe default chosen, so you can usually just continue. Change one if you prefer another
          option. Anything marked as manual needs a tick to confirm you have read it.
        </p>
      </div>
      <ResolvePage project={project} />
      {ready && (
        <Link className="h-button" to="/app/download">
          Next: download
        </Link>
      )}
    </section>
  )
}
