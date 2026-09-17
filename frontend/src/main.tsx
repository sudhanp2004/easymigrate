import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { bootstrapToken } from './api/token'
import { App } from './App'
import './styles.css'

const authenticated = bootstrapToken()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App authenticated={authenticated} />
  </StrictMode>,
)
