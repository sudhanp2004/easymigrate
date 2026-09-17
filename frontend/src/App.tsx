import { BrowserRouter } from 'react-router-dom'
import { AppRoutes } from './app/routes'
import { Unauthenticated } from './app/Unauthenticated'

interface AppProps {
  authenticated: boolean
}

export function App({ authenticated }: AppProps) {
  if (!authenticated) return <Unauthenticated />

  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  )
}
