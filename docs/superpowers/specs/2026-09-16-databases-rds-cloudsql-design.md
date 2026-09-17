# EasyMigrate — Databases: RDS → Cloud SQL (Spec 2)

Status: approved 2026-09-16
Supersedes: nothing. Builds on `2026-09-15-core-pipeline-networking-design.md` (spec 1), whose
architecture, CLI stages, IR base types and difference-engine model this spec assumes and extends.

## 1. Scope

Second family on the MVP roadmap (spec 1 §1). Depth-first (decision D18): this family is finished
in full — collection, faithful IR, a complete difference catalogue with tested strategies,
generation, and a live testbed run — before EKS starts.

### In scope

- `aws_db_instance` with engine `postgres` or `mysql`, including read replicas.
- Everything the RDS control plane reports about such an instance: class, storage, network
  placement, availability, backups, maintenance, parameters, encryption, logging, IAM auth.
- The Cloud SQL private-IP path, including the Private Services Access plumbing it requires.
- The difference catalogue that bridges the two (§5), and generation from the resolved IR (§6).

### Out of scope

- **Data.** Instances are created empty. Moving rows is the later paid service (decision D10).
- **Aurora** (`aws_rds_cluster`, engines `aurora-*`), MariaDB, Oracle, SQL Server, Db2 and
  RDS Custom. Each is reported as *not yet supported* inventory (§4.4), not as a difference.
- **Non-master database users, grants, schemas and extensions.** They live in the data plane,
  which a read-only control-plane scan cannot see.
- RDS Proxy, Performance Insights dashboards, Blue/Green deployments, snapshots, event
  subscriptions, and cross-region automated backups.
- Everything spec 1 puts out of scope: other destination clouds, the web app, cutover and rollback.

## 2. Two core changes this family forces

Everything else in this spec lives inside the new family. These two do not, and are called out
first because later families will depend on them.

### 2.1 `Decision.params`

`DB-PRIVATE-IP` must choose an IP range for the Private Services Access connection, and the user
must be able to choose a different one. Today a decision carries only a strategy id, so there is
nowhere to put that value.

`Decision` gains an optional `params`:

```python
class Decision(StrictModel):
    key: str
    strategy: str
    decided_by: Literal["default", "user"]
    acknowledged: bool = False
    params: dict[str, Any] = {}
```

```yaml
- key: DB-PRIVATE-IP@database_instance:test-postgres-db
  strategy: psa_auto_range
  decided_by: user
  params:
    range: 10.80.0.0
    prefix_length: 24
```

Rules declare and validate their own params:

```python
class DifferenceRule(ABC):
    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]: ...
    def validate_params(self, diff: Difference, strategy_id: str, params: dict) -> None: ...
```

- `default_params` returns `{}` for every rule that has none; the base class provides that default,
  so the existing networking rules are untouched.
- `resolve` fills `params` from `default_params` when a decision is created or when its strategy
  changes, and calls `validate_params` on every decision it loads. A hand-edited invalid value is a
  clear error naming the key, the param and why it was rejected — never a silent fallback.
- The decisions fingerprint stays keys-only: it answers "did the set of differences change since
  `resolve`?", which a param edit does not. An edited param is instead caught by `plan.lock.json`,
  which hashes the whole of `decisions.yaml` — so editing a param after `plan` makes `apply`
  correctly refuse as stale, and re-running `plan` picks the new value up.
- **Interactive `resolve` does not prompt for params.** It shows the defaults it chose and tells the
  user to edit `decisions.yaml` to change them. Prompting for free-form values per difference is a
  UI problem worth solving later, not now.

### 2.2 Passwords supplied at plan time

AWS never returns the master password. Decision D9 (generate one, store it in Secret Manager) is
**superseded for this family** by the user's decision on 2026-09-16: EasyMigrate never invents a
database credential. `plan` obtains one per instance, in this order:

1. `--db-password <ir-id>=<value>`, repeatable.
2. Environment variable `EASYMIGRATE_DB_PASSWORD_<SLUG>`, where `<SLUG>` is the IR id after the
   `database_instance:` prefix, uppercased with non-alphanumerics replaced by `_`.
