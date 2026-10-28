// The hosted build (easymigrate.sudhan.tech) is the same React app built with
// `--mode hosted` (.env.hosted). Read at call time so tests can stub the env per test.

export function isHosted(): boolean {
  return import.meta.env.VITE_HOSTED === '1'
}

export function apiBase(): string {
  return (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
}
