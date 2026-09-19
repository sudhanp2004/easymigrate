# Spec 4, Plan 3 of 5: Instance Generation

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `easymigrate plan` emits `google_compute_instance` and `google_compute_disk` for a scanned
EC2 environment, after nine `VM-*` difference rules have decided machine type, boot image, zone,
external reachability, run state and disk shape. This is the first plan in spec 4 that produces
Terraform.

**Spec:** `docs/superpowers/specs/2026-09-18-compute-ec2-design.md` §5 (the nine rules below), §6.1,
§6.2, §6.3 (binding).

## Global Constraints
- `easymigrate.differences` imports only `easymigrate.ir`. GCP facts therefore live in
  `differences/rules/aws_gcp/gce_facts.py`, beside the rules, exactly as `cloudsql_facts.py` does.
  `tests/test_dependency_rules.py` enforces this.
- **Rules decide, generation renders.** Every GCP-shaped choice is written into `target_hints` by a
  rule; `generate_gcp.py` reads hints and never recomputes a choice. Anything the generator meets
  that it cannot represent means a rule failed to run, and it raises `GenerationError` — the same
  contract spec 2 §6 established.
- `gce_facts.py` is model knowledge dated 2026-09-19, like `cloudsql_facts.py`. Say so in its
  docstring and add it to the "re-verify before customers" list.
- Generation skips any object with `excluded` set, and declares every field it touches in
  `FIELD_COVERAGE`. Replace the placeholder table added in plan 2 as fields become real.
- Deterministic output: objects sorted by IR id, lists sorted.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/differences/rules/aws_gcp/gce_facts.py   # NEW: GCP machine types, images, disks
src/easymigrate/differences/rules/aws_gcp/compute.py     # NEW: the nine VM-* rules
src/easymigrate/differences/rules/aws_gcp/__init__.py    # register them in AWS_GCP_RULES
src/easymigrate/plugins/compute/generate_gcp.py          # replace the plan-2 placeholder
tests/differences/test_compute_rules.py                  # NEW
tests/differences/test_gce_facts.py                      # NEW
tests/plugins/compute/test_generate_gcp.py               # NEW
tests/plugins/compute/fixtures/<case>/expected_compute.tf # golden HCL per fixture
```

## Interfaces

### `gce_facts.py`

```python
FACTS_AS_OF = "2026-09-19"