3. An interactive no-echo prompt, only when stdin is a TTY.

With none of the three and no TTY, `plan` fails listing exactly which instances need a password and
both non-interactive ways to supply one. Values are checked against Cloud SQL's password rules
before anything is generated.

The generator emits one `variable` block per instance in `database.tf`, and `plan` writes the values
to `.easymigrate/terraform/secrets.auto.tfvars.json` at mode 0600 — OpenTofu loads `*.auto.tfvars.json`
automatically, so `apply` needs no re-entry. The file is gitignored. This is not new exposure: the
value is in `tfplan` and in the GCS state either way. The report says so in one plain sentence and
tells the user to rotate the password after cutover if they would rather it never existed on disk.

## 3. Architecture

No new components. One new plugin, one new IR module, one new rule module.

```
src/easymigrate/
├── ir/database.py                              # DatabaseInstance and its nested models
├── differences/rules/aws_gcp/database.py       # the DB-* catalogue (§5)
└── plugins/database/
    ├── __init__.py                             # DatabasePlugin
    ├── collect_aws.py
    ├── normalize.py
    ├── aws_values.py                           # engine/class/parameter lookup tables
    └── generate_gcp.py                         # + FIELD_COVERAGE
```

`registry.PLUGINS` becomes `(NetworkPlugin(), DatabasePlugin())`. Order matters: the database
normalizer resolves VPC and subnet native ids to IR ids through `ctx.lookup`, and fills
`firewall_policy.applies_to` for security groups attached to an RDS instance, which spec 1 left for
later plugins to do.

`scripts/gen_differences_doc.py` currently imports the network generator's `FIELD_COVERAGE` by name.
It is generalized to walk every registered plugin's `FIELD_COVERAGE`, writing one
`docs/field-coverage-<family>.md` per family (`docs/field-coverage.md` keeps its current name for
`network` so existing links survive). The no-silent-mapping test (D19) runs per family.

## 4. Collection and IR

### 4.1 AWS calls

| Snapshot file | API | Notes |
|---|---|---|
| `database.db_instances.json` | `DescribeDBInstances` | includes `TagList` |
| `database.db_subnet_groups.json` | `DescribeDBSubnetGroups` | resolves subnet placement |
| `database.db_parameter_groups.json` | `DescribeDBParameterGroups` | |
| `database.db_parameters.json` | `DescribeDBParameters` | per non-default group, `Source=user` only |
| `database.db_clusters.json` | `DescribeDBClusters` | to report Aurora as unsupported |
| `database.instance_types.json` | `ec2:DescribeInstanceTypes` | vCPU and memory for the classes in use |

`aws_resource_types = {"rds:db", "rds:cluster", "rds:subgrp", "rds:pg"}`.

Access denied on any of them goes through `ctx.access_denied(...)`, naming the IAM action, exactly as
the network collector does.

**Instance class sizing.** Cloud SQL tiers are expressed in vCPUs and memory, so the class name alone
is not enough. The collector strips the `db.` prefix and asks `ec2:DescribeInstanceTypes` for the
underlying type. Classes with no EC2 equivalent (`db.m5d.*` variants that EC2 does expose resolve
normally; `db.serverless` and any future RDS-only class do not) fall back to a small built-in table
in `aws_values.py`, and if that misses too, the IR records the class name with null vCPU/memory and a
`scan_gap` finding. `DB-TIER` then offers only `manual`.

### 4.2 IR — `database_instance`

One kind. A read replica is the same kind with `role="read_replica"` and `replica_of` set. DB subnet
groups and parameter groups do not become IR objects; they are folded into the instance, the way
route tables folded into `subnet.internet` in spec 1.

