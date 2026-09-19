# EasyMigrate — Compute: EC2 → Compute Engine (Spec 4)

Status: draft 2026-09-18
Supersedes: nothing. Builds on `2026-09-15-core-pipeline-networking-design.md` (spec 1), whose
architecture, CLI stages, IR base types and difference-engine model this spec assumes, and on
`2026-09-16-databases-rds-cloudsql-design.md` (spec 2), whose `Decision.params` mechanism it reuses.
Independent of `2026-09-17-local-ui-design.md` (spec 3); the UI gains these resource kinds without
change, which is the tolerance spec 3 was built for.

## 1. Scope

Third family on the MVP roadmap, and the first compute family. Depth-first (decision D18): the
family is finished in full — collection, faithful IR, a complete difference catalogue with tested
strategies, generation, and a live testbed run — before EKS starts.

### In scope

- `aws_instance` — standalone EC2 instances, running or stopped.
- `aws_ebs_volume` — attached block storage, recreated empty.
- `aws_eip` — Elastic IPs, including unattached ones.
- `aws_launch_template` and `aws_autoscaling_group` — translated to instance templates and managed
  instance groups.
- The security-group → network-tag seam (§2.1), which finishes work spec 1 deliberately left open.
- The difference catalogue that bridges the two clouds (§5), and generation from the resolved IR (§6).

### Out of scope

- **Data.** Instances boot a stock image and disks are created empty. Nothing from the source disk
  is copied. This is the same boundary as spec 2 §1 and is the single most consequential limitation
  of this family — `VM-BOOT-IMAGE` (§5) exists to state it at high risk rather than imply otherwise.
- **AMIs.** No AMI is imported, converted or rebuilt. GCP image import is a separate product
  decision, not a difference.
- **IAM.** An instance profile is recorded and reported, never translated (`VM-IAM-PROFILE`,
  `manual`). Service accounts, roles and bindings remain roadmap-only.
- **Load balancers.** An ASG attached to a target group loses that attachment; MIG autohealing uses
  a standalone health check instead (`VM-ASG-HEALTHCHECK`). Named as a difference, not migrated.
- **EKS-managed instances.** Instances tagged as EKS node-group members are reported as belonging
  to a cluster and excluded from standalone translation, so spec 5 owns them without conflict.
- Spot Fleet, EC2 Fleet, Capacity Reservations, Savings Plans, Dedicated Hosts, Elastic Fabric
  Adapter, Nitro Enclaves, hibernation, and Systems Manager state.
- Everything spec 1 puts out of scope: other destination clouds, cutover and rollback.

## 2. Changes this family forces outside itself

Almost everything in this spec lives inside the new family. Three things do not: the two below, and
the `NET-EGRESS-TAG` narrowing they make possible (§10). All three are called out separately because
they touch shipped, live-tested families, and because later families will depend on them.

### 2.1 `FirewallPolicy.applies_to` is deleted; membership is read from forward references

Spec 1 left a seam. `FirewallPolicy.applies_to` is a `list[str]` of IR ids with the comment "filled
by later plugins", and the network generator's `FIELD_COVERAGE` records it as
`"not_generated: attaching the network tag to resources is a later plugin's job"`. Meanwhile the
generator already allocates a GCP network tag per policy and emits `target_tags = [own_tag]` on
every rule. The migrated firewall rules are correct today; nothing wears the tag they target.

The field is not unused. The database plugin writes it during normalize
(`plugins/database/normalize.py`), and `DB-SG-FIREWALL` reads it to find policies whose only
attachments are RDS instances. So the back-reference approach already ships.

It also already duplicates information. The same function that mutates `policy.applies_to` fills
`DatabaseInstance.network.firewall_policies`, which is a forward reference, is returned by
`DatabaseInstance.references()`, and is therefore validated by `IRDocument._check_integrity()`.
Every id in `applies_to` is derivable from the forward references of the objects that point at the
policy.