PREDEFINED: tuple[MachineType, ...]     # name, family, vcpus, memory_mib
SHARED_CORE: tuple[MachineType, ...]    # e2-micro, e2-small, e2-medium
AWS_FAMILY_TO_GCP: dict[str, str]       # "t"->"e2", "m"->"n2", "c"->"c2", "r"->"n2-highmem", …
IMAGE_FAMILIES: dict[str, tuple[str, str]]   # platform key -> (image project, image family)
DISK_TYPE: dict[str, str]               # gp2/gp3->pd-balanced, io1/io2->pd-ssd, st1/sc1->pd-standard
LOCAL_SSD_GIB = 375                     # GCP local SSD comes in fixed 375 GiB units
MIN_DISK_GIB = 10
```

`nearest_predefined(vcpus, memory_mib, instance_type) -> MachineType | None` — same vCPU count and
the smallest memory greater than or equal to the source, within the family chosen from the source's
family letter; falls back to any family when that one has no fit; `None` when nothing fits.

`image_for(platform, platform_details) -> tuple[str, str]` — Amazon Linux has no GCP equivalent and
maps to `rocky-linux-cloud/rocky-linux-9`, which the rule states outright rather than implying
equivalence. Windows maps to `windows-cloud/windows-2022`. Ubuntu, Debian, RHEL and SUSE map to
their own families.

### `compute.py` — nine rules

Ids, detection, strategies, risks and defaults are spec §5, verbatim. What each writes:

| Rule | Subject | Target hint written |
|---|---|---|
| `VM-BOOT-IMAGE` | instance, template | `boot_image = {"project": …, "family": …}` (params `image_family`, `image_project` override) |
| `VM-MACHINE-TYPE` | instance, template | `machine_type = "e2-medium"`, plus `machine_type_delta_mib` for the report |
| `VM-ZONE-MAPPING` | instance | `zone_letter = "a"` — **a letter, not a zone**: the GCP region is unknown at resolve time, so generation joins it to `ctx.region` |
| `VM-PUBLIC-IP` | instance | `external = "none" \| "ephemeral" \| "static"` |
| `VM-STOPPED` | instance | `desired_status = "TERMINATED"` |
| `VM-DISK-TYPE` | disk | `disk_type = "pd-ssd"` |
| `VM-DISK-EMPTY` | disk (non-boot) | `create_empty = True`, or `excluded` on `omit` |
| `VM-DISK-ENCRYPTION` | disk | nothing on `google_managed`; `manual` excludes nothing and generates as normal |
| `VM-INSTANCE-STORE` | disk (instance store) | `local_ssd_count = n`, or `excluded` on `omit` |

Rule interactions, in this order (spec §5): `VM-BOOT-IMAGE` before `VM-MACHINE-TYPE` (a Windows
image rules out shared-core types); `VM-INSTANCE-STORE` after `VM-MACHINE-TYPE` (local SSD needs a
type that supports it — when the chosen type does not, `omit` is the only strategy).

**`VM-ZONE-MAPPING` is positional.** AWS AZ letters are per-account aliases over stable AZ ids, so
`ap-south-1a` carries no meaning a GCP zone letter preserves. Sort the distinct `zone_id`s in the
document and assign `a`, `b`, `c` in that order, so instances that shared an AZ stay together and
instances that were spread stay spread. Instances with no `zone_id` sort last by `zone`.

### `generate_gcp.py`

Output file `terraform/compute.tf`. Reads only resolved IR and hints.

- **`google_compute_instance`** — `name`, `machine_type` (hint), `zone` = `f"{ctx.region}-{letter}"`,
  `boot_disk.initialize_params` (image from hint, size and type from the boot disk),
  `attached_disk` per non-boot EBS disk, `scratch_disk` per local SSD unit,
  `network_interface` with `subnetwork` and an `access_config` only when `external != "none"`,
  `tags` from `firewall_policies` (§6.2), `labels`, `desired_status` when stopped,
  `deletion_protection`, and `scheduling` when spot.
- **`google_compute_disk`** — one per non-boot, non-instance-store disk that was not excluded.
- **Network tags** come from the *same* allocator entry the network generator used for that policy,
  so a rename on one side cannot unlink the other. An instance whose policies were all removed
  emits no `tags` and one `info` finding, rather than an empty list that would read as deliberate.
- **`GenerationError`** when: an instance has no `machine_type` hint, no `boot_image` hint, no
  `zone_letter` hint, a boot disk that is missing or excluded, or a `local_ssd_count` on a machine
  type that cannot carry one.

The network generator already emits `target_tags` per policy; this is the plan where those tags
finally match a resource (spec §6.2).

## Required test cases (minimum)

**`tests/differences/test_gce_facts.py`**
- `nearest_predefined` maps `m5.xlarge` (4 vCPU, 16 GiB) to `n2-standard-4` exactly, delta 0
- `c5.xlarge` (4 vCPU, 8 GiB) picks a type with memory ≥ 8 GiB and reports the delta
- a burstable `t3.micro` maps to a shared-core type
- an unknown instance type with no sizing returns `None`
- `image_for` maps Amazon Linux to Rocky, Windows to `windows-cloud`, Ubuntu to Ubuntu
- every `PREDEFINED` entry has vCPU and memory consistent with its name (guards typos)

**`tests/differences/test_compute_rules.py`** — per rule: detection fires only where spec says,
every strategy's effect on the resolved IR, param validation, and risk. Specifically:
- `VM-BOOT-IMAGE` is **high** risk and its effect text says the disk contents did not come
- `VM-MACHINE-TYPE` risk is low under a 25% memory delta and medium at or above it
- `VM-ZONE-MAPPING` assigns `a`/`b` positionally by sorted `zone_id`, and two instances sharing an
  AZ id get the same letter
- `VM-PUBLIC-IP` defaults to `private_only` for both an ephemeral and an Elastic IP instance
- `VM-STOPPED` fires only for `state="stopped"` and its default is risk `none`
- `VM-DISK-EMPTY` does not fire for a boot disk (that is `VM-BOOT-IMAGE`'s subject)
- `VM-INSTANCE-STORE` converts to whole 375 GiB units and rounds up
- the two ordering interactions above hold in `AWS_GCP_RULES`

**`tests/plugins/compute/test_generate_gcp.py`**
- a minimal instance renders `google_compute_instance` with machine type, zone, boot disk and
  subnetwork
- `tags` equal the network generator's tag for the same policy — assert against the network
  generator's own output, not a hard-coded string, so a rename on either side fails the test
- an instance with no surviving policies emits no `tags` and one `info` finding
- `external = "none"` emits no `access_config`; `"ephemeral"` emits an empty one
- a stopped instance emits `desired_status = "TERMINATED"`
- a non-boot disk renders `google_compute_disk` plus an `attached_disk` on the instance
- an excluded disk renders neither
- instance-store renders `scratch_disk`, not `google_compute_disk`
- missing `machine_type`, `boot_image` or `zone_letter` hint each raise `GenerationError`
- `FIELD_COVERAGE` declares every field on every compute kind (the existing coverage test pattern)

**Golden HCL** — `expected_compute.tf` for each of the eight plan-1 fixtures, asserted the way the
database family asserts `expected_database.tf`. Add the compute generator to the provider-schema
(`tofu`-marked) validation test so the emitted HCL is checked against Google's real schema.

## Tasks
1. `gce_facts.py` and its tests.
2. `VM-MACHINE-TYPE`, `VM-BOOT-IMAGE`, `VM-ZONE-MAPPING` and their tests; register in
   `AWS_GCP_RULES` with the declared ordering.
3. `VM-PUBLIC-IP`, `VM-STOPPED`, and the four disk rules, with their tests.
4. `generate_gcp.py`: instances and disks, `FIELD_COVERAGE`, and the `GenerationError` guards.
5. Golden `expected_compute.tf` for all eight fixtures, plus provider-schema validation.
