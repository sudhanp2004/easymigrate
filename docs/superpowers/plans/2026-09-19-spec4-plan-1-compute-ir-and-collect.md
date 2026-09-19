# Spec 4, Plan 1 of 5: Compute IR, Collection and Normalization

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `easymigrate scan` collects EC2, EBS, Elastic IPs, launch templates and Auto Scaling
Groups, and `easymigrate analyze` turns them into faithful `compute_instance`, `disk`,
`external_address`, `instance_template` and `instance_group` IR objects. Plugin ordering becomes
declared rather than implied. No GCP knowledge is added anywhere in this plan.

**Spec:** `docs/superpowers/specs/2026-09-18-compute-ec2-design.md` §2.2, §3, §4 (binding).

## Global Constraints
- `easymigrate.ir.*` imports only the stdlib and pydantic. Plugins import `ir`, `providers` and
  `core.plugin`, never `differences`. `tests/test_dependency_rules.py` enforces this; it needs no
  change.
- The normalizer is **faithful**: it records what AWS reports, including things GCP cannot do, and
  makes no translatability judgements. Every AWS/GCP gap is a `Difference` (plan 3), never a
  Finding. No machine-type mapping, no image selection, no zone mapping in this plan — those are
  rules in `differences/rules/aws_gcp/gce_facts.py`, built in plan 3.
- Deterministic output: objects sorted by id, lists sorted, no dependence on scan order.
- IR ids follow spec 1 §3.1 — the `Name` tag when present, otherwise the native identifier, slugged;
  same-named siblings get their slugged native id appended. Reuse `easymigrate.ir.naming`.
- Collectors never raise on access denied: they call `ctx.access_denied(api, iam_action)` and return
  what they got.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...` (uv may not be on PATH). Every task ends green:
  `uv run pytest`, `uv run ruff format --check .`, `uv run ruff check .`, then commit.

**Known transient, accepted for this plan only.** `ComputeInstance.firewall_policies` is added here,
but `FirewallPolicy.applies_to` is not removed until plan 2, and the compute normalizer must **not**
write to it. Between this plan and plan 2, a security group guarding both an RDS instance and an EC2
instance still reads as RDS-only to `DB-SG-FIREWALL` (spec §2.1). Nothing generates compute until
plan 3, so the only effect is on network generation for that shared-policy case. Plan 2 closes it
and must follow immediately.

## File Structure
```
src/easymigrate/ir/compute.py                   # five intent kinds + nested models
src/easymigrate/plugins/compute/
├── __init__.py                                 # ComputePlugin
├── collect_aws.py                              # collect(connection, ctx) -> dict[str, Any]
├── aws_values.py                               # AWS-side lookup tables and tag helpers
└── normalize.py                                # normalize(snapshot, ctx) -> list[IntentObject]
src/easymigrate/core/plugin.py                  # Plugin.requires
src/easymigrate/core/pipeline.py                # analyze() validates requires
src/easymigrate/core/registry.py                # PLUGINS += ComputePlugin()
tests/ir/test_compute.py
tests/plugins/compute/{__init__.py,aws_builders.py,test_collect_aws.py,test_normalize.py,test_golden.py,test_plugin.py}
tests/core/test_plugin_ordering.py
```

`generate_gcp.py` is plan 3. Until then `ComputePlugin.generate_gcp` returns an empty `HclFile`, so
the plugin protocol is satisfied and `plan` keeps working — the same approach spec 2 plan 1 took.

## Interfaces

### `ir/compute.py`
All models are `StrictModel`. Reuse `SubnetRef` and `PolicyRef` from `easymigrate.ir.network`;
define `DiskRef`, `AddressRef` and `TemplateRef` with the same `_ref(kind)` helper pattern.

```python
Architecture = Literal["x86_64", "arm64", "i386"]
InstanceState = Literal["running", "stopped"]
Tenancy = Literal["default", "dedicated", "host"]
Platform = Literal["linux", "windows"]
DiskType = Literal["gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"]
HealthCheckType = Literal["EC2", "ELB"]