Keeping the back-reference costs three things. It is invisible to `references()`, so nothing
validates it. Filling it means a later plugin mutating an earlier plugin's objects, which makes
plugin order load-bearing (§2.2) and makes a network object's contents depend on which other
plugins ran. And it must be written by every future family that attaches to a security group —
each one a chance to forget.

**There is a latent bug in the current arrangement.** Only the database plugin writes `applies_to`,
so a security group guarding both an RDS instance and an EC2 instance contains only the database's
id, and `DB-SG-FIREWALL` reads it as RDS-only and drops it. That is harmless while EC2 is not
migrated and becomes live the moment this family lands — the policy guarding a migrated instance
would be silently removed, which principle 1 forbids.

This spec therefore:

- Adds `ComputeInstance.firewall_policies: list[PolicyRef]` and the same field on
  `InstanceTemplate`, both returned by `references()`.
- Deletes `FirewallPolicy.applies_to`, the database normalizer's mutation of it, and its
  `FIELD_COVERAGE` entry.
- Rewrites `DB-SG-FIREWALL` detection (§2.1.1).

#### 2.1.1 `DB-SG-FIREWALL` detection

Today: a policy is RDS-only when `applies_to` is non-empty and every entry starts with
`database_instance:`.

After this spec: a policy is RDS-only when at least one `database_instance` references it and no
`compute_instance` or `instance_template` does. Membership is computed once per `detect()` call by
walking forward references, which is the same information from a source that cannot drift.

The rule's strategies, risk, effect text and target hints are unchanged. Its existing tests keep
their expected output; new cases cover a policy shared between an RDS instance and a compute
instance, which must **not** be dropped.

### 2.2 Plugin ordering becomes declared rather than implied

`registry.PLUGINS` is an ordered tuple, and `pipeline.analyze()` accumulates `ctx.objects` after
each plugin so later plugins can `ctx.lookup()` earlier ones. With two plugins the dependency was
obvious and unstated. With three, and with compute referencing network objects during normalize, an
accidental reordering would fail as a `KeyError` inside a normalizer rather than as a clear error.

`Plugin` gains one field:

```python
class Plugin(Protocol):
    name: str
    requires: frozenset[str] = frozenset()
    aws_resource_types: frozenset[str]
```

`NetworkPlugin.requires` is empty; `DatabasePlugin.requires` and `ComputePlugin.requires` are
`{"network"}`. `analyze()` validates before running anything that each plugin's requirements appear
earlier in the sequence, raising `EasyMigrateError` naming both plugins if not. This validates the
existing order; it does not reorder, and it is not a dependency resolver.

`PLUGINS` becomes `(NetworkPlugin(), ComputePlugin(), DatabasePlugin())`. Compute and database are
independent of each other — neither declares the other in `requires`, and difference detection runs
after every normalizer, so `DB-SG-FIREWALL` sees compute instances regardless of position. Compute
is placed second only to keep the ordering readable; appending it would work identically.

## 3. Architecture

New module `plugins/compute/`, matching the existing family layout exactly:

```
plugins/compute/
    __init__.py          ComputePlugin: name, requires, aws_resource_types, three methods
    collect_aws.py       raw describe_* responses, one file per call
    normalize.py         snapshot -> IntentObject list
    aws_values.py        AWS-side lookup tables and tag/name helpers
    generate_gcp.py      resolved IR -> terraform/compute.tf

differences/rules/aws_gcp/
    compute.py           the VM-* rules
    gce_facts.py         GCP machine types, stock image families, disk types, local SSD sizes
```

New IR module `ir/compute.py`.

**GCP knowledge lives with the rules, not the plugin.** `tests/test_dependency_rules.py` allows
`easymigrate.differences` to import only `easymigrate.ir`, so a table the rules need cannot sit
under `plugins/`. This matches spec 2, where `cloudsql_facts.py` sits beside the database rules:
the rule chooses the GCP shape and records it in `target_hints`, and the generator reads the hint
rather than recomputing the choice. `gce_facts.py` follows that precedent, and like
`cloudsql_facts.py` it is model knowledge with a date, to be re-verified before customers.

No change to core, the pipeline stages, the CLI or the UI beyond §2.2.


