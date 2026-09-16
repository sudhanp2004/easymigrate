# Spec 2, Plan 2 of 4: `Decision.params` and the `DB-*` Rule Catalogue

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** decisions can carry rule-validated parameters, and `analyze`/`resolve` detect and resolve
every AWS→GCP database difference in spec §5. After this plan, `resolved_ir.json` carries every hint
plan 3's generator needs.

**Spec:** `docs/superpowers/specs/2026-09-16-databases-rds-cloudsql-design.md` §2.1, §5 (binding).
Also spec 1 §4 for the engine contract this extends.

## Global Constraints
- `easymigrate.differences.*` imports only `easymigrate.ir.*`, the stdlib, `pydantic` and `yaml`.
- Rules are pure: `detect` and `apply` never mutate their input; `apply` returns a new document via
  `model_copy(deep=True)`, and the engine re-validates after each one.
- Deterministic: differences sorted by key, rules applied in catalogue order.
- Decision keys are `"<RULE_ID>@<primary subject id>"`.
- Every `manual` strategy requires acknowledgement in `resolve`. That is existing behaviour.
- Cloud SQL facts (supported versions, tier bands, limits) live in **one** module-level table with a
  comment recording the date they were checked (2026-09-16), so a future refresh is one edit.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/differences/model.py            # Decision.params, DifferenceRule param hooks
src/easymigrate/differences/decisions.py        # params in merge, validation and fingerprint
src/easymigrate/differences/engine.py           # pass params into apply()
src/easymigrate/differences/rules/aws_gcp/__init__.py   # AWS_GCP_RULES += the DB rules
src/easymigrate/differences/rules/aws_gcp/database.py   # the 20 DB-* rules
src/easymigrate/differences/rules/aws_gcp/cloudsql_facts.py  # version, tier and limit tables
src/easymigrate/cli.py                          # resolve prints params
scripts/gen_differences_doc.py                  # render params in docs/differences.md
docs/differences.md                             # regenerated
tests/differences/{test_model.py,test_decisions.py,test_engine.py,test_database_rules.py,test_docs.py}
```

## Part A — `Decision.params` (core)

### `model.py`
```python
class Decision(StrictModel):
    key: str
    strategy: str
    decided_by: Literal["default", "user"]
    acknowledged: bool = False
    params: dict[str, Any] = Field(default_factory=dict)   # JSON-safe scalars and lists only