| Field | Built from | Notes |
|---|---|---|
| `engine` | `Engine` | `"postgres"` \| `"mysql"` |
| `engine_version` | `EngineVersion` | full string as reported, e.g. `"15.5"` |
| `role` | derived | `"primary"` \| `"read_replica"` |
| `replica_of` | `ReadReplicaSourceDBInstanceIdentifier` | IR id, or `external:<identifier>` when the primary is outside the scan |
| `compute.class_name` | `DBInstanceClass` | e.g. `"db.t3.micro"` |
| `compute.vcpus`, `compute.memory_mib` | EC2 lookup (§4.1) | nullable |
| `storage.size_gib` | `AllocatedStorage` | |
| `storage.max_size_gib` | `MaxAllocatedStorage` | null when RDS autoscaling is off |
| `storage.type`, `.iops`, `.throughput_mibps` | `StorageType`, `Iops`, `StorageThroughput` | |
| `storage.encrypted`, `.kms_key` | `StorageEncrypted`, `KmsKeyId` | |
| `network.network` | subnet group's `VpcId` → IR id | |
| `network.subnets` | subnet group's subnets → IR ids | ordered, deduplicated |
| `network.publicly_accessible` | `PubliclyAccessible` | |
| `network.port` | `Endpoint.Port` | |
| `network.firewall_policies` | `VpcSecurityGroups` → IR ids | |
| `high_availability` | `MultiAZ` | |
| `multi_az_cluster` | `DBClusterIdentifier` on a Multi-AZ DB cluster member | separate from `high_availability` |
| `zone` | `AvailabilityZone` | |
| `backup.retention_days`, `.window` | `BackupRetentionPeriod`, `PreferredBackupWindow` | |
| `backup.copy_tags_to_snapshot` | `CopyTagsToSnapshot` | |
| `maintenance.window` | `PreferredMaintenanceWindow` | `ddd:hh24:mi-ddd:hh24:mi` |
| `maintenance.auto_minor_version_upgrade` | `AutoMinorVersionUpgrade` | |
| `deletion_protection` | `DeletionProtection` | |
| `master_username` | `MasterUsername` | never a password |
| `initial_database` | `DBName` | the only database name the control plane knows |
| `parameters` | user-set parameter-group values | `dict[str, str]`, defaults excluded |
| `iam_auth` | `IAMDatabaseAuthenticationEnabled` | |
| `log_exports` | `EnabledCloudwatchLogsExports` | |
| `ca_certificate` | `CACertificateIdentifier` | recorded, not generated |

IR ids follow spec 1 §3.1: the `Name` tag when present, otherwise `DBInstanceIdentifier`.

### 4.3 Normalization rules (faithful capture only)

- Only `postgres` and `mysql` instances become IR objects.
- An instance in `creating`, `deleting` or `failed` state is captured with its status recorded and an
  `info` finding; the user may be looking at a half-built environment.
- A DB subnet group whose subnets span VPCs is impossible in AWS; if one is seen, `ctx.unmodelled`.
- Parameter values are captured verbatim, including AWS formula strings like
  `{DBInstanceClassMemory/32768}`. Judging them is `DB-PARAM-FLAGS`'s job, not the normalizer's.
- The normalizer emits no judgements about GCP. Every AWS/GCP gap is a `Difference` (§5).

### 4.4 Unsupported inventory

Aurora clusters and their members, MariaDB, Oracle, SQL Server, Db2 and RDS Custom instances each
produce one `unsupported` finding naming the engine, the identifier and the reason — for Aurora,
that its storage model, reader endpoints and serverless scaling have no Cloud SQL equivalent and it
needs a spec of its own. The scan still records them, so the user sees a complete inventory.

## 5. Difference catalogue (`DB-*`)

Subjects are IR object ids. Keys follow spec 1 §4.1: `<rule_id>@<subject id>`.

