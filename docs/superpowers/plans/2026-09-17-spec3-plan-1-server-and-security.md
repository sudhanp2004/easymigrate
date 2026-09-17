# Spec 3, Plan 1 of 4: The UI Server and Its Security

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `easymigrate ui` starts a loopback-only FastAPI server that opens the browser,
serves the SPA shell, reports project state, and cannot be driven by any other website.

**Spec:** `docs/superpowers/specs/2026-09-17-local-ui-design.md` §2, §5 (`/api/project`), §6,
§7 (binding). Product truth: `PRODUCT.md`.

## Global Constraints
- **The filesystem is the state.** The server holds no pipeline state in memory; every
  request reads `.easymigrate/` afresh, so the CLI and UI stay interchangeable on one
  project. A cache that breaks that property is a defect, not an optimisation.
- `core.pipeline` keeps its current signatures. If the UI seems to need a change there,
  raise it as a finding rather than editing the engine.
- `easymigrate.ui` may import `easymigrate.core`; it must never import boto3, `google.*`,
  or plugin internals directly. `tests/test_dependency_rules.py` enforces the existing
  layering — extend it to cover `ui`.
- FastAPI, uvicorn and sse-starlette go in an optional `ui` extra. A CLI-only install must
  keep working with none of them present, so `easymigrate.cli` must not import
  `easymigrate.ui` at module scope.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/ui/
├── __init__.py          # create_app(project, token) -> FastAPI
├── app.py               # application factory, static mounting, error handlers
├── security.py          # token generation, dependency, origin/host check
├── state.py             # project_state(project) -> ProjectState
└── static/.gitkeep      # built SPA lands here (plan 4)
src/easymigrate/cli.py   # + `ui` command, imported lazily
pyproject.toml           # + [project.optional-dependencies] ui
tests/ui/{__init__.py,conftest.py,test_security.py,test_state.py,test_app.py}
tests/test_dependency_rules.py   # + ui layering rule
```

## Interfaces

### `security.py`
```python
def new_token() -> str                      # secrets.token_urlsafe(32)

class Security:
    def __init__(self, token: str, host: str, port: int) -> None
    def check_origin(self, origin: str | None, host_header: str | None) -> None
    def check_token(self, supplied: str | None) -> None
```
- `check_origin` accepts only `http://127.0.0.1:<port>` and `http://localhost:<port>`, and
  a **missing** `Origin` (same-origin non-CORS requests and `curl` send none). It rejects
  any other value, and rejects a `Host` header that is not the expected loopback authority
  — that is what closes DNS rebinding, where the browser sends an attacker's hostname.
- `check_token` compares with `secrets.compare_digest`.
- Both raise `HTTPException(403)` with a short, non-leaking message.
- The token travels in the **`X-EasyMigrate-Token`** header — that exact name, since the
  client in plan 4 sends it and a mismatch is a silent 403 nobody can debug.
- Applied as FastAPI dependencies: origin on **every** route, token on every mutating
  route (`POST`, `PUT`, `DELETE`). `GET` routes expose account ids and ARNs, which is why
  the origin check is not limited to mutations.

### `state.py`
```python
class StageState(StrictModel):
    name: str                    # "scan" | "analyze" | "resolve" | "plan" | "apply"
    complete: bool
    artifact_at: datetime | None # mtime of the artifact that proves it ran

class ProjectState(StrictModel):
    root: str
    stages: list[StageState]     # in pipeline order
    source: dict | None          # cloud, account_id, region, scanned_at
    target: dict | None          # cloud, project, region
    blocked: list[str]           # readiness problems preventing `plan`
```
`project_state(project: Project) -> ProjectState` derives everything from files on disk:
`scan` from the snapshot's `_scan.json`, `analyze` from `ir.json`, `resolve` from
`decisions.yaml`, `plan` from `tfplan`, `apply` from the presence of a successful apply
record. `blocked` comes from the existing `decisions.readiness_problems(...)`, so the UI
never invents its own notion of readiness. A project with no `.easymigrate/` yet returns
every stage incomplete rather than raising.

### `app.py`
```python
def create_app(project: Project, security: Security) -> FastAPI
```
- `GET /api/project` → `ProjectState`.
- `GET /api/health` → `{"ok": true}`; the launcher polls it before opening the browser.
- Mounts `static/` at `/` when it contains an `index.html`, serving `index.html` for
  unknown non-`/api` paths so client-side routing works. When the directory is empty
  (before plan 4), `/` returns a plain page saying the UI assets were not built, naming
  the build command — never a bare 404.
- Exception handlers map `EasyMigrateError`, `StageInputMissing` and `PlanStale` to `400`
  with **the engine's own message verbatim**, and `ValidationError` to `422`. The engine's
  errors already name the resource and the next action; paraphrasing them into something
  vaguer is a defect.

### `cli.py` — the `ui` command
```
easymigrate ui [--port N] [--no-browser]
```
- Imports `easymigrate.ui` **inside the function**, so a CLI-only install without the extra
  still runs every other command. A missing dependency produces a one-line error naming the
  install command (`uv tool install "easymigrate[ui]"`), exit 1.
- Binds `127.0.0.1` only. There is deliberately **no flag to change the host**: exposing
  this server on a network would let anyone on it apply infrastructure.
- Port 0 (the default) asks the OS for a free port; `--port` is honoured exactly and its
  being in use is a clear error, not a silent fallback.
- Prints the launch URL including the token, and opens it with `webbrowser.open` unless
  `--no-browser`.

## Required test cases (minimum)

**Security (`test_security.py`) — these are the point of this plan:**
- a mutating request with no token → 403; with a wrong token → 403; with the right token → passes
- a `GET` with a foreign `Origin` (`https://evil.example`) → 403
- a request with a foreign `Host` header → 403 (the DNS-rebinding case)
- a request with **no** `Origin` and the correct `Host` → allowed
- `check_token` uses a constant-time comparison (assert on `secrets.compare_digest` being
  used, or that timing-sensitive code is not a plain `==`)
- token comparison rejects an empty string and `None`
- **no response body in any test ever contains a token supplied as a password** — a guard
  test that scans responses for the sentinel value

**State (`test_state.py`):**
- a directory with no `.easymigrate/` → all stages incomplete, no exception
- after a fake scan artifact exists → `scan` complete with its mtime, others incomplete
- `blocked` reports an unacknowledged manual decision, using the real
  `readiness_problems`
- `source`/`target` populated from `project.yaml` when present
- state is re-read per call: writing a new artifact between two calls changes the result
  (this is the anti-cache test)

**App (`test_app.py`), via `TestClient`:**
- `GET /api/health` → 200
- `GET /api/project` → the state shape above
- an endpoint raising `StageInputMissing` → 400 carrying the engine's exact message
- unknown non-`/api` path with no built assets → the "assets not built" page naming the
  build command, not a 404
- `easymigrate.cli` does not import `easymigrate.ui` at module scope (import-graph assert)

**Layering (`test_dependency_rules.py`):** `easymigrate.ui` imports no boto3, no `google.*`,
no plugin internals.

## Tasks
1. `security.py` with its full test file. Do this first and completely: every later plan
   adds routes behind these dependencies, and a gap here is a hole in all of them.
2. `state.py` and its tests.
3. `app.py`: factory, `/api/health`, `/api/project`, error handlers, static mounting with
   the not-built fallback.
4. The `ui` CLI command, the `ui` optional extra in `pyproject.toml`, the layering rule,
   and a README section describing `easymigrate ui` and that it binds loopback only.