class ImdsOptions(StrictModel):
    http_tokens: Literal["optional", "required"]
    hop_limit: int = Field(ge=1, le=64)

class ScalingPolicy(StrictModel):
    name: str
    policy_type: str                 # "TargetTrackingScaling", "StepScaling", "SimpleScaling"
    metric: str | None = None        # "ASGAverageCPUUtilization" etc., None for step policies
    target_value: float | None = None
```

Kinds and their fields are spec §4.2, verbatim. Beyond the table:

- `ComputeInstance.external_ip: str` is `"none"`, `"ephemeral"`, or an `external_address:` IR id.
  A validator rejects anything else.
- `ComputeInstance.disks: list[DiskRef]` is ordered, boot disk first. A validator rejects an empty
  list only when `instance_store` is not the sole storage — i.e. the list may be empty, and the
  boot-disk-first rule is enforced by the normalizer and asserted in its tests, not by the model
  (the model cannot see the referenced objects).
- `Disk.instance_store = True` requires `type is None` and no `iops`/`throughput_mibps`; a
  validator enforces this. `instance_store = False` requires `type` to be set.
- `iops` is accepted only for `io1`, `io2` and `gp3`; `throughput_mibps` only for `gp3`. Mirrors the
  `DbStorage` validators in `ir/database.py`.
- `InstanceGroup.min_size <= desired_capacity <= max_size`, enforced by a model validator.
- `references()`:
  - `ComputeInstance` → `subnet`, `firewall_policies`, `disks`, and `external_ip` when it is an IR id
  - `InstanceTemplate` → `firewall_policies`, `disks`
  - `InstanceGroup` → `template`, `subnets`
  - `Disk`, `ExternalAddress` → `[]`

### `plugins/compute/collect_aws.py`
`collect(connection, ctx: CollectContext) -> dict[str, Any]`

| File | Call | IAM action |
|---|---|---|
| `compute.instances` | `ec2.describe_instances` | `ec2:DescribeInstances` |
| `compute.instance_attributes` | `ec2.describe_instance_attribute` per instance, `Attribute` in `userData`, `disableApiTermination` | `ec2:DescribeInstanceAttribute` |
| `compute.volumes` | `ec2.describe_volumes` | `ec2:DescribeVolumes` |
| `compute.addresses` | `ec2.describe_addresses` | `ec2:DescribeAddresses` |
| `compute.images` | `ec2.describe_images` with the deduplicated `ImageId` list | `ec2:DescribeImages` |
| `compute.key_pairs` | `ec2.describe_key_pairs`, `IncludePublicKey=True` | `ec2:DescribeKeyPairs` |
| `compute.instance_types` | `ec2.describe_instance_types` | `ec2:DescribeInstanceTypes` |
| `compute.launch_templates` | `ec2.describe_launch_templates`, then `describe_launch_template_versions` per template | `ec2:DescribeLaunchTemplates`, `ec2:DescribeLaunchTemplateVersions` |
| `compute.autoscaling_groups` | `autoscaling.describe_auto_scaling_groups` | `autoscaling:DescribeAutoScalingGroups` |
| `compute.scaling_policies` | `autoscaling.describe_policies` | `autoscaling:DescribePolicies` |

Collection rules:
- `compute.instances` is flattened: the file is a list of instances, not a list of reservations.
  Instances in `terminated` state are kept in the file; `normalize` decides (spec §4.3).
- `compute.instance_attributes` is keyed by instance id:
  `{"i-abc": {"userData": "<base64 or null>", "disableApiTermination": bool}}`. `userData` stays
  base64 here; decoding is the normalizer's job.
- `compute.images` is one call with the deduplicated, sorted `ImageId` values. An AMI that is
  deregistered or owned by another account returns `InvalidAMIID.NotFound` — drop it from the
  request and it is simply absent from the file. No exception, no finding here.
- `compute.instance_types` is one call with the deduplicated, sorted `InstanceType` values, same
  `InvalidInstanceType` handling as the database collector.
- `compute.launch_templates` is `{"<template-id>": {"template": {...}, "versions": [...]}}`.
  All versions are collected; `normalize` keeps only the one an ASG references.
- Tags come from each `describe_*` response's `Tags`; no `ec2:DescribeTags` call.
- Access denied on any call → `ctx.access_denied(...)` and that file is `{}` (or `[]` where the
  shape is a list).
- `AwsConnection.client(service)` is generic, so `connection.client("autoscaling")` works with no
  change to `providers/aws/source.py`.

### `plugins/compute/aws_values.py`
- `FALLBACK_INSTANCE_TYPES: dict[str, tuple[int, int]]` — instance type → (vCPUs, memory MiB) for
  types `describe_instance_types` does not return. Seed with `t2.micro`, `t3.micro`, `t3.small`,
  `t3.medium`, `t3.large`, `t4g.small`, `t4g.medium`, `m5.large`, `m5.xlarge`, `m6i.large`,
  `c5.large`, `c5.xlarge`, `r5.large`, `r5.xlarge`. Each entry carries a comment with the
  AWS-documented figures it came from.
- `EKS_CLUSTER_TAG = "eks:cluster-name"`.
- `TRANSIENT_STATES = frozenset({"pending", "shutting-down", "stopping", "terminated"})`.
- `tags(resource)` and `name_tag(resource)`, matching `plugins/network/aws_values.py`.

### `plugins/compute/normalize.py`
`normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]`

Mapping is spec §4.2, verbatim. Beyond it:
- Subnets and security groups resolve to IR ids by scanning `ctx.objects` for `Subnet` /
  `FirewallPolicy` objects whose `source.id` matches the native id — the same lookup the database
  normalizer does. An unresolvable subnet drops the instance with one `scan_gap` finding naming the
  subnet id; an unresolvable security group is omitted with one `scan_gap` finding. **Do not write
  to `FirewallPolicy.applies_to`.**
- `disks`: block device mappings give `(device_name, VolumeId)`; the volume's details come from
  `compute.volumes`. The disk whose device name equals the instance's `RootDeviceName` is `boot`
  and is placed first; the rest follow sorted by device name. A volume attached to two instances
  (multi-attach) is captured once and referenced by both, with one `info` finding.
- Instance-store volumes appear in block device mappings without a `VolumeId`; they become `disk`
  objects with `instance_store=True`, `type=None` and `size_gib=None`.
- `vcpus` / `memory_mib`: `compute.instance_types` first, then `FALLBACK_INSTANCE_TYPES`, then
  `None` plus one `scan_gap` finding `SCAN-VM-TYPE-UNKNOWN` naming the type.
- `user_data`: base64-decoded as UTF-8; on a decode error, recorded as `None` plus one `info`
  finding `VM-USER-DATA-BINARY` naming the instance.
- `key_material`: from `compute.key_pairs`, the `PublicKey` for the instance's `KeyName` when AWS
  returned one; otherwise `None`. EasyMigrate never generates key material.
- `external_ip`: an `external_address` IR id when an Elastic IP in `compute.addresses` is associated
  with this instance; `"ephemeral"` when the instance has a `PublicIpAddress` but no Elastic IP;
  `"none"` otherwise.
- `platform`: `"windows"` when the instance or its AMI reports a Windows platform, else `"linux"`.
  `platform_details` carries the AMI's reported string verbatim, or `None` when the AMI is absent
  from `compute.images`.
- **Transient states:** an instance whose state is in `TRANSIENT_STATES` produces one `info` finding
  `VM-STATE-TRANSIENT` naming the state and becomes no IR object.
- **EKS members:** an instance carrying `EKS_CLUSTER_TAG` becomes a full `compute_instance` with
  `eks_cluster` set, plus one `info` finding `VM-EKS-MEMBER` naming the cluster. It is still an IR
  object — generation excludes it in plan 3.
- **Multi-NIC:** an instance with more than one network interface uses the primary interface's
  subnet and emits one `info` finding `VM-MULTI-NIC` naming the other interface ids.
- `instance_template`: only versions referenced by an ASG become IR objects. Every other version
  produces one `info` finding `VM-LT-VERSION-UNUSED` naming template and version.
- `instance_group`: `scaling_policies` are matched to the ASG by `AutoScalingGroupName`.
- **Unsupported inventory:** Spot Fleet requests, EC2 Fleets, Dedicated Hosts and Capacity
  Reservations each produce one `unsupported` finding, code `VM-RESOURCE-UNSUPPORTED`, naming the
  resource id and the reason (spec §4.4). They become no IR object.
- The normalizer emits no judgements about GCP.

### `plugins/compute/__init__.py`
```python
class ComputePlugin:
    name = "compute"
    requires = frozenset({"network"})
    aws_resource_types = frozenset({
        "ec2:instance", "ec2:volume", "ec2:elastic-ip",
        "ec2:launch-template", "autoscaling:autoScalingGroup",
    })