| Rule | Detects | Strategies (**default** first) |
|---|---|---|
| `DB-ENGINE-VERSION` | every instance | **`nearest_major`** (low): Cloud SQL `database_version` for the source major version; the patch level is Google's to choose · `manual`. When the major version predates Cloud SQL's oldest supported, the strategies are instead **`upgrade_to_oldest_supported`** (high — an engine upgrade, with application-visible behaviour changes) · `manual` |
| `DB-TIER` | every instance | **`nearest_custom_tier`** (medium): `db-custom-<vcpus>-<memory_mb>`, memory rounded up into Cloud SQL's 0.9–6.5 GB-per-vCPU band and to a 256 MB multiple · `shared_core` (medium): `db-f1-micro`/`db-g1-small`, chosen as the default instead when the source is a burstable micro or small class, which no custom tier can express · `manual` (the only strategy when sizing is unknown, §4.1) |
| `DB-PRIVATE-IP` | `publicly_accessible = false` | **`psa_auto_range`** (medium; params `range`, `prefix_length`): allocate a `/24` that overlaps no scanned VPC CIDR and no other allocation, generate the Private Services Access plumbing (§6.2), instance gets a private IP only · `public_ip_authorized_networks` (high): the database becomes internet-reachable, restricted to the SG's CIDRs · `manual` |
| `DB-PUBLIC-ACCESS` | `publicly_accessible = true` | **`public_ip_authorized_networks`** (medium, or high when any authorized network is `0.0.0.0/0`): authorized networks derived from the attached policies' ingress CIDR peers covering the DB port · `private_only` (medium): matches nothing in the source; every client must move inside the VPC · `manual` |
| `DB-SG-FIREWALL` | a `firewall_policy` whose only attachments are RDS instances | **`drop_with_note`** (medium): VPC firewall rules do not govern Cloud SQL — a private instance lives in a Google-managed producer VPC reached over the PSA peering, so anything in the peered VPC can reach it, and public access is controlled by authorized networks only. Sets a target hint on the `firewall_policy` so the network generator omits it instead of emitting a network-wide rule with an empty `applies_to` · `manual` |
| `DB-MULTI-AZ` | `high_availability` | **`regional_ha`** (low): `availability_type = REGIONAL` · `zonal` (medium): cheaper, no automatic failover. When `multi_az_cluster` is set (the three-instance readable-standby cluster), the only strategy is **`manual`**: Cloud SQL has no equivalent |
| `DB-STORAGE-AUTORESIZE` | every instance | **`match_source`** (low): autoresize on with `disk_autoresize_limit` = `max_size_gib` when RDS autoscaling was on, otherwise off — Cloud SQL defaults it on, so silence here would change behaviour · `enable_with_limit` (low) |
| `DB-STORAGE-TYPE` | `io1`/`io2`, or `gp3` with provisioned IOPS or throughput | **`pd_ssd`** (medium): Cloud SQL scales IOPS with disk size; the provisioned figures are lost and small disks may be slower than the source · `manual` |
| `DB-STORAGE-MINIMUM` | `size_gib` < 10 | **`grow_to_minimum`** (none): Cloud SQL's minimum disk is 10 GB |
| `DB-PARAM-FLAGS` | any user-set parameter | **`supported_flags_only`** (medium): translate parameters on a per-engine supported-flag list into `database_flags`; drop the rest, and every AWS formula value, listing each dropped parameter with its value in the report · `manual` |
| `DB-BACKUP-WINDOW` | `backup.retention_days > 0` | **`nearest_equivalent`** (low): backup window start → `start_time` (Cloud SQL has a start time, not a window), retention → `retained_backups`, clamped to Cloud SQL's limits with the clamp reported · `manual`. When retention is 0, the strategies are **`no_backups`** (high — matches the source) · `enable_default_backups` (none) |
| `DB-PITR` | `backup.retention_days > 0` | **`enable`** (low): `point_in_time_recovery_enabled` (Postgres) or `binary_log_enabled` (MySQL), with transaction-log retention clamped to Cloud SQL's maximum, which is shorter than RDS's 35 days · `disable` (medium) |
| `DB-MAINTENANCE-WINDOW` | `maintenance.window` set | **`nearest_hour`** (low): Cloud SQL takes one day and one hour, not a range with minutes; the window start is rounded down to the hour · `manual` |
| `DB-PORT` | `network.port` not the engine default | **`default_port`** (medium): Cloud SQL's port is fixed; every client connection string changes · `manual` |
| `DB-KMS-CMEK` | `storage.encrypted` with a customer-managed KMS key | **`google_managed`** (low): still encrypted at rest, by Google-managed keys · `manual`: create a Cloud KMS key and grant the Cloud SQL service identity, then set `encryption_key_name` |
| `DB-IAM-AUTH` | `iam_auth` | **`enable_flag`** (medium): set `cloudsql_iam_authentication`; the IAM users themselves are not created, since the AWS-side grants are invisible · `manual` |
| `DB-LOG-EXPORTS` | `log_exports` non-empty | **`note_only`** (none): Cloud SQL sends logs to Cloud Logging by default; the per-log-type selection has no equivalent |
| `DB-REPLICA` | `role = "read_replica"` | **`cloud_sql_replica`** (low): `master_instance_name` plus `replica_configuration`. When `replica_of` is `external:*`, the only strategy is **`manual`** |
| `DB-USERS` | every instance | **`root_user_only`** (medium): only the master user is visible to the control plane; every other user, grant, schema and extension must be recreated after cutover |
| `DB-NAME-REUSE` | every instance | **`keep`** (none): informational — a deleted Cloud SQL instance name cannot be reused for about a week, which matters when re-running a migration |