## 4. Collection and IR

### 4.1 AWS calls

| Call | Snapshot file | Used for |
|---|---|---|
| `describe_instances` | `compute.instances.json` | instances, their volumes, SGs, network placement |
| `describe_instance_attribute` | `compute.instance_attributes.json` | `userData` and `disableApiTermination`, which `describe_instances` omits |
| `describe_volumes` | `compute.volumes.json` | EBS size, type, IOPS, throughput, encryption |
| `describe_addresses` | `compute.addresses.json` | Elastic IPs, attached and unattached |
| `describe_images` | `compute.images.json` | AMI platform, for stock-image selection |
| `describe_key_pairs` (`IncludePublicKey=True`) | `compute.key_pairs.json` | public key material where AWS still holds it |
| `describe_instance_types` | `compute.instance_types.json` | vCPU and memory for machine-type mapping |
| `describe_launch_templates`, `describe_launch_template_versions` | `compute.launch_templates.json` | template shapes |
| `describe_auto_scaling_groups` | `compute.autoscaling_groups.json` | ASG sizing, zones, health checks, policies |
| `describe_policies` (autoscaling) | `compute.scaling_policies.json` | target-tracking and step policies |

Each call is wrapped so `AccessDenied` produces a `scan_gap` finding naming the IAM action, via the
existing `CollectContext.access_denied` (spec 1 §2.1).

`describe_instance_types` is already called by the database plugin into
`database.instance_types.json`. Compute writes its own file rather than reading another plugin's
snapshot: plugins must stay independently runnable, and one duplicated read-only API call is a
smaller cost than a cross-plugin snapshot dependency.

`aws_resource_types` for the inventory sweep (spec 1 §2.3): `ec2:instance`, `ec2:volume`,
`ec2:elastic-ip`, `ec2:launch-template`, `autoscaling:autoScalingGroup`.

### 4.2 IR — five kinds

#### `compute_instance`

| Field | Built from | Notes |
|---|---|---|
| `instance_type` | `InstanceType` | e.g. `"m5.xlarge"` |
| `vcpus`, `memory_mib` | `describe_instance_types` | nullable; null forces `VM-MACHINE-TYPE` to `manual` |
| `architecture` | `Architecture` | `"x86_64"` \| `"arm64"` |
| `state` | `State.Name` | `"running"` \| `"stopped"`; other states are findings (§4.3) |
| `zone` | `Placement.AvailabilityZone` | AWS AZ name, verbatim |
| `zone_id` | `Placement.AvailabilityZoneId` | the account-independent id, e.g. `aps1-az1` |
| `subnet` | `SubnetId` → IR id | |
| `firewall_policies` | `SecurityGroups` → IR ids | §2.1 |
| `disks` | block device mappings → IR ids | boot disk first |
| `image` | `ImageId` | the AMI id, recorded; never translated |
| `platform` | `describe_images` | `"linux"` \| `"windows"`, plus the reported distribution where known |
| `external_ip` | `PublicIpAddress`, `NetworkInterfaces` | `"none"` \| `"ephemeral"` \| IR id of an `external_address` |
| `private_ip` | `PrivateIpAddress` | recorded; GCP assigns its own unless reserved |
| `user_data` | `describe_instance_attribute` | base64-decoded, verbatim |
| `key_pair` | `KeyName` | name only; material lives on `key_pair` lookup |
| `iam_profile` | `IamInstanceProfile.Arn` | recorded, never translated |
| `tenancy` | `Placement.Tenancy` | `"default"` \| `"dedicated"` \| `"host"` |
| `placement_group` | `Placement.GroupName` | |
| `spot` | `InstanceLifecycle` | true when `"spot"` |
| `termination_protection` | `DisableApiTermination` | |
| `monitoring` | `Monitoring.State` | |
| `imds` | `MetadataOptions` | `HttpTokens`, hop limit |
| `eks_cluster` | `tag:eks:cluster-name` | set means excluded from standalone translation (§1) |

#### `disk`