```

`DifferenceRule` gains two non-abstract hooks, defaulting to no params so the eleven networking rules
need no change:
```python
def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]: return {}
def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None: return None
```
`validate_params` raises `ValueError` with a sentence naming the param and why it was rejected.

`apply` gains the params: `apply(self, ir, diff, strategy_id, params: dict[str, Any] | None = None)`.
Existing rules keep their signature by accepting and ignoring it — update all eleven to the new
signature in one mechanical commit so there is exactly one calling convention.

### `decisions.py`
- `merge_decisions` fills `params` from `rule.default_params(diff, strategy)` when a decision is
  created **or when its strategy changed**; an unchanged strategy keeps the user's params. Params
  belonging to a strategy that no longer exists are dropped with the decision, as today.
- `merge_decisions` calls `rule.validate_params` on every kept decision; a failure drops the params
  back to the defaults and reports it in the existing `dropped` list, phrased as
  `"<key>: <message>; reset to defaults"`.
- `fingerprint` is unchanged (keys only), but `DecisionSet` already stores it — add the params to the
  **readiness** check instead: `readiness_problems` calls `validate_params` and reports any failure,
  so a hand-edited invalid value blocks `plan` with a clear sentence rather than generating.
- `to_yaml` omits `params` when empty, so existing files round-trip byte-identically.

### `engine.py`
`resolve_ir` looks the decision's params up and passes them to `apply`.

### CLI
Interactive `resolve` prints, after the chosen strategy, any params as `key: value` lines followed by
`Edit decisions.yaml to change these.` It does **not** prompt for them (spec §2.1).

## Part B — `cloudsql_facts.py`

Checked 2026-09-16; each table carries that date in a comment.
- `SUPPORTED_VERSIONS: dict[str, list[str]]` — engine → Cloud SQL major versions, newest first, and
  `OLDEST_SUPPORTED: dict[str, str]`.
- `DATABASE_VERSION: dict[tuple[str, str], str]` — (engine, major) → `"POSTGRES_15"`, `"MYSQL_8_0"`.
- `SHARED_CORE_TIERS = ("db-f1-micro", "db-g1-small")` with their vCPU/memory figures, and
  `MEMORY_PER_VCPU_MIB = (922, 6656)`, `MEMORY_STEP_MIB = 256` for the custom-tier band.
- `MIN_DISK_GIB = 10`.
- `MAX_RETAINED_BACKUPS = 365`, `MAX_TRANSACTION_LOG_RETENTION_DAYS = 7`.
- `SUPPORTED_FLAGS: dict[str, frozenset[str]]` — engine → Cloud SQL database flag names. Seed it from
  the flags the fixtures need plus the common ones (`max_connections`, `log_min_duration_statement`,
  `shared_buffers` *excluded* — it is not settable on Cloud SQL — `work_mem`, `autovacuum`,
  `character_set_server`, `collation_server`, `sql_mode`, `slow_query_log`, `long_query_time`,
  `innodb_lock_wait_timeout`, `cloudsql_iam_authentication`). A comment says how to extend it.
- `FORMULA = re.compile(r"\{.*\}")` — AWS parameter formula values, which never translate.

## Part C — the `DB-*` catalogue, in this order

Order is load-bearing (spec §5): connectivity rules before `DB-SG-FIREWALL`, `DB-ENGINE-VERSION`
before `DB-TIER`. Contexts hold JSON-safe values only. Bold = default.

| # | Rule | Detects (one difference per…) | Strategies and what `apply` does |
|---|---|---|---|
| 1 | `DB-ENGINE-VERSION` | each instance; context `{engine, version, major, target}` | **`nearest_major`** (low): `target_hints["database_version"] = DATABASE_VERSION[...]`. `manual` (manual): `excluded`. When the major version is older than `OLDEST_SUPPORTED`, the strategy pair is instead **`upgrade_to_oldest_supported`** (high) setting the oldest supported version, and `manual`. Both variants are the same rule id with different `strategies` chosen at detect time via `context["upgrade_required"]`, and `strategy()` accepts both sets. |
| 2 | `DB-TIER` | each instance | **`nearest_custom_tier`** (medium): `target_hints["tier"] = f"db-custom-{vcpus}-{mem_mb}"`, memory clamped into `MEMORY_PER_VCPU_MIB` and rounded up to `MEMORY_STEP_MIB`; context records the source class and both figures. **`shared_core`** (medium) is the default instead when the class is burstable micro/small: sets `db-f1-micro` or `db-g1-small`. `manual` (manual): `excluded`. When `compute.vcpus` is None the only strategy is `manual`. |
| 3 | `DB-PRIVATE-IP` | instance with `publicly_accessible == False`; subjects = instance | **`psa_auto_range`** (medium; params `range`, `prefix_length`): `target_hints["private_network"] = <network id>` and `target_hints["psa"] = {"range": …, "prefix_length": …}`. `public_ip_authorized_networks` (high): `target_hints["public_ip"] = True` plus `authorized_networks` from the attached policies (same derivation as rule 4). `manual` (manual): `excluded`. |
| 4 | `DB-PUBLIC-ACCESS` | instance with `publicly_accessible == True` | **`public_ip_authorized_networks`** (medium; high when any resulting network is `0.0.0.0/0`, recorded in `context["open_to_world"]`): `target_hints["public_ip"] = True`, `target_hints["authorized_networks"] = [...]` — every `CidrPeer` CIDR in an ingress rule of an attached policy whose ports cover `network.port`, sorted and deduplicated. `private_only` (medium): behaves like `psa_auto_range`, with the same params. `manual` (manual): `excluded`. |
| 5 | `DB-SG-FIREWALL` | each `firewall_policy` whose `applies_to` is non-empty and contains only `database_instance:` ids | **`drop_with_note`** (medium): `excluded = "DB-SG-FIREWALL: VPC firewall rules do not govern Cloud SQL"` on the policy. `manual` (manual): the same exclusion, acknowledged. |
| 6 | `DB-MULTI-AZ` | instance with `high_availability` or `multi_az_cluster` | **`regional_ha`** (low): `target_hints["availability_type"] = "REGIONAL"`. `zonal` (medium): `"ZONAL"`. When `multi_az_cluster`, the only strategy is **`manual`** (manual): `excluded`. |
| 7 | `DB-STORAGE-AUTORESIZE` | each instance | **`match_source`** (low): `target_hints["disk_autoresize"] = storage.max_size_gib is not None` and, when set, `disk_autoresize_limit`. `enable_with_limit` (low): autoresize on with the limit from `max_size_gib` or 0 (unlimited). |
| 8 | `DB-STORAGE-TYPE` | `type` in `io1`/`io2`, or `gp3` with `iops`/`throughput_mibps` set | **`pd_ssd`** (medium): `target_hints["disk_type"] = "PD_SSD"`; context records the dropped IOPS and throughput. `manual` (manual): `excluded`. |
| 9 | `DB-STORAGE-MINIMUM` | `storage.size_gib < MIN_DISK_GIB` | **`grow_to_minimum`** (none): `target_hints["disk_size"] = MIN_DISK_GIB`. |
| 10 | `DB-PARAM-FLAGS` | instance with a non-empty `parameters` | **`supported_flags_only`** (medium): `target_hints["database_flags"] = {k: v}` for keys in `SUPPORTED_FLAGS[engine]` whose value is not a `FORMULA` match; `target_hints["dropped_parameters"] = {k: v}` for the rest; context lists both. `manual` (manual): `excluded`. |
| 11 | `DB-BACKUP-WINDOW` | each instance | with `retention_days > 0`: **`nearest_equivalent`** (low): `target_hints["backup"] = {"enabled": True, "start_time": <window start as "HH:MM">, "retained_backups": min(retention_days, MAX_RETAINED_BACKUPS)}`, context records any clamp. `manual` (manual): `excluded`. With `retention_days == 0`: **`no_backups`** (high) setting `{"enabled": False}`, and `enable_default_backups` (none) setting enabled with 7 retained backups at 03:00. |
| 12 | `DB-PITR` | instance with `retention_days > 0` | **`enable`** (low): `target_hints["pitr"] = {"enabled": True, "log_retention_days": min(retention_days, MAX_TRANSACTION_LOG_RETENTION_DAYS)}`; context records the clamp when RDS retention was longer. `disable` (medium): `{"enabled": False}`. |
| 13 | `DB-MAINTENANCE-WINDOW` | instance with `maintenance.window` | **`nearest_hour`** (low): `target_hints["maintenance_window"] = {"day": 1-7, "hour": 0-23}` from the window start, minutes discarded; context records the original window. `manual` (manual): no hints, no exclusion — the instance is generated without a maintenance window. |
| 14 | `DB-PORT` | `network.port != DEFAULT_PORT[engine]` | **`default_port`** (medium): no hints; context records the source port and the fixed Cloud SQL port. `manual` (manual): `excluded`. |
| 15 | `DB-KMS-CMEK` | `storage.encrypted and storage.kms_key` | **`google_managed`** (low): no hints; context records the AWS key ARN. `manual` (manual): `excluded`. |
| 16 | `DB-IAM-AUTH` | `iam_auth` | **`enable_flag`** (medium): adds `cloudsql_iam_authentication = "on"` to `target_hints["database_flags"]`, merging with rule 10's. `manual` (manual): no hints, no exclusion. |
| 17 | `DB-LOG-EXPORTS` | non-empty `log_exports` | **`note_only`** (none): no change; context lists the log types. |
| 18 | `DB-REPLICA` | `role == "read_replica"`; subjects = replica, then primary when it is an IR id | **`cloud_sql_replica`** (low): `target_hints["master_instance"] = <primary IR id>`. When `replica_of` starts with `external:`, the only strategy is **`manual`** (manual): `excluded`. |
| 19 | `DB-USERS` | each instance | **`root_user_only`** (medium): no change; context records `master_username`. |
| 20 | `DB-NAME-REUSE` | each instance | **`keep`** (none): no change. |

**`DB-PRIVATE-IP` param defaults.** `default_params` picks the first `/N` (default `prefix_length`
24) inside `10.0.0.0/8` that overlaps no `Network.cidr_blocks` in the document and no range already
allocated to another network by this rule, scanning from `10.64.0.0` upward in `prefix_length`-sized
steps; the allocation is per network, so two private instances in the same network get the same
range. `validate_params` requires `range` to be a valid IPv4 address that is the network address for
`prefix_length`, `prefix_length` between 16 and 24, and the resulting network to overlap no scanned
VPC CIDR — the error sentence names the overlapping VPC.

**Rule interactions to honour in `apply`:** rules 10 and 16 both write `database_flags`; each merges
into the existing dict rather than replacing it. Rule 5 runs after 3 and 4, so the authorized-network
derivation still sees the policy before it is excluded.

## Docs
`scripts/gen_differences_doc.py` groups rules by `family` with a heading per family, and renders each
strategy's params by calling `default_params` on a synthetic difference — or, simpler and preferred,
by having each rule declare `param_docs: ClassVar[dict[str, str]]` (param name → one-line meaning)
and rendering that. Pick the declarative one. `docs/differences.md` is regenerated and committed;
`test_docs.py` already fails on drift.

## Required test cases (minimum)

**Params (core):**
- a decision with params round-trips through YAML; an empty `params` is omitted from the output
- `merge_decisions` fills defaults on creation, keeps user params when the strategy is unchanged, and
  refills them when the strategy changes
- invalid hand-edited params → reported in `dropped` and reset to defaults on merge, and reported by
  `readiness_problems` when they reach `plan`
- the eleven networking rules still resolve unchanged (existing tests must pass untouched)

**Per DB rule (20×):** detect positive, detect negative, `apply` for every strategy asserting the
exact resulting `target_hints`/`excluded`, and the input document unchanged afterwards.

**Specifically:**
- `DB-ENGINE-VERSION`: postgres 15 → `POSTGRES_15`; postgres 9.6 → the upgrade variant with only two
  strategies; mysql 8.0 → `MYSQL_8_0`
- `DB-TIER`: `db.t3.micro` → `db-f1-micro` by default; `db.m5.large` (2 vCPU, 8 GiB) →
  `db-custom-2-8192`; a class whose memory is below the band → rounded up to the band's floor;
  unknown sizing → `manual` only
- `DB-PRIVATE-IP`: default range avoids a `10.64.0.0/16` VPC; two instances in one network share a
  range; two networks get different ranges; a hand-set overlapping range fails validation naming the
  VPC; `prefix_length` 25 fails
- `DB-PUBLIC-ACCESS`: authorized networks derived from an ingress rule covering 5432 and not from one
  covering only 22; `0.0.0.0/0` sets `open_to_world` and raises the risk
- `DB-SG-FIREWALL`: fires for a DB-only policy, does **not** fire for a policy also attached to
  something else, and runs after rule 4 so the derivation still worked
- `DB-PARAM-FLAGS`: a supported flag is kept, an unsupported one and a formula value are dropped and
  listed; then `DB-IAM-AUTH` merges its flag into the same dict without losing it
- `DB-BACKUP-WINDOW`: `"03:00-04:00"` → `start_time "03:00"`; retention 35 → clamped report; the
  retention-0 variant offers the other two strategies
- `DB-PITR`: retention 35 → log retention clamped to 7 with the clamp in context
- `DB-MAINTENANCE-WINDOW`: `"sun:05:30-sun:06:30"` → day 7, hour 5
- `DB-REPLICA`: an IR-id primary → hint; an `external:` primary → `manual` only

**Golden:** for each plan 1 fixture case, the detected `DB-*` keys are asserted as an exact list.

## Tasks
1. `Decision.params`, the rule hooks, the eleven networking rules' signature update, decisions and
   engine changes, and their tests.
2. `cloudsql_facts.py` plus rules 1–5 (version, tier, connectivity, firewall) with tests.
3. Rules 6–14 with tests.
4. Rules 15–20, the docs generator's family grouping and `param_docs`, the regenerated
   `docs/differences.md`, and the fixture-based key assertions.