Every `manual` strategy needs explicit acknowledgement in `resolve`, as in spec 1.

**Rule interactions**, applied in this order: `DB-PRIVATE-IP` and `DB-PUBLIC-ACCESS` are mutually
exclusive by detection. `DB-SG-FIREWALL` runs after both, because whether the policy is still needed
depends on which of them was chosen — `public_ip_authorized_networks` consumes the policy's CIDR
peers, and `drop_with_note` then removes the policy from generation. `DB-TIER` runs after
`DB-ENGINE-VERSION`, because shared-core tiers are not available for every engine version.

Cloud SQL also imposes two API-level prerequisites that no `DB-*` rule enforces on its own:
`availability_type = REGIONAL` (from `DB-MULTI-AZ`) requires automated backups (`DB-BACKUP-WINDOW`),
and a read replica (`DB-REPLICA`) requires its primary to have both automated backups
(`DB-BACKUP-WINDOW`) and point-in-time recovery or binary logging (`DB-PITR`) enabled. Nothing in
this spec forces one rule's default to satisfy another's, so the generator (§6) refuses to emit HCL
for either invalid combination with `GenerationError`, rather than silently turning backups on and
inventing a difference decision that was never presented to the user.

## 6. Generation

`plugins/database/generate_gcp.py` reads only the resolved IR and its target hints, and declares
every field it touches in `FIELD_COVERAGE` (D19). Output file: `terraform/database.tf`.

### 6.1 Resources

- `google_sql_database_instance` per instance, with `settings` assembled from the hints:
  `tier`, `availability_type`, `disk_size`, `disk_type`, `disk_autoresize`, `disk_autoresize_limit`,
  `backup_configuration`, `maintenance_window`, `database_flags`, `deletion_protection_enabled`,
  and `ip_configuration` (`ipv4_enabled`, `private_network`, `authorized_networks`, `ssl_mode`).
- `google_sql_database` for `initial_database`, when there is one.
- `google_sql_user` for the master user, its `password` reading the per-instance variable (§2.2).
- A read replica is the same resource with `master_instance_name` and `replica_configuration`, and no
  backup configuration of its own.
- Comment headers carry the source ARN and the decisions that shaped the resource, exactly as spec 1
  §5.2 requires.

### 6.2 Private Services Access

When any instance in a network resolved to `psa_auto_range`, the generator emits, **once per
network**:

- `google_compute_global_address` with `purpose = "VPC_PEERING"`, `address_type = "INTERNAL"`, the
  range and prefix length from the decision params;
- `google_service_networking_connection` joining that network to `servicenetworking.googleapis.com`.

Instances set `settings.ip_configuration.private_network` to the network and `depends_on` the
connection. The report warns that deleting a service networking connection can be slow and that the
allocated range cannot overlap anything later added to the VPC.

### 6.3 Naming

