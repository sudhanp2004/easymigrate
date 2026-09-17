# EasyMigrate — Local Review UI (Spec 3)

Status: approved 2026-09-17
Builds on: `2026-09-15-core-pipeline-networking-design.md` (spec 1) and
`2026-09-16-databases-rds-cloudsql-design.md` (spec 2). Product truth lives in
`PRODUCT.md`; this spec does not restate it.

This spec covers the **architecture** of the local UI. It deliberately says nothing about
visual design, layout or copy — that is decided separately and recorded in `DESIGN.md`.

## 1. Scope

`easymigrate ui` starts a local web application that drives all five pipeline stages —
`scan → analyze → resolve → plan → apply` — so a customer's engineer never needs the
terminal after launching it.

### Why this exists

Two limitations are specific and documented, not matters of taste:

1. **Decision params cannot be entered at all today.** Spec 2 §2.1 states that interactive
   `resolve` shows params but never prompts for them; the user must hand-edit
   `decisions.yaml`. A self-serve engineer editing YAML by hand mid-migration is the
   product's worst moment.
2. **Reviewing differences in a terminal is poor.** A live run produced 22 differences
   rendered as a table with every key truncated to `DB-BACKUP-WINDOW@datab…`. The
   explanation of each difference is the product's value, and the terminal hides it.

The report is also the artifact a user reads before approving a migration that creates real
infrastructure. That is inherently a reading surface.

### In scope

- A FastAPI application serving a React SPA, launched by `easymigrate ui`.
- An HTTP surface over `core.pipeline` covering all five stages.
- Background jobs with streamed output, surviving a closed browser tab.
- Local-only security: loopback binding, a launch token, origin checks.

### Out of scope

- **Hosting.** Nothing here assumes a server the user does not control. Multi-tenancy,
  accounts, and credential custody are explicitly not designed for.
- **Replacing the CLI.** The CLI remains fully supported and operates on the same
  artifacts. The UI is an alternative front end, not a successor.
- **Changing the engine.** `core.pipeline` keeps its current signatures. If the UI needs a
  change there, that is a finding to raise, not a liberty to take.
- Visual design, which `DESIGN.md` owns.

## 2. Process and project model

`easymigrate ui [--port N] [--no-browser]` binds an HTTP server to `127.0.0.1` on an
ephemeral port (or `--port`) and opens the browser at the launch URL.

The server operates on **one project directory**: the `.easymigrate/` folder in the
current working directory, the same one the CLI uses. There is no database and no
server-side project registry — the filesystem is the state.

This is what makes the CLI and UI interchangeable: a user may run `easymigrate resolve` in
a terminal and then refresh the UI, or vice versa, and both see the same truth. Any design
that cached pipeline state in server memory would break that property and is forbidden.

The server exits on `Ctrl-C`. A running job is not killed by the browser closing (§4).

## 3. The callback contract

`core.pipeline` takes human interaction as **blocking callbacks**: `choose` per difference
in `resolve`, `confirm_bucket` and `ask_password` in `plan`, `confirm` in `apply`. A CLI
blocks on a prompt; an HTTP request cannot block for twenty minutes.

**Decision: every callback is pre-satisfied before a job starts.** Jobs never wait on a
human. Nothing pauses.

This is possible because the pipeline already separates deciding from executing:

| Callback | How the UI satisfies it |
|---|---|
| `choose` (resolve) | The UI collects decisions through ordinary requests and writes `decisions.yaml`, then calls `resolve` with `accept_defaults=True` and no `choose`. |
| `confirm_bucket` (plan) | The UI asks first, then passes `confirm_bucket=lambda _: True`. Declining simply does not start the job. |
| `ask_password` (plan) | Passwords are collected up front and passed as `db_passwords`; `ask_password` is `None`, so a missing one is a clean error rather than a hang. |
| `confirm` (apply) | The UI takes explicit confirmation, then passes `confirm=lambda _: True`. |

The rejected alternative was a pausable job that transitions to `awaiting_input` and
resumes. It is more flexible and considerably more complex — a state machine, resumption
semantics, and a class of bugs where a job waits forever for an answer nobody is being
asked for. The pipeline's existing stage boundaries make it unnecessary.

**The consequence to respect:** an `apply` that has been confirmed runs to completion
without further questions. That matches the CLI, where `apply` prompts once and then runs.

## 4. Jobs

Long-running stages (`scan`, `plan`, `apply`) run as background jobs. `analyze` and
`resolve` are fast and synchronous.

**One job at a time per project.** Concurrent runs against one Terraform state are
meaningless; the constraint is a feature, and the API returns `409` while a job is active.

Each job owns two files:

- `.easymigrate/jobs/<job-id>.json` — `{id, stage, status, started_at, finished_at, error}`
  where status is `running` | `succeeded` | `failed`.
- `.easymigrate/jobs/<job-id>.log` — the output stream, appended to by the `stream`
  callback the pipeline already accepts.

Because job state is on disk rather than in memory:

- A closed browser tab reconnects by reading the files; nothing is lost.
- A job survives the browser entirely — important when a single Cloud SQL instance takes
  16m44s to create.
- If the server itself dies mid-job, the record is left `running` with no process behind
  it. On startup the server marks any `running` job whose process is gone as `failed`,
  with an error saying the server stopped during that stage. It must never silently
  present a dead job as live.

**Streaming:** `GET /api/jobs/{id}/stream` is a Server-Sent Events endpoint that replays
the log from the beginning, then tails it, then sends a terminal event carrying the final
status. Replaying from the start is deliberate: a user who opens the page late still sees
the whole story.

