# Hosted EasyMigrate, phase 1: the free migration report (2026-10-28)

## Intent

Sudhan wants to post one link on Reddit. A visitor opens it, follows a guide that needs no
cloud expertise, connects their AWS account and gets EasyMigrate's migration report and the
generated GCP Terraform, without installing anything.

Today EasyMigrate runs only on the user's own machine (CLI plus a local UI bound to
127.0.0.1). This phase adds a hosted, multi-visitor version at
**https://easymigrate.sudhan.tech** that does the read-only half of the pipeline:
scan → analyze → resolve → generate Terraform → download.

Phase 2 (later, separate design): connect GCP and let the site plan and apply. It is kept out
of phase 1 on purpose: a new site asking strangers for write access to their cloud is a much
harder sell than read-only AWS, and the free report is safe to post publicly.

## Goals

- One link, no install, no sign-up: a visitor can go from the landing page to a downloaded
  report in about ten minutes on a small account.
- Connecting AWS is a guided, mostly-click flow that grants **read-only** access, which the
  visitor can see and revoke themselves.
- A guide inside the site that a Reddit visitor can follow: what it does, each step, what
  access it takes, what is stored, how to remove access, what to do with the download.
- The visitor reviews the differences and picks decisions in the browser, using the same
  screens and rules as the local UI.
- The download is a zip they can use straight away: Terraform, `report.md`, and a README
  explaining how to apply it with OpenTofu/Terraform (or with the EasyMigrate CLI).

## Non-goals (phase 1)

- Anything that touches the visitor's GCP: no GCP credentials, no `tofu plan`, no apply, no
  destroy. The hosted server never holds GCP credentials.
- Accounts, sign-in, teams, billing.
- Data migration (unchanged product rule: configuration only).
- Multi-region scans in one run (one region per scan, as the CLI).

## Anti-goals (would make it a failure even if it works)

- Asking visitors to paste AWS access keys or secrets anywhere.
- Storing any AWS credential. Only the role ARN and our own external ID are kept.
- One visitor ever being able to see another visitor's scan, or to scan an account through
  another visitor's role (confused deputy).
- The hosted server being able to change anything in a visitor's AWS account.
- A guide that assumes the reader already knows IAM, CloudFormation or Terraform.

## Constraints

- Domain: `sudhan.tech`, owned by Sudhan. Frontend at `easymigrate.sudhan.tech`, API at
  `api.easymigrate.sudhan.tech` (same site, so a `SameSite=Lax` cookie works across them).
