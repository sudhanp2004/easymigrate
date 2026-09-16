# Spec 2, Plan 1 of 4: Database IR, Collection and Normalization

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `easymigrate scan` collects RDS, and `easymigrate analyze` turns it into faithful
`database_instance` IR objects, with unsupported engines reported as inventory findings. No GCP
knowledge is added anywhere in this plan.

**Spec:** `docs/superpowers/specs/2026-09-16-databases-rds-cloudsql-design.md` §3, §4 (binding).

## Global Constraints
- `easymigrate.ir.*` imports only the stdlib and pydantic. Plugins import `ir` and `providers`, never
  `differences`. `tests/test_dependency_rules.py` enforces this; it needs no change.
- The normalizer is **faithful**: it records what AWS reports, including things GCP cannot do, and
  makes no translatability judgements. Every AWS/GCP gap is a `Difference` (plan 2), never a Finding.
- Deterministic output: objects sorted by id, lists sorted, no dependence on scan order.
- IR ids follow spec 1 §3.1 — the `Name` tag when present, otherwise the native identifier, slugged;
  same-named siblings get their slugged native id appended. Reuse `easymigrate.ir.naming`.
- Collectors never raise on access denied: they call `ctx.access_denied(api, iam_action)` and return
  what they got.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...` (uv may not be on PATH). Every task ends green:
  `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/ir/database.py                  # DatabaseInstance + nested models
src/easymigrate/plugins/database/
├── __init__.py                                 # DatabasePlugin
├── collect_aws.py                              # collect(connection, ctx) -> dict[str, Any]
├── aws_values.py                               # lookup tables (instance classes, engine defaults)
└── normalize.py                                # normalize(snapshot, ctx) -> list[IntentObject]
src/easymigrate/core/registry.py                # PLUGINS += DatabasePlugin()
tests/plugins/database/{__init__.py,aws_builders.py,test_collect_aws.py,test_normalize.py,test_plugin.py}
tests/ir/test_database.py
```

`generate_gcp.py` is plan 3. Until then `DatabasePlugin.generate_gcp` returns an empty `HclFile`, so
the plugin protocol is satisfied and `plan` keeps working.

## Interfaces

### `ir/database.py`
All models are `StrictModel`. Reuse `Cidr4`, `NetworkRef`, `SubnetRef`, `PolicyRef` from
`easymigrate.ir.network`.

```python
Engine = Literal["postgres", "mysql"]
StorageType = Literal["gp2", "gp3", "io1", "io2", "standard"]

class DbCompute(StrictModel):
    class_name: str                  # "db.t3.micro"
    vcpus: int | None = None
    memory_mib: int | None = None

class DbStorage(StrictModel):
    size_gib: int
    max_size_gib: int | None = None  # None = RDS autoscaling off
    type: StorageType
    iops: int | None = None
    throughput_mibps: int | None = None
    encrypted: bool = False
    kms_key: str | None = None       # ARN; None means the AWS-managed key

class DbNetwork(StrictModel):
    network: NetworkRef | None = None      # None only when the VPC was not scanned
    subnets: list[SubnetRef] = []          # sorted, deduplicated
    publicly_accessible: bool = False
    port: int
    firewall_policies: list[PolicyRef] = []  # sorted

class DbBackup(StrictModel):
    retention_days: int = 0
    window: str | None = None        # "03:00-04:00" UTC, as RDS reports it
    copy_tags_to_snapshot: bool = False

class DbMaintenance(StrictModel):
    window: str | None = None        # "ddd:hh24:mi-ddd:hh24:mi"
    auto_minor_version_upgrade: bool = True

class DatabaseInstance(IntentObject):
    kind: Literal["database_instance"] = "database_instance"
    engine: Engine
    engine_version: str                     # full string as reported, e.g. "15.5"
    role: Literal["primary", "read_replica"] = "primary"
    replica_of: str | None = None           # "database_instance:<id>" or "external:<identifier>"
    compute: DbCompute
    storage: DbStorage
    network: DbNetwork
    high_availability: bool = False
    multi_az_cluster: bool = False
    zone: str | None = None
    backup: DbBackup = DbBackup()
    maintenance: DbMaintenance = DbMaintenance()
    deletion_protection: bool = False
    master_username: str | None = None
    initial_database: str | None = None
    parameters: dict[str, str] = {}         # user-set only, keys sorted
    iam_auth: bool = False
    log_exports: list[str] = []             # sorted
    ca_certificate: str | None = None
    status: str | None = None               # only when not "available"

    def references(self) -> list[str]:
        # network, subnets, firewall_policies, and replica_of when it is an IR id