Cloud SQL instance names are lowercase letters, digits and hyphens, must start with a letter, and are
at most 98 characters. Names are slugged with the existing `providers/gcp/naming.py` helpers, and a
changed name produces the usual `GEN-RENAMED` finding.

### 6.4 `plan` preflight

Before generating, `plan` checks that `sqladmin.googleapis.com` is enabled on the target project, and
`servicenetworking.googleapis.com` too when PSA is needed. A missing API is an error quoting the
exact `gcloud services enable` command. EasyMigrate does **not** generate `google_project_service`
resources: their `disable_on_destroy` default would turn a teardown of the migration into an outage
of everything else in the project.

## 7. Report

Beyond the standard difference and finding sections, the database section states, for each instance:
the chosen tier and what it was derived from, the resulting connectivity (private IP with its range,
or public IP with its authorized networks), the backup and PITR settings after clamping, every
dropped parameter, and the sentence about the password being present in state.

## 8. Testing

Mirrors spec 1 §7.

- **Collect:** moto-backed tests for each API, including access-denied paths and the EC2 sizing
  lookup with its fallbacks.
- **Fixtures** under `tests/plugins/database/fixtures/<case>/` with `snapshot/*.json`,
  `expected_ir.json` and `expected_database.tf`: `public_postgres` (the current testbed instance),
  `private_mysql_multiaz`, `read_replica`, `param_flags`, `unsupported_engines` (Aurora + Oracle),
  `custom_port`, `old_engine_version`, `psa_range_conflict` (VPC CIDRs that force a non-obvious
  allocation), `no_backups`.
- **Rules:** one test per rule per strategy, plus the three interaction orderings in §5.
- **Generation:** golden `.tf` comparison, a `tofu validate` run marked `tofu`, and the
  `FIELD_COVERAGE` completeness test for the new generator.
- **Live:** one run against the testbed. `testbed/aws/main.tf` gains a private MySQL instance
  alongside the existing public Postgres, so both connectivity paths are exercised, then everything
  is destroyed. Cloud SQL is billed per hour, so this runs once, deliberately.

## 8b. Proven live (2026-09-17)

A full `scan → analyze → resolve → plan → apply` ran against a real AWS account and
`easymigrate-test-30319`: 20 GCP resources created, both engines RUNNABLE, private IP
`10.64.0.3` inside the allocated `10.64.0.0/24` PSA range with an ACTIVE
`servicenetworking` peering, MySQL `max_connections` translated and
`max_heap_table_size` dropped as unsupported. Both clouds torn down afterwards.

Two things the run exposed that the test suite could not:

- **Real RDS reports `StorageThroughput: 0` on `gp2`** rather than omitting it, which the
  IR rejects (§4.2 allows provisioned values only on gp3/io1/io2). `analyze` failed
  outright. Fixed by treating 0 as absent. Every hand-built fixture had omitted the field,
  so no test caught it — a reminder that fixtures encode our idea of an API response.
- **A security group whose ingress references another security group yields no authorized
  networks**, so a `publicly_accessible` RDS instance migrates to a Cloud SQL instance with
  a public IP and an empty allow-list — reachable by nobody. Confirmed by connecting to the
  real IP and being refused. **Fixed 2026-09-17:** when no authorized network can be
  derived, `DB-PUBLIC-ACCESS` now offers a different strategy set — `public_ip_allow_cidrs`
  (params: `cidrs`, high risk), `private_only`, `manual` — and defaults to `manual`, so the
  case lands under "Needs your action" instead of silently generating an empty allow list.
  EasyMigrate will not guess who may reach a database over the internet, and will not
  quietly change a database's exposure model either; the user chooses.

## 9. Done when

- `scan → analyze → resolve → plan → apply` migrates the testbed's Postgres and MySQL instances into
  a working Cloud SQL pair, one public and one private over PSA, and both accept a connection with
  the password supplied at plan time.
- Every rule in §5 has a passing test for every strategy.
- `docs/differences.md` and the per-family field-coverage docs regenerate with no diff.
- Aurora and the other engines appear in the report as not yet supported, with their identifiers.
