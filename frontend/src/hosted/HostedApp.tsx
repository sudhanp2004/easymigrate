import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './hosted.css'
import { Guide } from './pages/Guide'
import { Landing } from './pages/Landing'
import { Privacy } from './pages/Privacy'
import { Wizard } from './Wizard'

export function HostedApp() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/guide" element={<Guide />} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/app/*" element={<Wizard />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
