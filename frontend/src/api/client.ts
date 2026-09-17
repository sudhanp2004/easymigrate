// Typed fetch wrapper (spec §6, §7). Every mutating request needs the per-run token the
// server prints when it launches the browser; GETs are read-only and carry no token by
// design (a page load must work with a token still parked in sessionStorage from a
// previous run, whether or not the server accepts it for reads).

export const TOKEN_HEADER = 'X-EasyMigrate-Token'
const TOKEN_STORAGE_KEY = 'easymigrate.token'

const STALE_TOKEN_MESSAGE =
  'This session is no longer authenticated (the server may have restarted). Relaunch `easymigrate ui`.'

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token)
  } catch {
    // sessionStorage can throw in locked-down browser contexts; the token then simply
    // does not persist across reloads, which the unauthenticated notice already handles.
  }
}

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function detailFromBody(body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (detail !== undefined) return JSON.stringify(detail)
  }
  return 'the server returned an error with no detail'
}

function isMutating(method: string | undefined): boolean {
  const upper = (method ?? 'GET').toUpperCase()
  return upper !== 'GET' && upper !== 'HEAD'
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (isMutating(init.method)) {
    const token = getToken()
    if (token) headers.set(TOKEN_HEADER, token)
  }
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })

  if (!response.ok) {
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      // no JSON body; fall through with a generic detail below
    }
    if (response.status === 403) {
      throw new ApiError(403, STALE_TOKEN_MESSAGE)
    }
    throw new ApiError(response.status, detailFromBody(body))
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}