| Field | Built from | Notes |
|---|---|---|
| `size_gib` | `Size` | |
| `type` | `VolumeType` | `gp2`/`gp3`/`io1`/`io2`/`st1`/`sc1`/`standard` |
| `iops`, `throughput_mibps` | `Iops`, `Throughput` | null where not provisioned |
| `encrypted`, `kms_key` | `Encrypted`, `KmsKeyId` | |
| `zone` | `AvailabilityZone` | |
| `boot` | derived from the device name | matches the instance's `RootDeviceName` |
| `instance_store` | derived | ephemeral volumes, which are not EBS and have no `VolumeId` |

#### `external_address`

| Field | Built from | Notes |
|---|---|---|
| `address` | `PublicIp` | |
| `attached` | `InstanceId` or `NetworkInterfaceId` | false for an unattached EIP |
| `scope` | `Domain` | `"vpc"` \| `"standard"` |

#### `instance_template`

Every `compute_instance` field that describes shape rather than placement: `instance_type`, `vcpus`,
`memory_mib`, `architecture`, `image`, `platform`, `firewall_policies`, `disks`, `user_data`,
`key_pair`, `iam_profile`, `spot`, `imds`. No `zone`, `state`, `private_ip` or `external_ip`
identity. Carries `version` and `default_version` from the launch template.

#### `instance_group`

| Field | Built from | Notes |
|---|---|---|
| `template` | `LaunchTemplate` → IR id | the version the ASG references, not every version |
| `min_size`, `max_size`, `desired_capacity` | ASG | |
| `zones` | `AvailabilityZones` | one zone means a zonal MIG is possible |
| `subnets` | `VPCZoneIdentifier` → IR ids | |
| `health_check_type` | `HealthCheckType` | `"EC2"` \| `"ELB"` |
| `health_check_grace` | `HealthCheckGracePeriod` | |
| `target_groups` | `TargetGroupARNs` | recorded so the loss is nameable |
| `scaling_policies` | `describe_policies` | policy type, metric, target value |
| `mixed_instances` | `MixedInstancesPolicy` | present means more than one instance type |

IR ids follow spec 1 §3.1: the `Name` tag when present, otherwise the native id.

### 4.3 Normalization rules (faithful capture only)

- An instance in `pending`, `shutting-down`, `stopping` or `terminated` state produces an `info`
  finding and is not translated; the user may be looking at a half-built or half-destroyed
  environment. `running` and `stopped` are translated.
- An instance carrying `tag:eks:cluster-name` is captured in full and marked with `eks_cluster`, so
  the inventory is complete, but is excluded from generation with a reason (§1). Spec 5 will consume
  these objects rather than rediscover them.
- Instance-store volumes have no `VolumeId`; they are captured as `disk` objects with
  `instance_store = true` and no size where AWS does not report one.
- An instance with more than one network interface is captured with the primary interface's subnet
  and an `info` finding naming the others; multi-NIC translation is not attempted.
- A launch template version referenced by no ASG is captured but not translated, with an `info`
  finding — the inventory stays complete without generating machines nobody asked for.
- The normalizer emits no judgements about GCP. Every AWS/GCP gap is a `Difference` (§5).

### 4.4 Unsupported inventory

Spot Fleets, EC2 Fleets, Dedicated Hosts and Capacity Reservations each produce one `unsupported`
finding naming the resource and the reason. The tagging-API sweep already reports resource types no
plugin claims; these are called out specifically because a user with them will otherwise assume the
instances they govern migrated cleanly.

## 5. Difference catalogue (`VM-*`)

Subjects are IR object ids. Keys follow spec 1 §4.1: `<rule_id>@<subject id>`.

