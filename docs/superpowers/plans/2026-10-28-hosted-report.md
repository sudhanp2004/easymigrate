# Hosted EasyMigrate (phase 1: free report) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public site (easymigrate.sudhan.tech + api.easymigrate.sudhan.tech) where a visitor connects AWS read-only, scans, reviews differences, chooses decisions and downloads the GCP Terraform + report.

**Architecture:** New `easymigrate.web` FastAPI app (Render) reusing the pipeline and the local UI's review/job routes, made project-per-request. Each visitor is an anonymous workspace (cookie) whose `.easymigrate/` directory is a local cache of an encrypted archive in Postgres (Neon). The React frontend gains a hosted build (Vercel) with landing, guide, privacy and a 5-step wizard.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, cryptography (Fernet), boto3 via the existing source seam; React 19 + Vite + react-router 6; CloudFormation JSON template.

**Spec:** `docs/superpowers/specs/2026-10-28-hosted-report-design.md`

## Global Constraints

- Frontend origin `https://easymigrate.sudhan.tech`; API `https://api.easymigrate.sudhan.tech`.
- The web app has **no** plan/apply/destroy route and never reads GCP credentials.
- Stored per workspace: id, sha256 of the secret, external ID, region, role ARN, account ID, timestamps, encrypted archive. Never AWS credentials.
- Cookie `em_ws`: `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=604800` (7 days), host-only.
- Retention: delete a workspace 7 days after `last_active_at`.
- Mutating requests need header `X-EasyMigrate: 1`.
- Limits: 10 new workspaces / IP / hour; 10 scans / workspace / day; 2 concurrent scans per instance (a slot older than 20 min counts as free); request body ≤ 64 KiB; archive ≤ 50 MiB compressed.
- Role: `EasyMigrateReadOnly-<ws8>` (ws8 = first 8 chars of workspace id), stack of the same name, trust principal `arn:aws:iam::538342414628:user/easymigrate-web` with `sts:ExternalId` condition, permissions = exactly `docs/aws-scan-policy.json`, `MaxSessionDuration` 3600.
- Role ARN regex `^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,128}$`; GCP project regex `^[a-z][a-z0-9-]{4,28}[a-z0-9]$` (empty allowed → `my-gcp-project`).
- `easymigrate.web` follows the same dependency rule as `easymigrate.ui` (no cloud SDKs, no plugins/providers/engine imports); AWS goes through `core.registry`.
- Local CLI and `easymigrate ui` behaviour unchanged; all existing tests keep passing.
- Env vars: `DATABASE_URL`, `EASYMIGRATE_DATA_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `EASYMIGRATE_WEB_ORIGIN`, `EASYMIGRATE_CFN_TEMPLATE_URL`, `EASYMIGRATE_WEB_DEV`, `EASYMIGRATE_DATA_DIR`.

## Review Focus

1. A workspace restored on a fresh instance (Render restart/sleep) with a job left `running` → that job shows `failed` with "the server restarted…", not running forever (pid reuse on containers makes `reap_stale` unreliable). Test in Task 3.
2. Visitor changes decisions after downloading → the next download reflects them (download regenerates every time). Test in Task 4.
3. Pasted role ARN with surrounding whitespace or from a different stack (external ID mismatch) → trimmed; mismatch gives a plain "created from another session" message, not a boto stack trace. Test in Task 4.
4. AWS region with no GCP mapping → download still works when the visitor supplies a GCP region, and says so when they don't. Test in Task 4.
5. Cookie missing/expired on any workspace route → 401 with a "start again" message; the frontend sends the visitor to the start screen. Tests in Task 4 and Task 6.

---

### Task 1: Core: external ID and `pipeline.generate()`

**Files:**
- Modify: `src/easymigrate/providers/aws/source.py` (`connect`), `src/easymigrate/core/registry.py` (`AwsSource.connect`), `src/easymigrate/core/pipeline.py` (`scan`, `plan`, new `generate`)
- Test: `tests/providers/aws/test_source_external_id.py`, `tests/core/test_pipeline_generate.py`

**Interfaces:**
- Produces: `aws_source.connect(*, region, profile=None, role_arn=None, external_id: str | None = None, session_name: str = "easymigrate-scan")`; `AwsSource.connect(..., external_id=None, session_name="easymigrate-scan")`; `pipeline.scan(..., external_id: str | None = None, session_name: str | None = None)` passes them to `source.connect` **only when not None** (existing fakes keep working).
- Produces: `pipeline.generate(project, *, gcp_project: str, gcp_region: str | None = None) -> GenerateSummary` with `@dataclass GenerateSummary(gcp_project: str, gcp_region: str, files: list[str], password_variables: list[str], findings: list[Finding])`. Raises `NotReady` / `EasyMigrateError` exactly as `plan()` does for missing scan/decisions/unmapped region. Writes resolved IR, superseded, `terraform/*.tf` + `terraform.tfvars` (old `*.tf` removed first), and the report. No credential check, no API preflight, no bucket, no OpenTofu, no secrets file, no config change.
- `plan()` calls the same two private helpers (`_resolve_for_generation`, `_write_terraform`) so both write identical files.

- [ ] **Step 1: Tests**
  - `test_connect_passes_external_id_and_session_name` (moto `sts`: patch `assume_role` via `botocore.stub` or monkeypatch session client; assert `ExternalId="abc"`, `RoleSessionName="easymigrate-ws12345678"`).
  - `test_connect_without_external_id_omits_it` (no `ExternalId` key in the call).
  - `test_scan_forwards_external_id_only_when_given` (fake source recording kwargs; with/without).
  - `test_generate_writes_terraform_without_gcp` (reuse `_project_ready_to_plan` helper from `tests/core/test_pipeline_plan_apply.py`; `registry.gcp_target` monkeypatched to raise if called; assert `providers.tf`, `backend.tf`, `terraform.tfvars` exist, tfvars contains `project = "p-123456"`, no `secrets.auto.tfvars.json`, `resolved_ir.json` written, report written).
  - `test_generate_matches_plan_output` (same project: run `plan` with the existing fakes into dir A, `generate` into dir B; every `*.tf` byte-identical).
  - `test_generate_not_ready_raises` (no decisions → `NotReady`).
  - `test_generate_lists_password_variables` (project with an RDS primary → `password_variables == [<var name>]`).
- [ ] **Step 2:** Run, see failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q` → all pass (incl. existing plan golden tests).
- [ ] **Step 5:** Commit `feat(core): pipeline.generate and external-ID role assumption`.

### Task 2: Shared review and job routes take the project per request

**Files:**
- Modify: `src/easymigrate/ui/routes/review.py`, `src/easymigrate/ui/routes/jobs.py`, `src/easymigrate/ui/jobs.py`, `src/easymigrate/ui/app.py`
- Create: `src/easymigrate/ui/routes/deps.py`
- Test: existing `tests/ui/*` unchanged and passing; add `tests/ui/test_route_deps.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) RouteDeps(project: Callable[..., Project], jobs: Callable[..., JobStore], reads: list, mutating: list)` in `ui/routes/deps.py`. `project`/`jobs` are FastAPI dependency callables; handlers take `project: Project = Depends(deps.project)`.
- `review.register(app, deps: RouteDeps)` and `jobs.register(app, deps: RouteDeps)`; `stages.register` keeps its signature (local only). `create_app` builds a `RouteDeps` returning its fixed project/JobStore.
- `JobStore.start(stage, work, on_finish: Callable[[JobRecord], None] | None = None)`: `on_finish(final_record)` is called after the terminal record is written (exceptions from it are swallowed and do not change the record).

- [ ] **Step 1: Tests** — `test_review_routes_use_per_request_project` (two projects, a dependency choosing by a header; `GET /api/differences` returns each project's own list); `test_on_finish_called_with_terminal_record` (succeeded and failed cases).
- [ ] **Step 2–4:** Fail, implement, full suite passes.
- [ ] **Step 5:** Commit `refactor(ui): review and job routes resolve the project per request`.

### Task 3: Web storage and workspaces

**Files:**
- Create: `src/easymigrate/web/__init__.py`, `web/settings.py`, `web/store.py`, `web/workspaces.py`
- Modify: `pyproject.toml` (extra `web = ["fastapi>=0.111","uvicorn[standard]>=0.30","sse-starlette>=2.1","psycopg[binary]>=3.2","cryptography>=43"]`), `tests/test_dependency_rules.py` (web rule = ui rule)
- Test: `tests/web/test_store.py`, `tests/web/test_workspaces.py`

**Interfaces:**
- `WebSettings` (frozen dataclass) `.from_env()`: `database_url: str | None`, `data_key: str` (required unless dev; dev generates one), `origin: str = "https://easymigrate.sudhan.tech"`, `cfn_template_url: str`, `dev: bool`, `data_dir: Path` (default `tempfile.gettempdir()/easymigrate-web`), `max_concurrent_scans: int = 2`.
- `WorkspaceRecord` (dataclass): `id, secret_hash, external_id, region: str|None, role_arn: str|None, account_id: str|None, created_at, last_active_at`; property `ws8`.
- `Store` protocol: `create(rec)`, `by_secret_hash(h) -> WorkspaceRecord|None`, `update(rec)`, `delete(id)`, `save_archive(id, data: bytes)`, `load_archive(id) -> bytes|None`, `inactive_since(cutoff) -> list[str]`, `hit(key: str, window_s: int, limit: int) -> bool` (True = allowed; records the hit). Implementations `MemoryStore` and `PostgresStore(url)` (creates tables `workspaces`, `archives` (FK cascade), `rate_hits(key, at)` with `IF NOT EXISTS`; `hit` also deletes that key's rows older than the window).
- `WorkspaceManager(store, settings)`: `create() -> tuple[WorkspaceRecord, str secret]`; `authenticate(secret|None) -> WorkspaceRecord|None` (touches `last_active_at` when older than 5 min); `project_for(rec) -> Project` (restore from archive when the local dir is missing, then mark every `running` job record failed with `"the server restarted while this {stage} was running; run it again"`; init a fresh project otherwise); `commit(rec)` (tar.gz of the project dir → Fernet → `save_archive`; per-workspace lock; raises `EasyMigrateError` over 50 MiB); `delete(rec)`; `cleanup(now) -> int` (deletes inactive >7 days, local dirs too).

- [ ] **Step 1: Tests** — store contract run against `MemoryStore` always and `PostgresStore` when `EASYMIGRATE_TEST_DATABASE_URL` is set (create/lookup/update/delete cascade/archive/inactive/hit limit+window); `test_secret_is_never_stored_plain`; `test_commit_restore_round_trip` (write files, commit, rm local dir, `project_for` restores identical content); `test_archive_is_encrypted` (stored bytes don't contain a known filename and fail to decrypt with another key); `test_restore_marks_running_jobs_failed` (Review Focus 1); `test_cleanup_deletes_after_7_days_only`; `test_authenticate_unknown_secret_is_none`; dependency-rules test covers `easymigrate.web`.
- [ ] **Step 2–4:** Fail, implement, pass.
- [ ] **Step 5:** Commit `feat(web): workspaces with encrypted archives in Postgres`.

### Task 4: Web API

**Files:**
- Create: `src/easymigrate/web/app.py`, `web/routes.py`, `web/download.py`, `web/README_download.md` (package data)
- Test: `tests/web/test_app.py`, `tests/web/test_routes.py`, `tests/web/test_download.py`

**Interfaces:**
- `create_web_app(settings: WebSettings | None = None, store: Store | None = None, source: Any = None) -> FastAPI` (factory for `uvicorn --factory`; `store` defaults to `PostgresStore` when `database_url` else `MemoryStore` with a logged warning; `source` defaults to `registry.source_for("aws")`). Docs routes off. Lifespan: `cleanup` at start and hourly.
- Middleware: CORS (`origin`, plus `http://localhost:5173` in dev; credentials; headers `Content-Type`, `X-EasyMigrate`), mutating-method check for `X-EasyMigrate: 1` (403 `"missing X-EasyMigrate header"`), body-size 413, security headers (`Strict-Transport-Security: max-age=31536000; includeSubDomains`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`).
- Workspace dependency: cookie → `WorkspaceManager.authenticate` → 401 `"Your session has ended. Data is deleted after 7 days without activity. Start again."`. Project dependency yields `project_for(rec)` and calls `commit` after mutating methods.
- Routes:
  - `GET /api/health` → `{"ok": true}`.
  - `POST /api/workspaces` → 201 `{ "ws8": ... }` + cookie; 429 after 10/IP/hour (`request.client.host`).
  - `DELETE /api/workspace` → 204, deletes, clears cookie.
  - `GET /api/project` → `WebState(ProjectState)` adding `aws: {region, role_arn, account_id} | null`, `suggested_gcp_region: str | null`, `expires_at: datetime`; stages `scan`, `analyze`, `resolve` only; `root` = `"workspace"`; re-sets the cookie (rolling).
  - `GET /api/aws/setup?region=` → `{external_id, stack_name, role_name, template_url, regions: [sorted REGION_MAP keys], quick_create_url}` where `quick_create_url = https://console.aws.amazon.com/cloudformation/home?region=<r>#/stacks/quickcreate?templateURL=<urlencoded>&stackName=<stack>&param_ExternalId=<ext>&param_RoleSuffix=<ws8>`; 400 on unknown region.
  - `POST /api/aws/connect {region, role_arn}` → trims, validates regex, `source.connect(region=, role_arn=, external_id=, session_name="easymigrate-<ws8>")`, stores region/role/account → `{account_id}`. Errors: regex → 400 `"That doesn't look like a role ARN…"`; `SourceConnectionError` containing `AssumeRole` → 400 `"AWS refused access to that role. Check the stack shows CREATE_COMPLETE and that it was created from this browser's link — a stack made in another session has a different external ID."`; others → 400 with the source message.
  - `POST /api/scan` (no body) → 202 `JobRecord`; 400 `"Connect AWS first."`; 429 per-workspace daily limit; 503 `"EasyMigrate is busy with other scans. Try again in a minute."` when slots full. Work = `pipeline.scan(project, region, role_arn=, external_id=, session_name=, source=)`; `on_finish` releases the slot and commits.
  - Shared `review.register` + `jobs.register` with web `RouteDeps`.
  - `POST /api/download {gcp_project: str = "", gcp_region: str | None}` → validates project regex, runs `pipeline.generate`, returns `application/zip` with `Content-Disposition: attachment; filename="easymigrate-<account>-<region>.zip"`. Unmapped region and no `gcp_region` → 400 telling them to pick a GCP region.
- `build_zip(project, summary: GenerateSummary) -> bytes` in `web/download.py`: `terraform/*.tf` except `backend.tf`, `terraform/terraform.tfvars`, `report.md`, `report.json`, `README.md` rendered from `README_download.md` (placeholders `{gcp_project}`, `{gcp_region}`, `{password_vars}` → `export TF_VAR_<name>=...` lines or "none").

- [ ] **Step 1: Tests** (TestClient, `MemoryStore`, fake source) — `test_no_plan_apply_destroy_routes`; `test_workspace_routes_need_cookie_401` (Review Focus 5); `test_mutating_without_header_403`; `test_cors_only_allows_origin`; `test_security_headers_present`; `test_workspace_creation_rate_limited`; `test_setup_builds_quick_create_url` (exact URL); `test_connect_trims_and_stores_account` / `..._rejects_bad_arn` / `..._maps_assume_role_denial` (Review Focus 3); `test_scan_requires_connection`; `test_scan_passes_external_id_and_commits`; `test_scan_busy_when_slots_full` and `test_stale_slot_freed_after_20_min`; `test_scan_daily_limit`; `test_project_state_shape`; `test_download_zip_contents` (no `backend.tf`, README has the password export); `test_download_reflects_changed_decisions` (Review Focus 2); `test_download_unmapped_region_needs_gcp_region` (Review Focus 4); `test_download_rejects_bad_gcp_project`; `test_delete_workspace_clears_cookie`.
- [ ] **Step 2–4:** Fail, implement, pass; `uv run ruff check . && uv run ruff format --check .`.
- [ ] **Step 5:** Commit `feat(web): hosted API for the free migration report`.

### Task 5: Deployment files

**Files:**
- Create: `deploy/aws/easymigrate-readonly-role.json`, `deploy/aws/publish-template.sh`, `deploy/README.md`, `render.yaml`, `frontend/vercel.json`
- Test: `tests/web/test_cfn_template.py`

**Interfaces:**
- Template params `ExternalId` (String, MinLength 16), `RoleSuffix` (String, `AllowedPattern ^[a-f0-9]{8}$`); resource `AWS::IAM::Role` named `!Sub EasyMigrateReadOnly-${RoleSuffix}` (JSON `Fn::Sub`), inline policy; output `RoleArn`.
- `publish-template.sh <bucket>`: creates bucket in us-east-1 if missing, bucket policy public `s3:GetObject` for `deploy/*` only (keeps Block Public Access for ACLs), uploads, prints the HTTPS URL.
- `render.yaml`: web service `easymigrate-api`, runtime python, `PYTHON_VERSION=3.12.7`, build `pip install uv && uv sync --frozen --extra web --no-dev`, start `.venv/bin/uvicorn easymigrate.web.app:create_web_app --factory --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`, `healthCheckPath: /api/health`, env vars with `sync: false`, plan free.
- `vercel.json`: `buildCommand: npm run build:hosted`, `outputDirectory: dist`, SPA rewrite `/(.*)` → `/index.html`, security headers incl. a CSP allowing `connect-src https://api.easymigrate.sudhan.tech`.

- [ ] **Step 1: Tests** — `test_template_actions_equal_scan_policy` (sets equal); `test_template_trust_requires_external_id` (condition `StringEquals sts:ExternalId = Ref ExternalId`, principal exact); `test_role_name_uses_suffix`; `test_render_yaml_starts_web_factory` (yaml load; start command contains `easymigrate.web.app:create_web_app --factory`).
- [ ] **Step 2–4:** Fail, implement, pass.
- [ ] **Step 5:** Commit `chore(deploy): CloudFormation role, Render and Vercel config`.

### Task 6: Hosted frontend

**Files:**
- Create: `frontend/.env.hosted`, `frontend/src/hosted/` (`HostedApp.tsx`, `config.ts`, `hostedGating.ts`, `Wizard.tsx`, `pages/Landing.tsx`, `pages/Guide.tsx`, `pages/Privacy.tsx`, `pages/Connect.tsx`, `pages/HostedScan.tsx`, `pages/Download.tsx`, `pages/Report.tsx`, `hosted.css`), tests beside them
- Modify: `frontend/src/api/client.ts`, `frontend/src/api/useJobStream.ts`, `frontend/src/main.tsx`, `frontend/vite.config.ts`, `frontend/package.json` (`build:hosted`: `tsc -b && vite build --mode hosted`)

**Interfaces:**
- `config.ts`: `IS_HOSTED = import.meta.env.VITE_HOSTED === '1'`, `API_BASE = import.meta.env.VITE_API_BASE ?? ''`. `.env.hosted`: `VITE_HOSTED=1`, `VITE_API_BASE=https://api.easymigrate.sudhan.tech`. Vite: `mode === 'hosted'` → `outDir: 'dist'`, else the existing static dir.
- `api()` in hosted mode: `fetch(API_BASE + path, {credentials: 'include'})`, mutating adds `X-EasyMigrate: 1` (no token); 401 → `ApiError(401, <server detail>)`; new `apiBlob(path, init) -> Promise<{blob, filename}>`. `useJobStream` uses `new EventSource(API_BASE + url, { withCredentials: IS_HOSTED })`.
- `main.tsx`: hosted → `<HostedApp/>` (no token bootstrap), else unchanged.
- Hosted routes: `/` Landing, `/guide`, `/privacy`, `/app` → redirect to first incomplete step; `/app/connect`, `/app/scan`, `/app/review` (reuses `AnalyzePage`), `/app/decisions` (reuses `ResolvePage`), `/app/download`, `/app/report`.
- `hostedGating.ts`: `HOSTED_STEPS = ['connect','scan','review','decisions','download']`; complete: connect ⇐ `state.aws?.account_id`; scan/review/decisions ⇐ stages scan/analyze/resolve; download needs decisions complete and `blocked` empty.
- Wizard shell: brand, progress bar of the five steps, "Delete my data" (DELETE then go to `/`), any 401 → "session ended" notice with "Start again". "Waking the server up (up to a minute)…" when `/api/project` takes > 3 s.
- Copy rules: plain language, second person, no unexplained acronyms (first use of IAM/ARN/CloudFormation explained inline); Guide sections exactly the six in the spec; Privacy states what is stored, 7-day deletion, how to delete now, how to remove the role.

- [ ] **Step 1: Tests** (vitest) — `client` hosted: credentials include + header, no token; `hostedGating` first-incomplete and download gate; `Connect` shows quick-create link from setup and posts trimmed ARN; `Download` posts gcp fields and triggers a save; 401 renders the start-again notice (Review Focus 5); wake-up message after 3 s (fake timers).
- [ ] **Step 2–4:** Fail, implement, `npm test`, `npm run lint`, `npm run build` (local static unchanged) and `npm run build:hosted` pass.
- [ ] **Step 5:** Commit `feat(frontend): hosted site with landing, guide and report wizard`.

### Task 7: Provision, docs, deploy hand-off, live test

- [ ] Neon project `easymigrate-web` (region nearest Render's, e.g. aws-us-east-1 or Singapore matching the Render region), get `DATABASE_URL`; run the Postgres store contract tests against it.
- [ ] AWS (operator account 538342414628): IAM user `easymigrate-web` + inline policy `sts:AssumeRole` on `arn:aws:iam::*:role/EasyMigrateReadOnly-*`, access key; `deploy/aws/publish-template.sh easymigrate-public-538342414628`.
- [ ] Generate `EASYMIGRATE_DATA_KEY` (Fernet); write all env values to a gitignored local file for Sudhan, never to the repo or chat logs beyond what is needed.
- [ ] README: "Hosted version" section linking the site; `deploy/README.md` step list; CHANGELOG entry; update memory.
- [ ] Hand Sudhan: Render Blueprint import + env values; Vercel import (root `frontend`); DNS CNAMEs `easymigrate` → Vercel, `api.easymigrate` → Render custom domain target.
- [ ] Live test after deploy (spec "Testing"): real quick-create in operator account, scan testbed region (or an empty region), review, download, `tofu init -backend=false && tofu validate` on the zip, then delete the stack and workspace.
- [ ] One review of the whole branch (sonnet), fix, `graphify update .`, commit, push.
