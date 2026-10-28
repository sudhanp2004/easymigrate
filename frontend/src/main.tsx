import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { bootstrapToken } from './api/token'
import { App } from './App'
import './styles.css'

const root = createRoot(document.getElementById('root')!)

// Compared against the build-time constant directly (not through hosted/config) so Vite
// drops the hosted site, and its stylesheet, from the local `easymigrate ui` bundle.
if (import.meta.env.VITE_HOSTED === '1') {
  void import('./hosted/HostedApp').then(({ HostedApp }) =>
    root.render(
      <StrictMode>
        <HostedApp />
      </StrictMode>,
    ),
  )
} else {
  const authenticated = bootstrapToken()
  root.render(
    <StrictMode>
      <App authenticated={authenticated} />
    </StrictMode>,
  )
}