| Rule | Detects | Strategies (**default** first) |
|---|---|---|
| `VM-BOOT-IMAGE` | every instance and template | **`stock_image`** (high; params `image_family`, `image_project`): boot a current stock image chosen from the source platform — the machine runs, and nothing that was on the source disk is present. The report names the AMI that was not migrated · `manual`: generate nothing for this instance. Amazon Linux has no GCP equivalent and defaults to `rocky-linux-9` with that stated outright; Windows defaults to the matching `windows-cloud` family and carries its own licensing note |
| `VM-MACHINE-TYPE` | every instance and template | **`nearest_predefined`** (low when the memory delta is under 25%, medium otherwise): same vCPU count, smallest predefined type whose memory is greater than or equal to the source, family chosen from the source family class; the report names the exact delta · `shared_core` (medium): `e2-micro`/`e2-small`/`e2-medium`, chosen as the default instead when the source is a burstable `t`-family micro or small, which no full type expresses · `custom_machine_type` (medium): exact vCPU and memory via a custom type, at higher per-unit cost and without committed-use discounts · `manual` (the only strategy when `vcpus` or `memory_mib` is null) |
| `VM-ZONE-MAPPING` | every instance | **`positional`** (low): AWS AZ letters are per-account aliases over stable AZ ids, so `ap-south-1a` carries no meaning a GCP zone letter could preserve. Zones are assigned positionally by sorted `zone_id`, keeping instances that shared an AZ together and instances that were spread apart · `manual` |
| `VM-PUBLIC-IP` | `external_ip` is not `"none"` | **`private_only`** (medium): no external IP; every client must reach the instance from inside the VPC or through a bastion. Matches nothing in the source, and mirrors the `DB-PUBLIC-ACCESS` default adopted after the 2026-09-17 live run · `ephemeral_external` (high): the instance stays internet-reachable, governed only by the migrated firewall rules · `static_external` (high; requires an `external_address`): as above, with a reserved address |
| `VM-IAM-PROFILE` | `iam_profile` set | **`manual`** (only strategy): the attached role is recorded and reported; GCP service accounts have different semantics and translating permissions is out of scope. Requires acknowledgement before `plan` |
| `VM-SSH-KEYS` | `key_pair` set | **`import_public_key`** (low): AWS still holds the public key, which becomes an `ssh-keys` metadata entry · `os_login` (medium): enable OS Login and let GCP IAM govern access, ignoring the key pair — the GCP-native answer, and the one that changes the access model · `user_supplied_key` (low; params `public_key`) · `no_ssh_access` (medium). When AWS returns no public key material the first strategy is absent; EasyMigrate never generates a key pair |
| `VM-USER-DATA` | `user_data` non-empty | **`startup_script`** (medium): copied verbatim into `metadata.startup-script`, which runs on every boot rather than first boot only, and does not interpret `#cloud-config` · `cloud_init` (medium): written to the `user-data` metadata key, honoured only by images that ship cloud-init · `omit` (low): the script is reported in full and not carried |
| `VM-DISK-TYPE` | `io1`/`io2`, `st1`/`sc1`, or `gp3` with provisioned IOPS or throughput | **`nearest_pd`** (medium): `io1`/`io2` → `pd-ssd`, `st1`/`sc1` → `pd-standard`, `gp2`/`gp3` → `pd-balanced`; persistent-disk performance scales with size, so provisioned figures are lost and small disks may be slower than the source · `hyperdisk` (medium): preserves provisioned IOPS and throughput, at a different price and with zone availability limits · `manual` |
| `VM-DISK-EMPTY` | every non-boot `disk` | **`create_empty`** (high): a persistent disk of matching size and type is created and attached, containing nothing. Mirrors `VM-BOOT-IMAGE` for data volumes, at the same risk, because a mounted empty disk is a subtler failure than a missing one · `omit` (medium): the disk is not created and is reported |
| `VM-DISK-ENCRYPTION` | `encrypted` with a customer-managed KMS key | **`google_managed`** (low): still encrypted at rest, by Google-managed keys · `manual`: create a Cloud KMS key and set `disk_encryption_key` |
| `VM-INSTANCE-STORE` | any `instance_store` disk | **`local_ssd`** (medium): a local SSD of the nearest supported size, which is ephemeral in both clouds but has fixed sizes in GCP and requires a supporting machine type · `omit` (low) |
| `VM-STOPPED` | `state = "stopped"` | **`create_stopped`** (none): the instance is created with `desired_status = "TERMINATED"`, so a machine the customer deliberately stopped is not silently started and billed · `create_running` (medium) |
| `VM-SPOT` | `spot` | **`spot_vm`** (medium): `provisioning_model = "SPOT"` with `automatic_restart = false`; preemption behaviour differs from AWS Spot interruption notices · `standard` (low): a normal VM, which costs more and cannot be preempted |
| `VM-TENANCY` | `tenancy` is `dedicated` or `host` | **`manual`** (only strategy): GCP sole-tenant nodes require a node group and affinity labels, which this family does not generate |
| `VM-PLACEMENT-GROUP` | `placement_group` set | **`manual`** (only strategy): cluster, spread and partition placement map to GCP resource policies with different guarantees |
| `VM-IMDS` | `imds.HttpTokens = "required"` | **`note_only`** (none): GCP's metadata server always requires the `Metadata-Flavor` header and has no v1/v2 distinction; software that explicitly requests IMDSv2 tokens will need changing |
| `VM-TERMINATION-PROTECTION` | `termination_protection` | **`deletion_protection`** (none): exact equivalent |
| `VM-ELASTIC-IP-UNATTACHED` | `external_address` with `attached = false` | **`reserve_static`** (low): a reserved external address is created, matching the source, and is billed while unused just as in AWS · `omit` (none): reported, not created |
| `VM-ASG-SHAPE` | every `instance_group` | **`regional_mig`** (low): a regional managed instance group distributing across the mapped zones, which is the closest match to an ASG spanning AZs · `zonal_mig` (medium): a single zone, cheaper and with no cross-zone redundancy; the default instead when the source ASG has exactly one AZ |
| `VM-ASG-HEALTHCHECK` | every `instance_group` | **`autohealing_http`** (medium; params `port`, `request_path`): a standalone health check drives MIG autohealing. When `health_check_type = "ELB"` the source health check lived on a target group this family does not migrate, so the parameters are the user's to supply · `no_autohealing` (low): instances are recreated only on VM failure, not application failure — which is what `health_check_type = "EC2"` already meant · `manual` |
| `VM-ASG-SCALING` | `scaling_policies` non-empty | **`cpu_target`** (medium): a `google_compute_autoscaler` tracking CPU utilisation, when the source is a target-tracking policy on CPU · `fixed_size` (low): no autoscaler; the group stays at `desired_capacity` · `manual` (the only strategy for step scaling, scheduled actions and non-CPU target tracking, which have no single equivalent) |
| `VM-ASG-MIXED-INSTANCES` | `mixed_instances` set | **`manual`** (only strategy): an instance template declares one machine type; expressing a weighted mix of types and purchase options needs choices EasyMigrate cannot infer |
| `VM-ASG-TARGET-GROUPS` | `target_groups` non-empty | **`drop_with_note`** (high): the group's load-balancer attachment is not migrated, so migrated instances receive no traffic until a load balancer exists. Named at high risk because the ASG will look healthy while serving nothing |
| `VM-ASG-LIFECYCLE` | lifecycle hooks present | **`note_only`** (medium): MIG update policies have no pause-and-callback equivalent |

