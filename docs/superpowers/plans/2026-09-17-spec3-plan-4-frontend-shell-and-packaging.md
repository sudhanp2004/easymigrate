# Spec 3, Plan 4 of 4: Frontend Shell and Packaging

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** the React application's foundations — build pipeline into the wheel, API client
with token handling, live job streaming, and the stage-aware routing shell — so that
`easymigrate ui` on a plain `pip` install serves a working app.

**Scope boundary:** this plan builds the **shell and the plumbing**, not the screens'
visual design. What each surface looks like — layout, type, colour, motion, the way a
difference is presented — is decided separately and recorded in `DESIGN.md`, then built as
plan 5. This is a deliberate split, not deferred work: the plumbing below is determined by
the architecture and does not change when the visual direction is chosen.

**Spec:** `docs/superpowers/specs/2026-09-17-local-ui-design.md` §6 (client half), §7
(binding). Product truth, including the terminology the interface must use:
`PRODUCT.md`.

## Global Constraints
- **`easymigrate ui` must work from a plain `pip`/`uv` install with no node toolchain on
  the user's machine.** Built assets are committed to `src/easymigrate/ui/static/`. This is
  the constraint the React choice creates; everything below serves it.
- The token lives in `sessionStorage`, never `localStorage` (which would outlive the
  server it authenticates to) and never a cookie (which a cross-site request would send
  automatically, defeating the whole mitigation).
- The client never renders a password it sent, and never puts one in a URL.
- The UI uses the engine's vocabulary — difference, strategy, risk, manual, decision,
  params, finding — and does not invent synonyms.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Python side: `uv run pytest`, `ruff format --check .`, `ruff check .`. Frontend:
  `npm run build`, `npm run lint`, `npm test`. Every task ends green on both, then commits.

## File Structure
```
frontend/
├── package.json, vite.config.ts, tsconfig.json
├── index.html
└── src/
    ├── main.tsx            # entry; reads the token, mounts the app
    ├── api/client.ts       # typed fetch wrapper, token header, error mapping
    ├── api/types.ts        # mirrors the server's view models
    ├── api/useJobStream.ts # SSE subscription hook
    ├── app/routes.tsx      # stage-aware routing shell
    └── app/useProject.ts   # project state, polling, stage gating
src/easymigrate/ui/static/  # build output, committed
.github/workflows/          # + frontend build-and-drift job
pyproject.toml              # hatch: include static/ in the wheel
```

## Interfaces

### Token handling (`main.tsx`)
The server opens the browser at `http://127.0.0.1:<port>/?t=<token>`. On load the app
reads `t` from the query string, stores it in `sessionStorage`, and **immediately replaces
the URL** with `history.replaceState` so the token is not left in the address bar, in
browser history, or in any screenshot the user pastes into a bug report. A reload with no
`t` and nothing in `sessionStorage` shows a page saying the session is not authenticated
and to relaunch `easymigrate ui` — never a blank screen.

### `api/client.ts`
```ts
async function api<T>(path: string, init?: RequestInit): Promise<T>
class ApiError extends Error { status: number; detail: string }
```
- Adds the `X-EasyMigrate-Token` header — the name plan 1's server expects — to every
  mutating request, reading it from `sessionStorage`.
- Non-2xx → `ApiError` carrying the server's `detail` **verbatim**. The engine's messages
  already name the resource and the next action; the client must surface them, not
  replace them with "Something went wrong".
- A 403 means the token is stale (the server restarted) — the client says exactly that and
  tells the user to relaunch.

### `api/useJobStream.ts`
```ts
function useJobStream(jobId: string | null): {
  lines: string[]; status: Status | null; error: string | null
}
```
Opens an `EventSource` on `/api/jobs/{id}/stream`, appends lines, and closes on the
terminal event. Because the server replays the log from byte 0, a component mounting late
still shows the whole run — the hook needs no catch-up logic of its own. On unmount it
closes the stream; on a dropped connection it reconnects once and relies on replay for
correctness.

### `app/useProject.ts`
```ts
function useProject(): { state: ProjectState | null; refresh(): void; activeJob: JobRecord | null }
```
Fetches `/api/project`, and while a job is active polls it every few seconds so the shell
learns when a stage completes. Polling is deliberate and cheap: it is loopback, and it
keeps the client's model of "where am I" derived from the server's files rather than from
optimistic local state that could disagree with the CLI.

### `app/routes.tsx`
One route per stage — `/scan`, `/analyze`, `/resolve`, `/plan`, `/apply` — plus `/` which
redirects to the first incomplete stage. A stage whose prerequisites are unmet is
reachable but renders an explanation of what must happen first, taken from the server's
`blocked` list. **Stages are never hidden**: a user who wants to see what `resolve` will
ask must be able to look.

### Packaging
- `npm run build` outputs to `../src/easymigrate/ui/static/`, which is committed.
- `pyproject.toml` includes `static/**` in the wheel via hatch's `force-include` or
  `artifacts`, so the assets ship even though the directory is otherwise build output.
- **CI drift job:** installs node, runs `npm ci && npm run build`, and fails if
  `git diff --exit-code src/easymigrate/ui/static/` is non-empty. Committed build output
  silently drifts from source unless something checks it; this is that check.

## Required test cases (minimum)

**Client (`vitest` + Testing Library):**
- `api()` attaches the token header on `POST` and omits it on `GET`
- a non-2xx response produces `ApiError` carrying the server's `detail` unchanged
- a 403 produces the "relaunch the server" message, not a generic failure
- the token is removed from the URL after load, and is present in `sessionStorage`
- loading with no token and empty `sessionStorage` renders the unauthenticated notice
- `useJobStream` accumulates lines and stops on the terminal event
- `useProject` polls while a job is active and stops when it completes

**Python side:**
- a test asserting `src/easymigrate/ui/static/index.html` exists and the app serves it —
  this is what catches a release built without the frontend
- `easymigrate ui` on an install where `static/` is empty still starts and shows the
  "assets not built" page from plan 1 rather than crashing

## Tasks
1. Vite/React/TypeScript scaffold, `npm run build` wired to output into
   `src/easymigrate/ui/static/`, hatch packaging so the assets ship, and the Python test
   that the served `index.html` exists.
2. `api/client.ts` and `api/types.ts` with their tests, including the verbatim-error and
   token-header cases.
3. `main.tsx` token handling (read, store, strip from URL, unauthenticated notice) and its
   tests.
4. `useJobStream` and `useProject` with their tests.
5. `app/routes.tsx` — the stage shell with prerequisite explanations — plus the CI drift
   job and a README section on building the frontend.

## What comes next

Plan 5 builds the five stage surfaces themselves against `DESIGN.md`. It is not written
yet because the visual world is not chosen; writing it now would produce screens designed
by default rather than by decision. Run `/impeccable` new-work to establish that world
first.
