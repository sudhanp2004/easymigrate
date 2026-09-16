# Spec 2, Plan 3 of 4: Cloud SQL Generation and Private Services Access

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `easymigrate plan` writes `terraform/database.tf` — Cloud SQL instances, databases, users
and the Private Services Access plumbing — from the resolved IR, and `tofu validate` accepts it.

**Spec:** `docs/superpowers/specs/2026-09-16-databases-rds-cloudsql-design.md` §6 (binding), §2.2 for
the password variable this plan declares but does not fill (plan 4 fills it).

## Global Constraints
- The generator contains **no difference logic**. It reads resolved IR plus `target_hints`, skips
  objects with `excluded` set, and raises `GenerationError` when it meets something a rule should
  have handled — that exception guards the contract between plans 2 and 3.
- **No silent mapping (D19):** every IR field this generator could touch is declared in
  `FIELD_COVERAGE` with `exact` / `metadata` / `rule:<ID>` / `hint:<key>` / `not_generated:<why>` /
  `derived:<why>`. The coverage test fails on any undeclared field.
- Names come from the IR id, never the display name, through `providers/gcp/naming.py`. A changed
  name emits the existing `GEN-RENAMED` finding.
- Output is deterministic: resources in IR object order, attributes in a fixed order.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/plugins/database/generate_gcp.py   # generate(resolved, ctx) -> HclFile, FIELD_COVERAGE
src/easymigrate/plugins/database/__init__.py       # delegate generate_gcp to it
src/easymigrate/providers/gcp/naming.py            # + cloudsql_name() if the 98-char rule needs it
scripts/gen_differences_doc.py                     # walk every plugin's FIELD_COVERAGE
docs/field-coverage-database.md                    # generated
tests/plugins/database/{test_generate_gcp.py,test_generate_gcp_golden.py,test_generate_gcp_tofu.py,test_field_coverage.py}
tests/plugins/database/fixtures/<case>/expected_database.tf
```

## Interfaces

### `generate(resolved: IRDocument, ctx: GenerateContext) -> HclFile`

Emits, in this order:

**1. Private Services Access — once per network** that has at least one instance with a
`target_hints["psa"]`:
```hcl
resource "google_compute_global_address" "<network-label>_psa" {
  name          = "<network-name>-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = <prefix_length>
  address       = "<range>"
  network       = google_compute_network.<network-label>.id
}