Every `manual` strategy needs explicit acknowledgement in `resolve`, as in spec 1.

**Rule interactions**, applied in this order:

- `VM-MACHINE-TYPE` runs after `VM-BOOT-IMAGE`, because a Windows stock image constrains the
  machine types available and shared-core types are not supported for Windows.
- `VM-PUBLIC-IP` runs before `VM-ELASTIC-IP-UNATTACHED`: an EIP the user chose to drop with
  `private_only` must not then be reserved as an unattached address.
- `VM-INSTANCE-STORE` runs after `VM-MACHINE-TYPE`, because local SSD requires a machine type that
  supports it; when the chosen type does not, the rule's only strategy is `omit`.
- `VM-ASG-SHAPE` runs before the other `VM-ASG-*` rules, because zonal and regional MIGs take
  different autohealing and autoscaler resources.
- `DB-SG-FIREWALL` (spec 2) now sees compute instances. A policy guarding both an RDS instance and
  a migrated compute instance is no longer RDS-only and is kept, so the instance keeps its firewall
  rules (§2.1.1).

The generator refuses with `GenerationError` rather than silently reconciling two invalid
combinations, following spec 2 §5: a `local_ssd` disk on a machine type that cannot carry one, and
`spot_vm` with `termination_protection`, which GCP rejects.

