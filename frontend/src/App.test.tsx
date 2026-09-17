import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { App } from './App'

describe('App', () => {
  it('shows the unauthenticated notice when there is no token', () => {
    render(<App authenticated={false} />)
    expect(screen.getByText(/not authenticated/i)).toBeInTheDocument()
    expect(screen.getByText(/easymigrate ui/i)).toBeInTheDocument()
  })
})
