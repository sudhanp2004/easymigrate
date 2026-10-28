import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Brand } from '../app/Brand'

export function SiteLayout({ children }: { children: ReactNode }) {
  return (
    <div className="h-site">
      <header className="h-site-header">
        <Link to="/" className="h-brand-link" aria-label="EasyMigrate home">
          <Brand />
        </Link>
        <nav className="h-site-nav">
          <Link to="/guide">Guide</Link>
          <Link to="/privacy">Privacy</Link>
          <Link to="/app" className="h-button h-button-small">
            Start free
          </Link>
        </nav>
      </header>
      <main>{children}</main>
      <footer className="h-footer">
        <p>
          EasyMigrate · free, read-only report · <Link to="/guide">Guide</Link> ·{' '}
          <Link to="/privacy">Privacy</Link> ·{' '}
          <a href="https://github.com/sudhanp2004/easymigrate" target="_blank" rel="noopener noreferrer">
            Command-line version
          </a>
        </p>
        <p className="h-hint">Provided as is, without warranty. Always review the plan before you apply it.</p>
      </footer>
    </div>
  )
}