## 6. Generation

`plugins/compute/generate_gcp.py` reads only the resolved IR and its target hints, and declares
every field it touches in `FIELD_COVERAGE` (D19). Output file: `terraform/compute.tf`.

### 6.1 Resources

| IR | Terraform |
|---|---|
| `compute_instance` | `google_compute_instance` |
| `disk` (non-boot, EBS) | `google_compute_disk` + `attached_disk` block |
| `disk` (boot) | `boot_disk.initialize_params` on the instance |
| `disk` (instance store) | `scratch_disk` block |
| `external_address` | `google_compute_address` |
| `instance_template` | `google_compute_instance_template` |
| `instance_group` (regional) | `google_compute_region_instance_group_manager` |
| `instance_group` (zonal) | `google_compute_instance_group_manager` |
| `VM-ASG-HEALTHCHECK` | `google_compute_health_check` |
| `VM-ASG-SCALING` | `google_compute_autoscaler` / `google_compute_region_autoscaler` |

### 6.2 Network tags

An instance's `tags` attribute is the sorted set of GCP network tags belonging to the policies in
its `firewall_policies`. The tag is the one the network generator already allocates for that policy;
both sides read it from the same allocator rather than recomputing a name, so a rename on one side
cannot silently unlink the other. This is the point at which the `target_tags` the network family
has been emitting since spec 1 begin to match something.

An instance whose policies were all removed by another rule — `DB-SG-FIREWALL` drops policies that
only guarded RDS — emits no tags and an `info` finding, rather than an empty `tags` list that would
read as deliberate.

### 6.3 Naming

Reuses the existing allocator. GCP network tags and instance names must satisfy RFC 1035, which many
security-group and instance names do not; renames produce the existing `GEN-RENAMED` finding rather
than a new difference.

### 6.4 `plan` preflight

`compute.googleapis.com` is already required by the network family, so this family adds no new
service check. Instance templates referenced by a MIG are generated with `create_before_destroy`, as
GCP forbids deleting a template in use.

## 7. Report

Per-instance sections in the style of spec 2's Databases section, naming for each instance: the
machine type chosen and the delta from the source, the image it will boot and the AMI that was not
migrated, its network tags and the policies they came from, whether it is reachable from outside,
and each disk created empty. A closing paragraph states plainly that no disk contents, no installed
software and no running workload were migrated, so every instance boots as a blank machine of the
right shape on the right network.

## 8. Testing

- Unit tests per difference rule: detection, every strategy's effect on the resolved IR, param
  validation and rejection.
- Machine-type mapping tested against a table of real EC2 types spanning `t`, `m`, `c`, `r` and
  `arm64` families, including types with no clean predefined match.
- Golden HCL tests for each resource in §6.1.
- Provider-schema validation (`tofu`-marked, as in spec 2 §8) for instances, disks, templates, MIGs,
  autoscalers and health checks.
- Integrity tests that a `compute_instance` referencing a missing policy, subnet or disk fails to
  load, proving §2.1's claim.
- `DB-SG-FIREWALL` regression tests: existing RDS-only cases produce identical output from forward
  references, and a policy shared between an RDS instance and a compute instance is kept (§2.1.1).
- A test that `analyze` rejects a plugin sequence violating `requires` (§2.2).
- Testbed additions in `testbed/aws/main.tf`: a standalone instance with a key pair, user data, an
  extra EBS volume and an Elastic IP; a stopped instance; and a launch template with an ASG across
  two AZs. The existing EKS node group stays, and should appear in the scan as excluded rather than
  translated.
- A live run against the testbed, matching the bar set by spec 1 and spec 2: applied, verified in
  the GCP console, and its failures recorded here.

