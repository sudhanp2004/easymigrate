export function Unauthenticated() {
  return (
    <main className="page">
      <h1>Not authenticated</h1>
      <p>
        This session has no valid launch token. Relaunch <code>easymigrate ui</code> from
        your terminal and open the URL it prints.
      </p>
    </main>
  )
}