- Hosting (Sudhan's choice): frontend on **Vercel**, API on **Render**.
- Free tiers to start (my call, see Calls): Render free web service (sleeps after 15 min idle,
  ~1 min cold start, 512 MB), Neon free Postgres, Vercel Hobby. Moving to Render Starter
  (~$7/month, always on) is a dashboard change, no code change.
- The repo is public: no secrets in it. Secrets live in Render/Vercel environment variables.
- The existing CLI and local UI keep working unchanged; the hosted app reuses the pipeline,
  rules, generators and React review screens rather than forking them.
- Operator AWS account for the role trust: 538342414628 (Sudhan's).

## How it works

### Architecture

```
Browser ──> easymigrate.sudhan.tech (Vercel: static React build, landing + guide + app)
   │
   └─ fetch / EventSource, credentials: include
      ──> api.easymigrate.sudhan.tech (Render: FastAPI, `easymigrate.web`)
             ├─ sts:AssumeRole (ExternalId) ──> visitor's AWS, read-only role
             └─ Neon Postgres: workspaces, encrypted project archives, rate-limit counters
```

- **New package `easymigrate.web`** with its own app factory (`create_web_app()`), separate
  from `easymigrate.ui`. It registers only: workspace, AWS-connect, scan, analyze, resolve and
  review, generate, download, job stream, delete. There is no plan/apply/destroy route in it
  at all (not merely disabled). Entry point: `easymigrate-web` console script or
  `uvicorn easymigrate.web.app:create_web_app --factory`.
- The local UI's localhost `Security` (Host/Origin pinned to 127.0.0.1, per-run token) is not
  used. Web security is described below.

### Visitor workspace (no sign-up)

- "Start free" calls `POST /api/workspaces`. The server creates a workspace with a random id,
  a random 32-byte secret, and a random **external ID** (`secrets.token_hex(16)`). It sets
  the secret in a cookie: `HttpOnly; Secure; SameSite=Lax; Domain=api.easymigrate.sudhan.tech`
  (host-only), 7 days.
- Every API call is scoped to the workspace that cookie identifies. No workspace id in URLs.
- One workspace per browser. "Start over" deletes it and makes a new one. "Delete my data"
  deletes it immediately.
- **Retention:** a workspace and all its files are deleted 7 days after its last activity
  (a cleanup pass at startup and hourly in-process). The site says this plainly.
- A `ws_<id>` local directory under a temp root holds the familiar `.easymigrate/` project so
  the existing `Project`/pipeline code runs unchanged. After every stage the directory is
  archived (tar.gz), **encrypted** (Fernet, key from `EASYMIGRATE_DATA_KEY`) and stored in
  Postgres. Before any request that needs it, a missing directory (fresh instance after a
  restart or sleep) is restored from the archive. Postgres is the source of truth; local disk
  is a cache.

### Connecting AWS

1. The visitor picks the AWS region to scan (dropdown of the regions EasyMigrate maps to GCP).
2. **"Open AWS to create the read-only role"** opens a CloudFormation *quick-create* link in a
   new tab, with the stack name and the workspace's external ID pre-filled:
   `https://console.aws.amazon.com/cloudformation/home?region=<r>#/stacks/quickcreate?templateURL=<S3 URL>&stackName=EasyMigrateReadOnly-<ws8>&param_ExternalId=<id>&param_RoleSuffix=<ws8>`,
   where `<ws8>` is the first 8 characters of the workspace id.
   The visitor ticks "I acknowledge IAM resources" and clicks Create (about a minute).
3. The template (`deploy/aws/easymigrate-readonly-role.yaml`, also linked as "view exactly what
   this creates") makes one IAM role, `EasyMigrateReadOnly-<ws8>` (a suffix, so "Start over" or a
   second browser never collides with an earlier stack in the same account):
   - trust: the operator principal `arn:aws:iam::538342414628:user/easymigrate-web`, condition
     `sts:ExternalId` equals the `ExternalId` parameter;
   - permissions: exactly the actions in `docs/aws-scan-policy.json` (read-only), inline;
   - `MaxSessionDuration` 3600;
   - output `RoleArn`.
   The template is hosted in a public-read S3 bucket in the operator account
   (`easymigrate-public-<suffix>`, us-east-1), uploaded by a script in `deploy/`.
   CloudFormation requires an S3 URL for quick-create.
4. The visitor copies `RoleArn` from the stack's Outputs tab and pastes it. **"Test
   connection"** assumes the role with the external ID and calls `sts:GetCallerIdentity`, and
   the page shows "Connected to AWS account 1234…" or a specific, plain error (role not found,
   external ID mismatch = the stack was made for another browser session, stack still
   creating).
5. Only the role ARN and region are stored, with the workspace. Temporary credentials live in
   memory for the duration of one scan.

`providers/aws/source.connect()` gains an optional `external_id` (passed to `assume_role`)
and a session name argument; the hosted server's base session uses its environment
credentials (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` of `easymigrate-web`), never a
profile.

`easymigrate-web` IAM user (operator account): one inline policy allowing only
`sts:AssumeRole` on `arn:aws:iam::*:role/EasyMigrateReadOnly-*`. Its access keys go into Render
environment variables only.

Disconnecting: the site explains "delete the EasyMigrateReadOnly-<ws8> stack in CloudFormation" with
a direct link; deleting the workspace also makes the role useless to us (the external ID is
gone).

### Pipeline stages in the web app

Each long stage is a background job with the same live log stream the local UI uses (SSE,
`EventSource` with `withCredentials`).

- **Scan**: `pipeline.scan(project, region, role_arn, external_id)`. A scan holds one of the concurrency
  slots; a slot older than 20 min counts as free, so a hung AWS call cannot block the service
  (a running thread cannot be killed safely; botocore's own timeouts and retries bound each call). On the
  free tier the instance can sleep if the visitor closes the tab mid-scan; the job is then marked
  failed on restore and the page offers "Scan again".
- **Analyze**: `pipeline.analyze(project)`; optional scope (VPC/tag/exclude) as in the local UI.
- **Resolve**: the existing review/decision screens and routes, scoped to the workspace.
- **Generate** (new): a new `pipeline.generate(project, *, gcp_project, gcp_region)` that does
  the resolve-IR + generate-files part of `plan()` and writes the report, with no GCP
  credential check, no API preflight, no state bucket, no OpenTofu. `plan()` is refactored to
  call it, so the two can never drift. The GCP project id is optional on the web
  (placeholder `my-gcp-project` written to `terraform.tfvars` if empty, with a comment).
- **Download**: `GET /api/download` streams a zip:
  `terraform/*.tf`, `terraform/terraform.tfvars`, `report.md`, `report.json`, `README.md`.
  For the download, `backend.tf` is replaced by a version with **no backend** (local state),
  so `tofu init && tofu apply` works with nothing set up first; the README says how to move
  to a GCS backend. Database passwords are left as sensitive variables; the README shows
  `TF_VAR_...` usage. No secrets file is ever generated on the server.
- The **report** is also shown in the browser (the same `report.md` rendered).

### Web security

- CORS: only `https://easymigrate.sudhan.tech` (plus `http://localhost:5173` when
  `EASYMIGRATE_WEB_DEV=1`), `allow_credentials=True`.
- Mutating requests require the header `X-EasyMigrate: 1` (forces a CORS preflight, so a
  cross-site form post can never act), on top of the `SameSite=Lax` cookie.
- Rate limits (Postgres counters): 10 new workspaces per IP per hour; 10 scans per workspace
  per day; a global cap of 2 concurrent scans per instance (a third gets a plain "busy, try
  again in a minute"). Request bodies are size-limited.
- No scan contents, role ARNs or external IDs in logs. Errors shown to the visitor never
  include a stack trace.
- HSTS and standard security headers on both hosts (Vercel `vercel.json` headers, FastAPI
  middleware).
- Role ARN is validated (`^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$`) before any AWS call.

### Website (frontend)

The existing React app (`frontend/`) gains a **hosted build mode** (`VITE_MODE=hosted`,
`VITE_API_BASE=https://api.easymigrate.sudhan.tech`) that:

- adds public pages: **Landing** (`/`): what it does in one sentence, the three steps, "free,
  read-only, nothing to install", "Start free", links to the guide and privacy page;
  **Guide** (`/guide`): the steps below with illustrations, plus an FAQ; **Privacy** (`/privacy`):
  what is stored, for how long, how to delete it and how to revoke access;
- runs the app under `/app/...` as a step wizard with a progress bar:
  1. Connect AWS → 2. Scan → 3. Review differences → 4. Choose decisions → 5. Download;
- hides Plan/Apply and shows instead "Want EasyMigrate to build this in GCP for you? Coming
  soon / use the CLI" with a link to the GitHub README;
- replaces the localhost token with the cookie (`credentials: 'include'`) and the
  `X-EasyMigrate` header;
- shows a "waking the server up (up to a minute)" state when the first API call is slow (Render
  free tier cold start).

The local build (`easymigrate ui`) is unchanged.

**Guide content (in the site, plain language):**
1. What EasyMigrate does and does not do (reads AWS, never changes it; never copies data).
2. Connect AWS: why a role and not keys; the CloudFormation click-through with what each screen
   looks like; where to find the Role ARN; how to remove it later.
3. Scan: what it reads, how long it takes, what "scan gaps" mean.
4. Differences and decisions: what a difference is, what the safe default is, when to change it.
5. Download: what is in the zip and how to apply it (install OpenTofu, `gcloud auth
   application-default login`, `tofu init`, `tofu apply`), or use the CLI.
6. FAQ: is it safe, what is stored, cost, which services are covered, what happens to my data.

### Deployment

- `render.yaml` (Blueprint): one Python web service, build `uv sync --frozen --extra web`,
  start `uvicorn easymigrate.web.app:create_web_app --factory --host 0.0.0.0 --port $PORT`,
  health check `/api/health`, env vars listed (values set in the dashboard):
  `DATABASE_URL`, `EASYMIGRATE_DATA_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `EASYMIGRATE_WEB_ORIGIN`, `EASYMIGRATE_CFN_TEMPLATE_URL`, `EASYMIGRATE_OPERATOR_PRINCIPAL`.
- `frontend/vercel.json`: build `npm run build:hosted`, output `dist`, SPA fallback rewrite,
  security headers.
- New optional extra `web` in `pyproject.toml` (FastAPI, uvicorn, psycopg, cryptography).
- `deploy/` holds the CloudFormation template, the S3 upload script and a short
  `deploy/README.md` with the one-time setup.
- DNS (at Sudhan's DNS provider): `easymigrate` CNAME → Vercel; `api.easymigrate` CNAME →
  Render. Both platforms issue HTTPS certificates.

**One-time operator setup** (I do what my access allows; the rest is listed for Sudhan):
- Neon project + database (I can create it via the Neon connection available here).
- AWS: `easymigrate-web` IAM user + policy + access key, S3 bucket + template upload (I can do
  this with the local AWS credentials, which belong to account 538342414628).
- Render and Vercel: Sudhan connects the GitHub repo once in each dashboard (Blueprint import
  on Render, project import on Vercel with root `frontend/`) and pastes the env values I
  provide. Then adds the two DNS records.

## Testing

- Unit tests for `pipeline.generate()` (and that `plan()` output is unchanged: the existing
  golden tests keep passing), `connect(external_id=…)`, workspace lifecycle, archive
  encrypt/restore round-trip, retention cleanup, rate limits, role-ARN validation, CORS and
  the custom-header check, and that the web app has no plan/apply/destroy routes.
- Postgres in tests: a fake store behind the same interface; one optional integration test
  against a real database when `EASYMIGRATE_TEST_DATABASE_URL` is set.
- Frontend: vitest for the wizard gating, hosted API client (credentials, header), cold-start
  state.
- Live test before posting: deploy, create the role in the operator account through the real
  quick-create link, scan the testbed (scaled down), review, download, `tofu init && tofu
  validate` the zip. Then delete the stack and the workspace.

## Calls I made (Sudhan to check)

1. **No sign-up**: an anonymous workspace per browser, kept 7 days. Fewest steps for a Reddit
   visitor; sign-in can come with phase 2.
2. **API on its own subdomain** (`api.easymigrate.sudhan.tech`) rather than proxying through
   Vercel, so long-lived log streams are not cut or buffered by the proxy.
3. **Neon free Postgres** for storage (Render's free Postgres expires after 30 days; Render's
   free web service has no persistent disk). Archives are encrypted on top of Neon's
   at-rest encryption.
4. **Free tiers first**; the cost of always-on is ~$7/month when traffic justifies it. Vercel
   Hobby is meant for non-commercial use, so it needs to move to Pro if EasyMigrate starts
   charging.
5. **Download uses local Terraform state** so it applies with no setup; the README explains
   moving to GCS.
6. **CloudFormation quick-create** for the role (one click, visible template, easy removal),
   not manual IAM steps or AWS CLI commands.
7. **Licence unchanged** (proprietary). Visitors use the hosted service; they do not need a
   licence to the code. Revisit if you want outside contributors.

## Left to the builder

- Exact page layout and visual design within the existing brand (`frontend/public/brand`).
- Job runner internals (threads vs asyncio) within the stated concurrency limits.
- Table schema and migration tooling for the few web tables.
