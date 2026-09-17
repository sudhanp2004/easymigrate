// Token bootstrap (spec §6): the server opens the browser at
// `http://127.0.0.1:<port>/?t=<token>`. On load we read `t`, store it in
// sessionStorage, and strip it from the URL immediately so it never lingers in the
// address bar, browser history, or a pasted screenshot.

import { getToken, setToken } from './client'

/** Reads `?t=` from `location`, persists it, and strips it via `history.replaceState`.
 * Returns true if a token is available afterwards (freshly read or already stored). */
export function bootstrapToken(): boolean {
  const url = new URL(window.location.href)
  const fromUrl = url.searchParams.get('t')

  if (fromUrl) {
    setToken(fromUrl)
    url.searchParams.delete('t')
    window.history.replaceState(null, '', url.pathname + url.search + url.hash)
    return true
  }

  return getToken() !== null
}
