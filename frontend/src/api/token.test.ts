import { beforeEach, describe, expect, it } from 'vitest'
import { getToken } from './client'
import { bootstrapToken } from './token'

describe('bootstrapToken', () => {
  beforeEach(() => {
    sessionStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('stores the token and strips it from the URL', () => {
    window.history.replaceState(null, '', '/?t=abc123&foo=bar')

    const result = bootstrapToken()

    expect(result).toBe(true)
    expect(getToken()).toBe('abc123')
    expect(window.location.search).not.toContain('t=abc123')
    expect(window.location.search).toContain('foo=bar')
  })

  it('reuses a token already in sessionStorage when the URL has none', () => {
    sessionStorage.setItem('easymigrate.token', 'already-stored')

    const result = bootstrapToken()

    expect(result).toBe(true)
  })

  it('reports no token when the URL and sessionStorage are both empty', () => {
    const result = bootstrapToken()

    expect(result).toBe(false)
  })
})
