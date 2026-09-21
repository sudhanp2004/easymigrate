# Spec 4, Plan 4 of 5: Access and Identity

> Lean plan: interfaces, rules and required test cases. No pre-written code; implement once, with TDD.

**Goal:** the nine access/identity `VM-*` rules decide SSH access, boot scripts, identity, spot,
tenancy, placement, IMDS, deletion protection and unattached Elastic IPs, and the generator renders
them — `metadata`, `scheduling`, `deletion_protection` and `google_compute_address`. After this plan
a standalone EC2 instance migrates completely (spec §11: plans 1–4 are a coherent family), and the
report carries the per-instance Compute section from spec §7.

**Spec:** `docs/superpowers/specs/2026-09-18-compute-ec2-design.md` §5, §6.1, §7 (binding).

## Global Constraints
Same as plan 3: rules decide and write `target_hints`, generation renders; `differences` imports
only `ir`; deterministic output; every task ends with pytest + ruff format/check green, then a commit
ending `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

Rules that apply to both instances and templates iterate `_shapes(ir)` so plan 5 gets them free.

## Rules

| Rule | Subject | Strategies (default first) → hint |
|---|---|---|
| `VM-SSH-KEYS` | shape with `key_pair` | `import_public_key` (low; only when `key_material` present; param `username`) → `metadata["ssh-keys"] = "<username>:<key>"` · `os_login` (medium) → `metadata["enable-oslogin"] = "TRUE"` · `user_supplied_key` (low; params `username`, `public_key`) · `no_ssh_access` (medium) → `metadata["block-project-ssh-keys"] = "TRUE"`. Default is `os_login` when there is no key material. **Windows:** only `no_ssh_access`, whose effect says the key pair only decrypted the admin password and names `gcloud compute reset-windows-password`. `username` default preserves the AWS login: Ubuntu → `ubuntu`, Debian → `admin`, otherwise `ec2-user`. |
| `VM-USER-DATA` | shape with non-empty `user_data` | `startup_script` (medium) → `metadata["startup-script"]` (Windows: `windows-startup-script-ps1` / `-cmd`, with `<powershell>`/`<script>` wrappers stripped) · `cloud_init` (medium; Linux only) → `metadata["user-data"]` · `omit` (low). Default is `cloud_init` when the script starts with `#cloud-config`, since running YAML as shell would fail. |
| `VM-IAM-PROFILE` | shape with `iam_profile` | `manual` only. **Does not exclude** — the instance is generated with no service account and the role is reported. |
| `VM-SPOT` | shape with `spot` | `spot_vm` (medium) → `spot = True` · `standard` (low) |
| `VM-TENANCY` | instance, `dedicated`/`host` | `manual` only, not excluding: created on shared hardware |
| `VM-PLACEMENT-GROUP` | instance with `placement_group` | `manual` only, not excluding |
| `VM-IMDS` | shape with `imds.http_tokens == "required"` | `note_only` (none) |
| `VM-TERMINATION-PROTECTION` | instance with `termination_protection` | `deletion_protection` (none) → `deletion_protection = True` |
| `VM-ELASTIC-IP-UNATTACHED` | `external_address`, `attached=False` | `reserve_static` (low) → `reserve = True` · `omit` (none) → `excluded` |

Metadata hints are merged into one `metadata` dict hint, so rules compose rather than overwrite.

Order in `AWS_GCP_RULES`: after plan 3's rules; `VM-ELASTIC-IP-UNATTACHED` after `VM-PUBLIC-IP`.

**Fix to `VM-PUBLIC-IP`:** `static_external` is offered only when the source is an Elastic IP
(`allowed_strategies`); today it is offered to every public instance and the generator has no
address to bind.

## Generation
- `metadata` from the hint, keys sorted. `deletion_protection` from the hint.
- `scheduling { provisioning_model = "SPOT", preemptible = true, automatic_restart = false,
  on_host_maintenance = "TERMINATE", instance_termination_action = "STOP" }` when `spot`.
- `google_compute_address` (`region = var.region`, `address_type = "EXTERNAL"`) for every
  unattached address with `reserve`, and for every attached address whose instance chose `static`;
  that instance's `access_config` sets `nat_ip` to the address.
- **Fix:** a non-boot disk's `zone` follows its owning instance's `zone_letter`, not a hard-coded
  `a` (an instance in zone `b` could not attach it). Disks no live instance owns are not generated
  (plan 5's template disks).
- `GenerationError` for `static` without an address. **Spec correction:** spec §5 says GCP rejects
  `spot_vm` with termination protection; Compute Engine documents no such limit (only MIG members
  and instance templates cannot carry deletion protection), so both render.
- `FIELD_COVERAGE` entries for every field this plan makes real.

## Report (spec §7)
`## Compute` section, one row per migrated instance: machine type and memory delta, stock image and
the AMI not migrated, network tags and the policies they came from, reachability, disks created
empty. Closing paragraph: no disk contents, installed software or running workload were migrated.

## Required tests (minimum)
- Each rule: detection only where the table says; each strategy's hint; risk; params validated
  (`public_key` must look like an OpenSSH public key; `username` matches `^[a-z_][a-z0-9_-]*$`).
- SSH: no key material → `import_public_key` absent and default `os_login`; Windows → only
  `no_ssh_access`.
- User data: `#cloud-config` defaults to `cloud_init`; Windows `<powershell>` goes to
  `windows-startup-script-ps1` unwrapped; `cloud_init` not offered on Windows.
- `VM-IAM-PROFILE`, `VM-TENANCY`, `VM-PLACEMENT-GROUP` are manual and leave the instance generated.
- Metadata from two rules merges.
- `VM-PUBLIC-IP`: `static_external` not offered for an ephemeral source.
- Generator: metadata, scheduling, deletion_protection, reserved address, static `nat_ip`, disk zone
  follows instance, the static-without-address `GenerationError`, coverage.
- Report: Compute section rows and the closing paragraph.
- Golden `expected_compute.tf` updated for every fixture, and the provider-schema test still passes.

## Tasks
1. Nine rules + the `VM-PUBLIC-IP` fix, with tests; register.
2. Generator (metadata, scheduling, protection, addresses, disk-zone fix), coverage, tests, goldens,
   tofu schema test.
3. Report Compute section.
