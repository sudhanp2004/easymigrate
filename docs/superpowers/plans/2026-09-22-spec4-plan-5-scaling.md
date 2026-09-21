# Spec 4, Plan 5 of 5: Scaling

> Lean plan: interfaces, rules and required test cases. No pre-written code; implement once, with TDD.

**Goal:** a launch template plus an Auto Scaling group becomes an instance template, a managed
instance group, a health check and an autoscaler, after six `VM-ASG-*` rules have decided shape,
autohealing, scaling and the gaps that cannot be bridged. Adds the testbed resources for the live
run.

**Spec:** `docs/superpowers/specs/2026-09-18-compute-ec2-design.md` §4.2, §5, §6.1, §6.4, §8.

## Global Constraints
Same as plans 3–4.

## Prerequisites the earlier plans left out (found while planning)
- **Collection:** `describe_images` and `describe_instance_types` are called only for running
  instances' AMIs and types. Launch-template versions' `ImageId`/`InstanceType` must be included,
  or every template has no sizing (→ `VM-MACHINE-TYPE` manual) and no platform, and no group can
  generate. Collect launch templates first, then images and types for the union.
- **Collection:** new `compute.lifecycle_hooks.json` = `{group name: [hooks]}` from
  `describe_lifecycle_hooks` per group (`autoscaling:DescribeLifecycleHooks`; denied → scan gap).
- **IR:** `InstanceGroup.lifecycle_hooks: list[str]` (hook names; default empty, so saved state
  still loads).
- **Normalize templates fully:** `disks` from `BlockDeviceMappings` (the boot disk is the mapping on
  the image's `RootDeviceName`; when the template omits it, it comes from the image's own root
  mapping, so every template has a boot disk), plus `key_material`, `iam_profile`
  (`IamInstanceProfile.Arn` or `.Name`), `spot` (`InstanceMarketOptions.MarketType == "spot"`) and
  `imds`. Template disks have `zone = ""` — they belong to no zone until a MIG places an instance.
  With disks present, plan 3–4's shape rules (`VM-BOOT-DISK-MINIMUM`, `VM-DISK-*`, `VM-SSH-KEYS`,
  `VM-USER-DATA`, `VM-SPOT`, `VM-IAM-PROFILE`, `VM-IMDS`) apply to templates unchanged.
- **`VM-ZONE-MAPPING`** includes group zones in its positional letter assignment (a group AZ with no
  instance carrying its `zone_id` sorts by name), so an instance and a group sharing an AZ share a
  GCP zone.

## Rules (run after every plan 3–4 rule; `VM-ASG-SHAPE` first)

| Rule | Detects | Strategies (default first) → hint |
|---|---|---|
| `VM-ASG-SHAPE` | every group | `regional_mig` (low) · `zonal_mig` (medium; **default when the group has one AZ**). Hints: `mig = "regional"\|"zonal"`, `zone_letters` (all of the group's letters, or the first), `subnet` (first sorted subnet — a GCP subnet is regional, so one serves every zone; effect text names it). If the template was excluded by an earlier rule, the group is excluded too, with that reason. Effect states MIG instances get no external IP. |
| `VM-ASG-HEALTHCHECK` | every group | `autohealing_http` (medium; params `port` 1–65535, `request_path` starting `/`, defaults 80 and `/`) → `health_check = {port, request_path}`, `initial_delay_sec` = grace or 300 · `no_autohealing` (low) · `manual` (acknowledge only). **Default:** `autohealing_http` for `ELB`, `no_autohealing` for `EC2` — the spec's own text says EC2 checks already meant "recreate on VM failure only", which is what `no_autohealing` does; defaulting EC2 groups to an invented HTTP port would break healthy groups. |
| `VM-ASG-SCALING` | `scaling_policies` non-empty | `cpu_target` (medium) → `autoscaler = {"cpu_target": target/100}` — offered only for exactly one `TargetTrackingScaling` policy on `ASGAverageCPUUtilization` and `max_size ≥ 1` · `fixed_size` (low) · `manual` (acknowledge only; the only strategy otherwise) |
| `VM-ASG-MIXED-INSTANCES` | `mixed_instances` | `manual` only, acknowledge: the group uses the template's single machine type |
| `VM-ASG-TARGET-GROUPS` | `target_groups` non-empty | `drop_with_note` (high) |
| `VM-ASG-LIFECYCLE` | `lifecycle_hooks` non-empty | `note_only` (medium) |

## Generation
- `google_compute_instance_template` per live template referenced by a live group: `name_prefix`
  (templates are immutable, so `lifecycle { create_before_destroy = true }` needs a fresh name,
  spec §6.4), `machine_type`, a `disk` block per disk (boot: `source_image`, `boot = true`; data:
  empty, `disk_size_gb`, `disk_type`; all `auto_delete = true`), `network_interface.subnetwork` from
  the referencing group's `subnet` hint, `tags`, `labels`, `metadata`, `scheduling` when spot.
  `GenerationError` when two groups sharing a template chose different subnets.
- Regional group → `google_compute_region_instance_group_manager` (`region`,
  `distribution_policy_zones`); zonal → `google_compute_instance_group_manager` (`zone`). Both:
  `base_instance_name`, `version.instance_template`, `target_size` = desired capacity (omitted when
  an autoscaler owns it), `auto_healing_policies` when a health check exists.
- `google_compute_health_check` (`http_health_check { port, request_path }`) per group with the hint.
- `google_compute_region_autoscaler` / `google_compute_autoscaler` per group with the hint:
  `min_replicas`, `max_replicas`, `cooldown_period` (grace or 60), `cpu_utilization.target`.
- `FIELD_COVERAGE` for template and group replaces the placeholders.

## Required tests (minimum)
- Collection: template AMIs/types reach `describe_images`/`describe_instance_types`; lifecycle hooks
  collected per group; denied hooks call → scan gap.
- Normalize: template disks (boot from the template mapping, and from the image when omitted),
  spot, imds, iam, key material, lifecycle hook names.
- Zone mapping shares a letter between an instance and a group in the same AZ.
- Each rule: detection, strategies, defaults (one-AZ → zonal; EC2 → no_autohealing; step scaling →
  manual only), params, excluded template → excluded group, ordering.
- Generator: template (boot + data disk, subnet, create_before_destroy), regional and zonal MIG,
  health check + autohealing, autoscaler and no `target_size` with it, subnet conflict error,
  coverage. Golden `expected_compute.tf` for `asg_two_az`; tofu schema test includes it.

## Testbed (spec §8)
Add to `testbed/aws/main.tf`: a key pair (public key from a variable) and user data on the `app`
instance; a stopped instance; a launch template and a two-AZ ASG (min 1, desired 1, max 2) with a
CPU target-tracking policy. EKS stays and must scan as excluded. Validated with `terraform validate`
only; the live apply is a separate, user-approved step.

## Tasks
1. Collection + IR + template normalization + zone-mapping change, with tests and fixture updates.
2. Six `VM-ASG-*` rules, tests, registration, differences doc.
3. Generator: templates, MIGs, health checks, autoscalers; coverage; goldens; tofu test.
4. Testbed additions.