```

Validation rules enforced by the model:
- `role == "read_replica"` requires `replica_of`; `role == "primary"` forbids it.
- `storage.iops` and `storage.throughput_mibps` are only allowed for `gp3`, `io1`, `io2`.
- `network.port` is 1–65535.
- `replica_of` matches `^(database_instance:|external:).+`.

**Never add a password field.** The model must have nowhere to put one.

### `plugins/database/collect_aws.py`
`collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]` returns the six snapshot
files in spec §4.1, each `{"<Key>": [...]}` shaped like the boto3 response, paginated fully:

| File | Call | IAM action |
|---|---|---|
| `database.db_instances` | `rds.describe_db_instances` | `rds:DescribeDBInstances` |
| `database.db_subnet_groups` | `rds.describe_db_subnet_groups` | `rds:DescribeDBSubnetGroups` |
| `database.db_parameter_groups` | `rds.describe_db_parameter_groups` | `rds:DescribeDBParameterGroups` |
| `database.db_parameters` | `rds.describe_db_parameters` per group, `Source="user"` | `rds:DescribeDBParameters` |
| `database.db_clusters` | `rds.describe_db_clusters` | `rds:DescribeDBClusters` |
| `database.instance_types` | `ec2.describe_instance_types` | `ec2:DescribeInstanceTypes` |

Collection rules:
- `db_parameters` is keyed by group name: `{"<group-name>": [ {ParameterName, ParameterValue}, ... ]}`.
  Only groups actually attached to a collected instance are queried, and only default groups are
  skipped (a group named `default.*` can still be attached and is then queried like any other).
- `instance_types` is one call with the deduplicated, sorted list of `DBInstanceClass` values with
  the `db.` prefix stripped. Classes EC2 rejects (`InvalidInstanceType`) are dropped from the request
  and simply absent from the file — no exception, no finding here; `normalize` decides.
- Tags come from `DescribeDBInstances`' `TagList`; no extra `ListTagsForResource` calls.
- Access denied on any call → `ctx.access_denied(...)` and that file is `{}`.

### `plugins/database/aws_values.py`
- `FALLBACK_INSTANCE_TYPES: dict[str, tuple[int, int]]` — class name (without `db.`) → (vCPUs,
  memory MiB), covering the classes EC2 does not expose. Seed it with `t3.micro`, `t3.small`,
  `t3.medium`, `t4g.micro`, `t4g.small`, `t4g.medium`, `m5.large`, `m5.xlarge`, `r5.large`,
  `r5.xlarge`. Each entry carries a comment with the AWS-documented figures it came from.
- `DEFAULT_PORT: dict[str, int] = {"postgres": 5432, "mysql": 3306}`.
- `SUPPORTED_ENGINES = frozenset({"postgres", "mysql"})`.
- `UNSUPPORTED_ENGINE_REASONS: dict[str, str]` — one sentence per engine family
  (`aurora-postgresql`, `aurora-mysql`, `mariadb`, `oracle-*`, `sqlserver-*`, `db2-*`, `custom-*`)
  explaining why it is not yet supported, matching spec §4.4.

### `plugins/database/normalize.py`
`normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]`

Mapping is spec §4.2, verbatim. Beyond it:
- Subnet-group VPC and subnets resolve to IR ids by scanning `ctx.objects` for `Network`/`Subnet`
  objects whose `source.id` matches the native id. A native id that resolves to nothing leaves
  `network=None` / omits the subnet, and adds one `scan_gap` finding per instance naming the ids —
  this is what happens when the VPC lives outside the scanned region.
- Security groups resolve the same way to `firewall_policy` ids; **and for each resolved policy, the
  instance's IR id is appended to that policy's `applies_to`**, sorted and deduplicated (spec 1 left
  `applies_to` for later plugins to fill).
- `compute.vcpus` / `memory_mib`: `instance_types` first, then `FALLBACK_INSTANCE_TYPES`, then
  `None` plus one `scan_gap` finding `SCAN-DB-CLASS-UNKNOWN` naming the class.
- `parameters`: the attached group's user-set parameters, values as reported (formula strings such
  as `{DBInstanceClassMemory/32768}` are kept verbatim). A parameter with no `ParameterValue` is
  skipped.
- `replica_of`: `ReadReplicaSourceDBInstanceIdentifier` resolved to the IR id of the instance with
  that `DBInstanceIdentifier`; if it is not in the snapshot, `external:<identifier>`.
- `multi_az_cluster` is `True` when the instance has a `DBClusterIdentifier` naming a cluster in
  `db_clusters` whose engine is `postgres`/`mysql` (the Multi-AZ DB cluster shape) — not Aurora.
- `status`: set only when `DBInstanceStatus != "available"`, plus one `info` finding
  `DB-STATUS-TRANSIENT` naming the status.
- **Unsupported inventory:** every instance whose engine is not in `SUPPORTED_ENGINES`, and every
  Aurora cluster, produces one `unsupported` finding, code `DB-ENGINE-UNSUPPORTED`, naming the
  engine, the identifier and the reason from `UNSUPPORTED_ENGINE_REASONS`. They become no IR object.
- A DB subnet group whose subnets span more than one VPC → `ctx.unmodelled(...)`.

### `plugins/database/__init__.py`
```python
class DatabasePlugin:
    name = "database"
    aws_resource_types = frozenset({"rds:db", "rds:cluster", "rds:subgrp", "rds:pg"})
