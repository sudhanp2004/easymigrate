# Spec 3, Plan 2 of 4: Background Jobs and Streamed Output

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `scan`, `plan` and `apply` run as background jobs whose state lives on disk, so a
closed browser tab reconnects to a fifteen-minute apply and sees its output from the
beginning.

**Spec:** `docs/superpowers/specs/2026-09-17-local-ui-design.md` §3, §4, §5 (job and stage
endpoints) — binding.

## Global Constraints
- **Jobs never block on a human.** Every pipeline callback is pre-satisfied before the job
  starts (spec §3). No job may enter a state where it waits for input. If a task seems to
  need one, that is a design failure to raise, not a state to add.
- **Job state is on disk, not in memory.** The server process holds a handle to a running
  job, but the truth a request reads is the record file. This is what survives a closed
  tab, and what lets a dead job be detected.
- One job at a time per project; starting a second while one runs is `409`.
- `.easymigrate/` is already gitignored wholesale by `Project.ensure_gitignored`, so
  `jobs/` inside it needs no new rule. (Spec §4 says otherwise; the spec is wrong on this
  point — fix the spec line in the same commit.)
- A password must never appear in a job record, a log file, or any response.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/ui/jobs.py        # JobRecord, JobStore, runner, reaper
src/easymigrate/ui/routes/stages.py   # POST /api/scan, /api/plan, /api/apply
src/easymigrate/ui/routes/jobs.py     # GET /api/jobs/{id}, /api/jobs/{id}/stream
src/easymigrate/core/project.py   # + jobs_dir property
tests/ui/{test_jobs.py,test_stage_routes.py,test_job_stream.py}
```

## Interfaces

### `core/project.py`
`jobs_dir -> Path` (`.easymigrate/jobs`), created on demand, matching the existing
path-property style.

### `jobs.py`
```python
Status = Literal["running", "succeeded", "failed"]

class JobRecord(StrictModel):
    id: str                      # uuid4 hex
    stage: str                   # "scan" | "plan" | "apply"
    status: Status
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None     # the engine's message, verbatim
    pid: int                     # the server process that owns it

class JobStore:
    def __init__(self, project: Project) -> None
    def active(self) -> JobRecord | None
    def get(self, job_id: str) -> JobRecord          # KeyError -> 404 at the route
    def log_path(self, job_id: str) -> Path
    def start(self, stage: str, work: Callable[[Callable[[str], None]], None]) -> JobRecord
    def reap_stale(self) -> list[JobRecord]
```

- `start` refuses with `JobAlreadyRunning` when `active()` is not None. It writes the
  record **before** launching the work, so a crash between the two cannot orphan a running
  thread with no record.
- `work` receives a `stream` callable that appends a line to the log file. That is exactly
  the `stream` parameter `pipeline.apply` already takes, so no engine change is needed.
- The work runs in a `threading.Thread` (not a process): the pipeline is I/O-bound on cloud
  APIs and OpenTofu, the thread must share the `Project`, and killing it is never required
  — jobs are not cancellable in this plan, which is honest, since a half-cancelled `tofu
  apply` is worse than a completed one.
- On completion the record is rewritten with `status`, `finished_at`, and on failure the
  exception's message. Writes are atomic (reuse `project.write_json`), so a reader never
  sees a half-written record.
- `reap_stale` is called once at server startup: any record with `status == "running"`
  whose `pid` is not the current process is rewritten as `failed` with the error
  `"the EasyMigrate server stopped while this <stage> was running; its result is unknown"`.
  **A dead job must never be shown as live** — that is the specific dishonesty this
  function exists to prevent.

### `routes/stages.py`
All three are `POST`, all take the token dependency, all return the `JobRecord` and `202`.
All return `409` when a job is active.

| Route | Body | Pre-satisfied callbacks |
|---|---|---|
| `/api/scan` | `region`, and one of `profile` / `role_arn` | none needed |
| `/api/plan` | `gcp_project`, optional `gcp_region`, `db_passwords: dict[str,str]`, `create_bucket: bool` | `confirm_bucket=lambda _: create_bucket`, `ask_password=None`, `db_passwords=…` |
| `/api/apply` | `plan_fingerprint: str` | `confirm=lambda _: True`, `stream=<job stream>` |

- `ask_password=None` is deliberate: a missing password then produces the engine's clear
  error listing the instances and both non-interactive ways to supply one, instead of the
  server hanging on a prompt nobody can answer.
- `create_bucket=False` with a missing bucket makes the engine decline, which surfaces as a
  normal `400`. The UI asks the user *before* starting the job, per spec §3.
- **`/api/apply` requires `plan_fingerprint`**, the hash of `plan.lock.json` the client
  last read. A mismatch is `409` with a message saying the plan changed since it was
  reviewed. `plan.lock.json` already stops the *engine* applying a stale plan; this stops a
  stale *browser tab* approving a plan the user never saw.

### `routes/jobs.py`
- `GET /api/jobs/{id}` → the record, or 404.
- `GET /api/jobs/{id}/stream` → Server-Sent Events. It **replays the log from byte 0**,
  then tails it, then emits a terminal event carrying the final status and closes. Replay
  from the start is deliberate: a user opening the page late still sees the whole story.
  Poll the file with a short sleep rather than adding a filesystem-watch dependency; the
  log is append-only and the volume is small.
- The stream ends when the record reaches a terminal status **and** the log has been fully
  read, so the last lines are never lost to a race.

## Required test cases (minimum)

**`test_jobs.py`:**
- `start` writes the record before the work begins (assert the record exists from inside
  the work callable)
- a successful job → `succeeded`, `finished_at` set, log contains the streamed lines
- a job whose work raises → `failed`, `error` carries the exception message verbatim
- a second `start` while one is active raises `JobAlreadyRunning`
- `active()` returns None once the job finishes
- **`reap_stale`**: a record written with a different pid and `status="running"` becomes
  `failed` with the "server stopped" message; a `succeeded` record is left alone; the
  current process's own running job is left alone
- records survive a fresh `JobStore` instance — the disk is the truth (the anti-memory test)

**`test_stage_routes.py`** (GCP and OpenTofu seams faked exactly as
`tests/core/test_pipeline_plan_apply.py` does):
- each route returns 202 and a job id, and the job reaches `succeeded`
- a second start while one runs → 409
- `/api/plan` passes the supplied passwords through to the pipeline (assert on a fake
  pipeline capturing kwargs) and passes `ask_password=None`
- `/api/plan` with `create_bucket=false` and a missing bucket → the engine's error at 400
- `/api/apply` with a stale `plan_fingerprint` → 409 naming the mismatch
- `/api/apply` with the correct fingerprint → 202
- **no password appears in the job record, the log file, or any response** — assert by
  scanning all three for the sentinel

**`test_job_stream.py`:**
- a client attaching **after** the job finished still receives the whole log, then the
  terminal event
- a client attaching mid-job receives earlier lines and then later ones
- the terminal event carries the final status, and the stream closes
- lines written just before completion are not lost

## Tasks
1. `Project.jobs_dir`, `jobs.py` (record, store, start, atomic writes) and `test_jobs.py`
   except reaping.
2. `reap_stale` plus its tests, wired into the app factory's startup.
3. `routes/stages.py` for all three stages, with `test_stage_routes.py`, including the
   fingerprint check and the password-leak guard.
4. `routes/jobs.py` with the SSE stream and `test_job_stream.py`; add the `sse-starlette`
   dependency to the `ui` extra; correct the spec's §4 gitignore sentence.