## 9. Done when

- `scan` collects all ten call groups, with a `scan_gap` finding for each denied permission.
- `analyze` produces the five IR kinds, and `applies_to` no longer exists anywhere in the codebase —
  including the database normalizer that writes it today.
- `DB-SG-FIREWALL` derives membership from forward references, and keeps a policy shared between an
  RDS instance and a compute instance.
- Every rule in §5 detects, resolves and generates, with acknowledgement enforced for `manual`.
- `plan` generates valid Terraform for a testbed containing a standalone instance, a stopped
  instance, an extra EBS volume, an Elastic IP and an ASG.
- `apply` creates them in GCP, and a migrated instance is reachable on a port its migrated firewall
  rules allow — proving the tag seam end to end.
- The report states the boot-image and empty-disk limitations without softening them.
- `NET-EGRESS-TAG` resolves to hosting subnet CIDRs where instances exist (§10), and its `effect`
  text no longer says `applies_to is not yet known`.

## 10. Change to the network family: `NET-EGRESS-TAG`

GCP egress rules cannot target network tags, only CIDR ranges. Spec 1's `peer_attached_ranges`
strategy therefore falls back to the peer policy's entire network CIDR blocks, and its `effect` text
admits why: `"applies_to is not yet known"`. That fallback is knowingly over-permissive — an egress
rule that meant "to the database tier" becomes "to the whole VPC".

With `ComputeInstance.firewall_policies` in place, the peer's membership is known at analyze time.
It is still not possible to resolve to instance IPs: GCP assigns internal addresses at apply time,
so no CIDR exists to write. The achievable narrowing is to **the union of the CIDRs of the subnets
hosting the peer policy's instances**, which is statically known and strictly tighter than the
network's full range whenever the peer's instances do not span every subnet.

`peer_attached_ranges` therefore resolves to, in order:

1. The union of `subnet.cidr` for every distinct subnet of every instance whose
   `firewall_policies` include the peer policy.
2. The peer policy's network CIDR blocks, unchanged from today, when the peer has no migrated
   instances — a policy guarding nothing, or guarding only resources this family excludes.

The strategy's `effect` text states which of the two applied, and the report names the ranges. Its
risk stays `medium`: narrower than before, still broader than a security group.

This is a behaviour change to a shipped rule with existing tests. Those tests gain cases for both
branches, and the existing no-instances case must keep its current output exactly.

## 11. Plans

1. **Compute IR, collection and normalization** — `ir/compute.py`, `plugins/compute/{collect_aws,
   normalize,aws_values,machine_types,images}.py`, the `requires` field and its validation (§2.2).
   No differences, no generation.
2. **The tag seam** — `ComputeInstance.firewall_policies`, deletion of `applies_to`, its coverage
   entry and the database normalizer's mutation of it, the `DB-SG-FIREWALL` rewrite (§2.1.1), and
   the `NET-EGRESS-TAG` narrowing (§10) with both branches tested. This plan changes two shipped
   families and is the highest-regression-risk step in the spec; it is second so the rest of the
   family is built on a settled seam.
3. **Instance generation** — `VM-BOOT-IMAGE`, `VM-MACHINE-TYPE`, `VM-ZONE-MAPPING`, `VM-PUBLIC-IP`,
   `VM-STOPPED`, `VM-DISK-*`, `VM-INSTANCE-STORE`, and `google_compute_instance` / `_disk`.
4. **Access and identity** — `VM-SSH-KEYS`, `VM-USER-DATA`, `VM-IAM-PROFILE`, `VM-SPOT`,
   `VM-TENANCY`, `VM-PLACEMENT-GROUP`, `VM-IMDS`, `VM-TERMINATION-PROTECTION`,
   `VM-ELASTIC-IP-UNATTACHED`, and `google_compute_address`.
5. **Scaling** — `instance_template` and `instance_group` generation, the five `VM-ASG-*` rules,
   MIGs, autoscalers and health checks, plus the live testbed run.

Plans 1–4 ship a coherent family on their own: if plan 5 slips, standalone compute still migrates.