```
with the three protocol methods delegating to the modules above.

### `core/registry.py`
`PLUGINS = (NetworkPlugin(), DatabasePlugin())`. Order is load-bearing: the database normalizer reads
network objects out of `ctx.objects`, which `pipeline.analyze` fills plugin by plugin.

## Required test cases (minimum)

**`tests/ir/test_database.py`**
- a minimal valid instance round-trips through `IRDocument.model_validate_json`
- `references()` returns network, subnets, policies and an IR-id `replica_of`, and omits an
  `external:` one
- read_replica without `replica_of` raises; primary with one raises
- `iops` on `gp2` raises; on `gp3` is accepted
- port 0 and port 70000 raise
- an unknown extra field raises (StrictModel)

**`tests/plugins/database/test_collect_aws.py`** (moto, plus a stub connection where moto is thin)
- every file is produced with the expected key, and pagination is followed (two pages → both present)
- parameters are collected per attached group only, with `Source="user"`
- an instance class EC2 rejects is dropped from the `describe_instance_types` request and the other
  classes still come back
- access denied on `describe_db_instances` → `SCAN-ACCESS-DENIED` finding naming
  `rds:DescribeDBInstances`, and that file is `{}` while the others are still collected
- no `ListTagsForResource` call is made (assert on the stub)

**`tests/plugins/database/test_normalize.py`** — hand-built snapshots via `aws_builders.py`
- the testbed shape (public Postgres, `db.t3.micro`, 20 GiB, `publicly_accessible`) → exact expected
  `DatabaseInstance`
- private MySQL in a subnet group → `network`, `subnets` and `firewall_policies` resolved to IR ids,
  and the referenced `FirewallPolicy.applies_to` now contains the instance id
- a read replica → `role`, `replica_of` as an IR id; the same with the primary missing →
  `external:<identifier>`
- MultiAZ → `high_availability`; a Multi-AZ DB cluster member → `multi_az_cluster`
- parameters: user-set values kept including a formula string; defaults excluded
- vCPU/memory from `instance_types`; from the fallback table; neither → `None` plus
  `SCAN-DB-CLASS-UNKNOWN`
- Aurora, MariaDB and Oracle instances → three `unsupported` findings, zero IR objects
- an unresolvable VPC → `network=None` and a `scan_gap` finding, and the document still validates
- `status="creating"` → recorded plus a `DB-STATUS-TRANSIENT` info finding
- a subnet group spanning two VPCs → `ctx.unmodelled` (strict mode raises `UnmodelledData`)
- normalizing the same snapshot twice gives identical output (determinism)

**Fixture cases** (`tests/plugins/database/fixtures/<case>/snapshot/*.json` plus
`expected_ir.json`), the same pattern as the network fixtures. Plans 2 and 3 assert difference keys
and golden HCL against these exact case names, so create all nine here:
`public_postgres` (the testbed instance), `private_mysql_multiaz`, `read_replica`, `param_flags`
(one supported flag, one unsupported, one formula value), `unsupported_engines` (Aurora + Oracle),
`custom_port` (Postgres on 5433), `old_engine_version` (Postgres 9.6), `psa_range_conflict` (VPC
CIDRs at `10.64.0.0/16` so the default PSA allocation must move), `no_backups`
(`retention_days = 0`). Each fixture's snapshot includes the `network.*` files its VPC and security
groups need, so the whole pipeline can run against it. `test_golden.py` asserts
`Snapshot.from_dir` → `normalize` equals `expected_ir.json` for every case.

**`tests/plugins/database/test_plugin.py`**
- `DatabasePlugin` satisfies the `Plugin` protocol and its `generate_gcp` returns an empty `HclFile`
- `registry.PLUGINS` has `network` before `database`

## Tasks
1. `ir/database.py` and `tests/ir/test_database.py`.
2. `aws_values.py`, `collect_aws.py`, `tests/plugins/database/test_collect_aws.py` and the
   `aws_builders.py` helpers.
3. `normalize.py` and `test_normalize.py`.
3b. The nine fixture cases with their `expected_ir.json` and `test_golden.py`.
4. `DatabasePlugin`, the registry wiring, `test_plugin.py`, and a README line listing RDS as a
   scanned family.
