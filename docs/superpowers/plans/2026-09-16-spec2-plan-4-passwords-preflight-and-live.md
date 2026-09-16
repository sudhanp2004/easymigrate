# Spec 2, Plan 4 of 4: Passwords, API Preflight, Report and the Live Run

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `easymigrate plan` collects a password per database, checks the target project's APIs, and
writes the tfvars OpenTofu needs; the report explains what was decided; and a real AWS→GCP database
migration is proven against the testbed.

**Spec:** `docs/superpowers/specs/2026-09-16-databases-rds-cloudsql-design.md` §2.2, §6.4, §7, §8, §9
(binding).

## Global Constraints
- EasyMigrate **never invents a database credential** (spec §2.2 supersedes decision D9 for this
  family). There is no code path that generates one, and no password ever reaches `ir.json`,
  `resolved_ir.json`, `differences.json`, `decisions.yaml`, `report.md` or `report.json`.
- `core` never imports `google` or `boto3` directly; the API check goes through the existing
  `registry.gcp_target()` seam so tests fake it by monkeypatching.
- Exit codes stay 0/1/2 as in spec 1 §6.2.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/core/pipeline.py          # plan(): passwords, preflight, secrets file
src/easymigrate/core/project.py           # secrets_path, write_secrets()
src/easymigrate/core/report.py            # the database section
src/easymigrate/cli.py                    # --db-password, env vars, prompt
src/easymigrate/providers/gcp/services.py # enabled_services() via the Service Usage API
src/easymigrate/core/registry.py          # GcpTarget gains services_enabled()
.gitignore                                # secrets.auto.tfvars.json
testbed/aws/main.tf                       # + private MySQL instance
tests/{test_cli_plan_apply.py,core/test_pipeline_plan_apply.py,core/test_report.py,providers/gcp/test_services.py}
tests/live/README.md                      # the spec-2 live procedure
```

## Interfaces

### Passwords

`pipeline.plan(...)` gains `db_passwords: dict[str, str] | None = None` and
`ask_password: Callable[[str], str] | None = None`.

Resolution order per primary, non-excluded `database_instance` in the resolved IR:
1. `db_passwords[ir_id]`
2. `os.environ["EASYMIGRATE_DB_PASSWORD_<SLUG>"]`, where `<SLUG>` is the id after
   `database_instance:`, uppercased with every non-alphanumeric character replaced by `_`
3. `ask_password(ir_id)` when the CLI supplied one (it only does so when stdin is a TTY)

With none of the three, raise `EasyMigrateError` listing every instance still missing a password and
both non-interactive ways to supply one, including the exact env var name for each.

Validation before anything is generated — a failure names the instance and the rule broken:
- 1–100 characters
- no leading or trailing whitespace, no NUL, no newline

Replicas are skipped: a Cloud SQL replica inherits its primary's users.

`Project.write_secrets(values: dict[str, str]) -> Path` writes
`.easymigrate/terraform/secrets.auto.tfvars.json` as `{"db_password_<label>": "<value>"}` — the same
`<label>` the generator used for its `variable` blocks, so plan 3's labels and this file must agree;
take the mapping from the generator rather than recomputing it (`generate` returns it via
`ctx.info`-free means: expose `database_password_variables(resolved) -> dict[str, str]` from
`plugins/database/generate_gcp.py`, mapping IR id → variable name, and have the generator use the
same function). The file is written with mode 0600, atomically, and added to `.gitignore`.

`plan.lock.json` includes the secrets file's fingerprint, so editing a password after `plan` makes
`apply` correctly refuse as stale.

### `plan` preflight (spec §6.4)

`providers/gcp/services.py`:
```python
def enabled_services(gcp_project: str) -> set[str]   # via serviceusage.services.list, state ENABLED
```
`GcpTarget` gains `services_enabled(gcp_project: str, required: set[str]) -> set[str]` returning the
missing ones, so `core` stays free of `google` imports and tests fake it.

`plan` computes the required set from the resolved IR: `sqladmin.googleapis.com` when any Cloud SQL
instance will be generated, plus `servicenetworking.googleapis.com` when any PSA block will be. If
any are missing, raise `EasyMigrateError` quoting
`gcloud services enable <missing> --project <gcp_project>`. If the check itself fails (no
permission to read Service Usage), print a warning and continue — a missing permission to *check*
must not block a user whose APIs are already on.

EasyMigrate generates no `google_project_service` resources: `disable_on_destroy` defaults to true,
so a teardown would disable the API for everything else in the project.

### Report (spec §7)

`report.py` gains a **Databases** section, per instance, in plain sentences:
- the chosen tier and what it was derived from (AWS class, vCPUs, memory)
- connectivity: `private IP over the <range> Private Services Access range on <network>`, or
  `public IP, reachable from <n> authorized networks` with the CIDRs listed, and a loud line when one
  is `0.0.0.0/0`
- backups and PITR after clamping, saying so when a value was clamped
- every dropped parameter with its value
- `The database password you supplied is stored in the Terraform state in your GCS bucket and in
  .easymigrate/terraform/secrets.auto.tfvars.json. Rotate it after cutover if that is not acceptable.`
- the `DB-USERS` reminder that only the master user was migrated

`report.json` carries the same facts as data. **Neither file ever contains a password.**

### CLI

`easymigrate plan` gains `--db-password ID=VALUE`, repeatable. A value without `=`, or an id that is
not a `database_instance` in the resolved IR, is exit 1 with a message naming the valid ids. The
prompt uses `typer.prompt(..., hide_input=True)` and is passed as `ask_password` only when
`sys.stdin.isatty()`.

## Testbed and live run

`testbed/aws/main.tf` gains, alongside the existing public Postgres:
- an `aws_db_instance` `mysql`, `db.t3.micro`, 20 GiB, `publicly_accessible = false`, `multi_az =
  false` (cost), in the existing DB subnet group, with a security group allowing 3306 from the VPC
  CIDR, `backup_retention_period = 1`, and a parameter group setting one supported flag
  (`max_connections`) and one unsupported one, so `DB-PARAM-FLAGS` is exercised for real.

`tests/live/README.md` gains the spec-2 procedure: apply the testbed, run
`scan → analyze → resolve --accept-defaults → plan --gcp-project easymigrate-test-30319 --db-password
… → apply`, then verify by connecting to both Cloud SQL instances (the public one over its
authorized network, the private one from a small Compute Engine VM in the migrated VPC), then destroy
everything on both clouds. Cloud SQL and RDS both bill per hour: this runs once, deliberately, and the
teardown step is part of the procedure, not an afterthought.

## Required test cases (minimum)

**Passwords:**
- all three sources resolve, in the documented precedence
- a missing password → error naming every instance and its env var
- each validation rule rejects, naming the instance
- replicas need no password
- the secrets file is written with mode 0600, contains exactly the generator's variable names, and is
  gitignored
- no password appears in `ir.json`, `resolved_ir.json`, `report.md` or `report.json` — assert by
  scanning the written files for the test password string
- changing the secrets file after `plan` makes `apply` refuse as stale

**Preflight:**
- a missing `sqladmin` → error quoting the exact `gcloud` command
- PSA in the plan and `servicenetworking` missing → both named
- no database instances → neither API required, and the check is skipped entirely
- the check raising → warning, and `plan` continues
- `enabled_services` parses a paginated Service Usage response and filters to `ENABLED`

**Report:**
- the database section renders tier derivation, private and public connectivity, a clamped backup
  value, dropped parameters, and the password sentence
- `0.0.0.0/0` in authorized networks renders the loud line
- `report.json` has the same facts and no password

**CLI:**
- `--db-password` with a bad id → exit 1 listing valid ids; without `=` → exit 1
- non-TTY with no password and no env var → exit 1, and the message names both non-interactive ways
- TTY path drives the prompt through `CliRunner(input=...)`

## Tasks
1. `Project.write_secrets`, the password resolution and validation in `pipeline.plan`,
   `database_password_variables`, the `.gitignore` entry, the lock-file fingerprint, and their tests.
2. `providers/gcp/services.py`, the `GcpTarget.services_enabled` seam, the preflight in `plan`, and
   its tests.
3. The report's database section in both formats, and its tests.
4. The CLI flag, env var and prompt with their tests; the testbed MySQL instance; the
   `tests/live/README.md` procedure; and a README usage line showing `plan --db-password`.
5. The live run itself, on the user's say-so, followed by teardown on both clouds and a commit
   recording what was observed.