```
with the three protocol methods delegating to the modules above.

### `core/plugin.py`
`Plugin` gains `requires: frozenset[str]` (spec §2.2). `NetworkPlugin.requires = frozenset()`;
`DatabasePlugin.requires = frozenset({"network"})`.

### `core/pipeline.py`
`analyze()` validates the plugin sequence **before running any plugin**: for each plugin, every name
in `requires` must belong to a plugin earlier in the sequence. On violation raise
`EasyMigrateError` naming the plugin, the missing requirement, and whether it is absent or merely
later. Add a module-level helper so the check is testable without running a scan.

### `core/registry.py`
`PLUGINS = (NetworkPlugin(), ComputePlugin(), DatabasePlugin())`.

## Required test cases (minimum)

**`tests/ir/test_compute.py`**
- a minimal valid `compute_instance` round-trips through `IRDocument.model_validate_json`
- `references()` returns subnet, policies and disks; includes `external_ip` when it is an IR id and
  omits it for `"none"` and `"ephemeral"`
- `external_ip = "public"` raises; `external_ip = "external_address:eip-1"` is accepted
- `instance_store=True` with a `type` raises; with `iops` raises; `instance_store=False` without a
  `type` raises
- `iops` on `gp2` raises; on `gp3` and `io1` is accepted; `throughput_mibps` on `io1` raises
- `InstanceGroup` with `desired_capacity` above `max_size` raises; with `min_size` above
  `desired_capacity` raises
- `ImdsOptions.hop_limit = 0` raises
- an unknown extra field raises (StrictModel)
- an `IRDocument` containing a `compute_instance` whose `subnet`, a policy, or a disk is missing
  fails `_check_integrity` — this is the claim spec §2.1 makes for forward references

**`tests/plugins/compute/test_collect_aws.py`** (moto, plus a stub connection where moto is thin)
- every file is produced with the expected key, and pagination is followed (two pages → both present)
- `compute.instances` is flat, not reservation-shaped
- `describe_instance_attribute` is called for both attributes per instance, and the file is keyed by
  instance id with `userData` still base64
- an AMI that returns `InvalidAMIID.NotFound` is dropped from the request and the other AMIs still
  come back
- an instance type EC2 rejects is dropped from the `describe_instance_types` request and the others
  still come back
- `describe_key_pairs` is called with `IncludePublicKey=True`
- launch template versions are collected per template and keyed by template id
- access denied on `describe_instances` → `SCAN-ACCESS-DENIED` finding naming `ec2:DescribeInstances`,
  that file empty, and the other files still collected
- access denied on `autoscaling:DescribePolicies` alone leaves the ASG file populated

**`tests/plugins/compute/test_normalize.py`** — hand-built snapshots via `aws_builders.py`
- a standalone Linux instance with one boot volume → exact expected `ComputeInstance` and `Disk`,
  with subnet and policies resolved to IR ids
- **the referenced `FirewallPolicy.applies_to` is still empty** (guards the plan-1 constraint)
- boot disk is first in `disks`; extra volumes follow sorted by device name
- an instance-store volume → `instance_store=True`, `type=None`, `size_gib=None`
- a stopped instance → `state="stopped"` and an IR object; a `terminated` one → no object plus
  `VM-STATE-TRANSIENT`
- an Elastic IP attached to an instance → `external_ip` is the `external_address` IR id; an
  unattached EIP → its own object with `attached=False`; a public IP with no EIP → `"ephemeral"`
- vCPU/memory from `instance_types`; from the fallback table; neither → `None` plus
  `SCAN-VM-TYPE-UNKNOWN`
- user data base64-decodes; invalid UTF-8 → `None` plus `VM-USER-DATA-BINARY`
- a key pair with public key material → `key_material` set; without → `None`
- an EKS-tagged instance → IR object with `eks_cluster` set plus `VM-EKS-MEMBER`
- a two-NIC instance → primary subnet used plus `VM-MULTI-NIC`
- an unresolvable subnet → instance dropped plus a `scan_gap` finding; an unresolvable security
  group → omitted from `firewall_policies` plus a `scan_gap` finding
- an ASG with a launch template → `instance_group` and one `instance_template` for the referenced
  version only; the unused version produces `VM-LT-VERSION-UNUSED`
- an ASG with a target-tracking CPU policy → `scaling_policies` populated with metric and target
- a Dedicated Host → one `unsupported` finding, zero IR objects
- normalizing the same snapshot twice gives identical output (determinism)

**Fixture cases** (`tests/plugins/compute/fixtures/<case>/snapshot/*.json` plus `expected_ir.json`),
the same pattern as the network and database fixtures. Plans 3–5 assert difference keys and golden
HCL against these exact case names, so create all eight here: `standalone_linux` (the testbed
instance: one boot volume, a key pair, user data, an Elastic IP), `stopped_instance`,
`extra_volumes` (gp3 with provisioned IOPS plus an instance-store volume), `windows_instance`,
`spot_instance`, `eks_member`, `asg_two_az` (launch template, ASG across two AZs, CPU target
tracking), `unsupported_resources` (a Dedicated Host and a Spot Fleet request). Each fixture's
snapshot includes the `network.*` files its VPC, subnets and security groups need, so the whole
pipeline can run against it. `test_golden.py` asserts `Snapshot.from_dir` → `normalize` equals
`expected_ir.json` for every case.

**`tests/plugins/compute/test_plugin.py`**
- `ComputePlugin` satisfies the `Plugin` protocol and its `generate_gcp` returns an empty `HclFile`
- `aws_resource_types` covers every type the collector reads, so the inventory sweep does not report
  a resource this family migrates

**`tests/core/test_plugin_ordering.py`**
- `registry.PLUGINS` satisfies every `requires`
- a sequence with compute before network raises `EasyMigrateError` naming both plugins
- a sequence missing `network` entirely raises, with a message distinguishing absent from later
- `analyze()` raises before calling any plugin's `normalize` (assert with a spy plugin)

## Tasks
1. `ir/compute.py` and `tests/ir/test_compute.py`.
2. `core/plugin.py` `requires`, the `analyze()` validation, `registry` wiring for the two existing
   plugins, and `tests/core/test_plugin_ordering.py`. `ComputePlugin` does not exist yet, so the
   ordering tests use stub plugins.
3. `aws_values.py`, `collect_aws.py`, `tests/plugins/compute/test_collect_aws.py` and the
   `aws_builders.py` helpers.
4. `normalize.py` and `test_normalize.py`.
5. The eight fixture cases with their `expected_ir.json` and `test_golden.py`.
6. `ComputePlugin`, the registry entry, `test_plugin.py`, and a README line listing EC2 as a scanned
   family.