`.easymigrate/jobs/` is added to the gitignore rules the project already writes.

## 5. HTTP surface

All endpoints are under `/api`. Every mutating request requires the launch token (§6).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/project` | Project state: which stages have run, their artifact timestamps, the active job if any, and whether `plan` is blocked by unacknowledged manual decisions. Drives the whole UI's sense of "where am I". |
| `POST` | `/api/scan` | Starts a scan job. Body: region, and one of profile or role ARN. |
| `POST` | `/api/analyze` | Runs `analyze` synchronously; returns findings and the object counts by kind. |
| `GET` | `/api/differences` | Every detected difference with its rule metadata: title, impact, and **the strategies allowed for that specific difference**, each with effect, risk, and whether it is manual. |
| `GET` | `/api/decisions` | Current decisions, including params and acknowledgement state. |
| `PUT` | `/api/decisions/{key}` | Sets one decision: strategy, optional params, optional acknowledgement. Validates params through the owning rule and returns the rule's own error message on rejection. |
| `POST` | `/api/resolve` | Writes `decisions.yaml` and reports readiness problems. |
| `POST` | `/api/plan` | Starts a plan job. Body: GCP project, optional region, passwords per instance, bucket-creation consent. |
| `GET` | `/api/report` | The report as structured JSON — the same data `report.json` carries. |
| `POST` | `/api/apply` | Starts an apply job. Requires the plan fingerprint the client last saw, so a stale page cannot apply a plan the user never reviewed. |
| `GET` | `/api/jobs/{id}` · `/api/jobs/{id}/stream` | Job status; SSE stream. |

Two details that matter more than they look:

- **`GET /api/differences` must return `allowed_strategies(diff)`, not the rule's class-level
  strategy tuple.** Five rules vary their strategy set per difference. Offering a strategy a
  difference disallows produces a crash later in `resolve_ir`; that exact bug has been
  introduced and caught twice already in this codebase.
- **`POST /api/apply` takes the fingerprint of the plan the user reviewed.** If the plan has
  been regenerated since, the request is rejected. `plan.lock.json` already exists for this
  purpose at the engine level; the API check is what stops a stale *page* from approving.

**Errors:** `EasyMigrateError` and friends map to `400` with the engine's own message
verbatim. The engine's errors are written for humans and already name the resource and the
next action; the UI must not paraphrase them into something vaguer.

## 6. Security

A local server that can create and destroy cloud infrastructure is a real attack surface.
Any website open in the same browser can send requests to `127.0.0.1`, and DNS rebinding
defeats naive origin checks. An unprotected `POST /api/apply` would let a visited page
touch the user's cloud.

Three required mitigations:

1. **A launch token.** Generated per run, included in the URL the browser opens, stored by
   the SPA in `sessionStorage`, and required as a header on every mutating request. A
   cross-site request cannot read it, so it cannot forge one.
2. **Origin and Host checks.** Requests whose `Origin` or `Host` is not the expected
   loopback address are rejected, which closes DNS rebinding.
3. **Loopback binding only.** `127.0.0.1`, never `0.0.0.0`. Not configurable — a flag to
   expose this server on a network is a foot-gun, and hosting is out of scope.

**Secrets:** database passwords travel over loopback HTTP, which is acceptable locally.
They are passed straight to `plan`, land only in the existing 0600
`secrets.auto.tfvars.json`, and are **never returned in any response**. The report's
existing sentence about the password being present in Terraform state is shown as-is.

`GET` endpoints are read-only and unauthenticated by the token check, but still
origin-checked. They expose account IDs and ARNs, which is why the origin check applies to
everything, not only mutations.

## 7. Packaging

- `src/easymigrate/ui/` — `app.py` (FastAPI application), `jobs.py` (job records and
  streaming), `security.py` (token, origin check), `routes/`.
- `frontend/` — Vite/React source, not shipped in the wheel.
- `src/easymigrate/ui/static/` — built assets, committed, served by FastAPI.

Committing built assets is what lets `easymigrate ui` work from a plain `pip` or `uv`
install with no node toolchain on the user's machine. A CI check rebuilds the frontend and
fails if the committed output differs from a fresh build, so the two cannot drift.

FastAPI, uvicorn and sse-starlette become dependencies of an optional `ui` extra, so a
CLI-only install stays lean.

## 8. Testing

Follows the existing patterns; no new infrastructure.

- API tests use FastAPI's `TestClient` against a temp project directory, with the GCP and
  OpenTofu seams faked exactly as `tests/core/test_pipeline_plan_apply.py` does.
- Security tests are mandatory and specific: a mutating request without the token is
  rejected; a request with a foreign `Origin` is rejected; the server refuses to bind
  anything but loopback; no response body ever contains a supplied password.
- Job tests: a job record survives a simulated disconnect; a `running` job whose process is
  gone is marked `failed` on startup; a second job while one is active returns `409`.
- Contract test: `/api/differences` returns `allowed_strategies(diff)` for a difference
  whose rule varies its set — this guards the bug class that has recurred twice.
- Frontend tests are deferred to the implementation plan, once the surfaces exist.

## 9. Done when

- `easymigrate ui` on a fresh install opens a browser and drives `scan → analyze →
  resolve → plan → apply` to completion against a real AWS account and GCP project,
  without the user touching a terminal.
- Decision params are editable in the UI, closing the spec 2 §2.1 limitation.
- Closing the browser during a 15-minute `apply` and reopening it shows the job still
  running, with its output from the beginning.
- The security tests in §8 pass.
- A CLI-only install is unaffected, and CLI and UI can be used interchangeably on one
  project.