resource "google_service_networking_connection" "<network-label>_psa" {
  network                 = google_compute_network.<network-label>.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.<network-label>_psa.name]
}
```
If two instances in one network resolved to different ranges, that is a plan-2 bug, not something to
paper over: raise `GenerationError` naming both.

**2. `google_sql_database_instance` per non-excluded `database_instance`**, primaries before
replicas:
- `name` from the IR id, `database_version` from `hint:database_version`, `region` from `ctx.region`
- `deletion_protection` mirrors `deletion_protection` (the Terraform-level guard) and
  `settings.deletion_protection_enabled` the same value
- `settings`: `tier` (`hint:tier`), `availability_type` (`hint:availability_type`, default `ZONAL`),
  `disk_size` (`hint:disk_size` when `DB-STORAGE-MINIMUM` fired, else `storage.size_gib`),
  `disk_type` (`hint:disk_type`, default `PD_SSD`), `disk_autoresize` and `disk_autoresize_limit`
  (`hint:disk_autoresize*`), `database_flags` as sorted `{name, value}` blocks
  (`hint:database_flags`), `backup_configuration` and `maintenance_window` from their hints,
  `user_labels` from `labels`
- `backup_configuration` for Postgres uses `point_in_time_recovery_enabled` and for MySQL
  `binary_log_enabled`, both from `hint:pitr`, with `transaction_log_retention_days` and
  `backup_retention_settings.retained_backups`
- `ip_configuration`: `ipv4_enabled = hint:public_ip` (default false), `private_network` as a `Ref`
  to the network when `hint:private_network` is set, `authorized_networks` blocks from
  `hint:authorized_networks` (each `{name, value}`, name = a slug of the CIDR), and
  `ssl_mode = "ENCRYPTED_ONLY"` whenever the instance has a public IP
- `depends_on` the network's `google_service_networking_connection` when it has a private network
- a replica additionally sets `master_instance_name` (`hint:master_instance`) and
  `replica_configuration { failover_target = false }`, and emits **no** `backup_configuration`
- a comment header with the source ARN, the AWS instance class, and every `resolution` entry with its
  `decided_by`, exactly as the network generator writes them

**3. `google_sql_database`** for `initial_database`, when set and the instance is a primary.

**4. `google_sql_user`** for `master_username` (defaulting to the engine's own default when null),
`instance` the primary, `password = var.db_password_<label>`.

**5. One `variable` block per primary instance** in the same file:
```hcl
variable "db_password_<label>" {
  type      = string
  sensitive = true
  # no default: easymigrate plan writes secrets.auto.tfvars.json
}
```
Replicas get no user and no variable — a Cloud SQL replica inherits its primary's users.

**`database_password_variables(resolved: IRDocument) -> dict[str, str]`** — IR id → variable name,
for every primary, non-excluded instance. The generator itself uses this function to name its
`variable` blocks, and plan 4's `plan` uses it to write `secrets.auto.tfvars.json`, so the two can
never drift. It takes the same resolved IR and needs no `GenerateContext`.

**`GenerationError` cases** (each means a plan-2 rule failed to run): a non-excluded instance with no
`hint:database_version` or no `hint:tier`; a `hint:private_network` naming a network that is not in
the document; an instance with both `hint:public_ip` and `hint:private_network` unset; a replica whose
`replica_of` is an `external:` id; a `parameters` dict that is non-empty while `hint:database_flags`
and `hint:dropped_parameters` are both absent.

### Naming
Cloud SQL instance names: lowercase letters, digits and hyphens, starting with a letter, at most 98
characters. `gcp_name()` already slugs; add the 98-character truncation and the leading-letter rule in
`providers/gcp/naming.py` as `cloudsql_name()` if `gcp_name` does not already satisfy both, and reuse
the existing `GcpNameAllocator` for collisions so a truncation that collides still gets a unique name
and a `GEN-RENAMED` finding.

### `FIELD_COVERAGE`
`dict[str, dict[str, str]]` keyed by model class name (`DatabaseInstance`, `DbCompute`, `DbStorage`,
`DbNetwork`, `DbBackup`, `DbMaintenance`), one entry per field. Examples of the expected declarations:
`engine_version` → `rule:DB-ENGINE-VERSION`; `compute.class_name` → `metadata:written into the
resource comment header`; `compute.vcpus` → `rule:DB-TIER`; `network.port` → `rule:DB-PORT`;
`ca_certificate` → `not_generated:Cloud SQL manages its own server certificates`;
`zone` → `not_generated:Cloud SQL picks the zone; DB-MULTI-AZ decides regional vs zonal`;
`status` → `metadata:scan-time state, reported not generated`;
`storage.size_gib` → `exact`.

### `scripts/gen_differences_doc.py`
Generalize: walk `registry.PLUGINS`, take each plugin's generator `FIELD_COVERAGE` if it has one, and
write `docs/field-coverage.md` for `network` (unchanged name and content) and
`docs/field-coverage-<family>.md` for the rest. The existing `test_field_coverage.py` pattern is
copied for the database generator.

## Required test cases (minimum)

**Unit (`test_generate_gcp.py`)** — small hand-built resolved IR:
- a public Postgres primary: `ipv4_enabled = true`, authorized networks rendered, `ssl_mode` set, no
  `private_network`, no `depends_on`
- a private MySQL primary: `ipv4_enabled = false`, `private_network` referencing the network,
  `depends_on` the service networking connection, `binary_log_enabled` rather than
  `point_in_time_recovery_enabled`
- PSA blocks emitted once for two private instances in one network; two networks → two pairs;
  conflicting ranges in one network → `GenerationError` naming both instances
- a replica: `master_instance_name`, no backup configuration, no user, no variable
- `database_flags` rendered sorted; an empty dict emits no block
- `disk_autoresize_limit` omitted when autoresize is off
- an excluded instance produces nothing at all, and an excluded instance that was some other
  instance's replica primary → `GenerationError`
- every `GenerationError` case above, each asserting the message names the offending object
- a name longer than 98 characters is truncated, stays unique against a colliding sibling, and emits
  `GEN-RENAMED`
- comment headers contain the source ARN and each resolution key with `decided_by`

**Golden (`test_generate_gcp_golden.py`):** for every plan 1 fixture case, run
`Snapshot.from_dir` → `normalize` → `detect` → `resolve_ir` with defaults → `generate`, and compare
against `expected_database.tf` byte for byte. Fixtures gain that file; a regeneration helper lives in
`scripts/` only if one already exists for the network fixtures — otherwise write them by hand so the
goldens are reviewed, not recorded.

**Tofu (`test_generate_gcp_tofu.py`, marked `tofu`):** the generated `database.tf` plus the network
generator's output plus the boilerplate passes `tofu init -backend=false` and `tofu validate`, for
the public and the private fixture. This catches attribute-name mistakes the unit tests cannot.

**Coverage (`test_field_coverage.py`):** every field of every database IR model appears in
`FIELD_COVERAGE`, every declaration parses, and `rule:` declarations name a rule that exists in the
catalogue.

## Tasks
1. The generator's instance path (no PSA, no replicas) plus its unit tests, and `FIELD_COVERAGE` with
   the coverage test.
2. PSA, replicas, the password variables and the user, plus their unit tests and every
   `GenerationError` case.
3. Naming (the 98-character rule and collisions), the golden fixtures and the golden test.
4. The `tofu validate` test, the `gen_differences_doc.py` generalization, and the committed
   `docs/field-coverage-database.md`.
