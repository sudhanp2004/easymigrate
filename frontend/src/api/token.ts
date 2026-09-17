// Token bootstrap (spec §6): the server opens the browser at
// `http://127.0.0.1:<port>/?token=<token>`. On load we read `token`, store it in
// sessionStorage, and strip it from the URL immediately so it never lingers in the
// address bar, browser history, or a pasted screenshot.

import { getToken, setToken } from './client'

/** The query parameter `easymigrate ui` puts the launch token in. It must match
 * cli.py's launch URL exactly; tests/ui/test_cli_ui.py asserts that across the
 * Python/TypeScript boundary, which is where this drifted once already. */
export const TOKEN_PARAM = 'token'

/** Reads `?token=` from `location`, persists it, and strips it via `history.replaceState`.
 * Returns true if a token is available afterwards (freshly read or already stored). */
export function bootstrapToken(): boolean {
  const url = new URL(window.location.href)
  const fromUrl = url.searchParams.get(TOKEN_PARAM)

  if (fromUrl) {
    setToken(fromUrl)
    url.searchParams.delete(TOKEN_PARAM)
    window.history.replaceState(null, '', url.pathname + url.search + url.hash)
    return true
  }

  return getToken() !== null
}
