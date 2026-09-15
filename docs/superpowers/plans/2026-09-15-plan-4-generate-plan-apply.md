# EasyMigrate Plan 4 of 4: OpenTofu, Generator, Report, `plan` and `apply`

> Lean plan: interfaces, rules and required tests. There is no pre-written code.

**Goal:**
- `easymigrate plan --gcp-project P` checks that the decisions are ready and builds resolved IR, then generates GCP Terraform and runs `tofu plan` with an app-managed OpenTofu. It writes `plan.lock.json` and the report.
- `easymigrate apply` shows the summary, asks `[y/N]` and applies exactly that plan.

**Spec (binding):** `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md` §5 (engine, HCL, mapping, plan, apply), §6 (report and errors), §7.6–7.9, §9 (done-when).

## Global Constraints
- Dependency rules as enforced by `tests/test_dependency_rules.py`. `easymigrate.engine.*` imports no `easymigrate` package except `easymigrate.core.errors`; it's added to the checker as allowed for core. Only `core.registry` imports plugins and providers.
- Generators read **only resolved IR** and `target_hints`, and skip objects with `excluded` set. They contain no difference logic.
- **GCP names and Terraform labels derive from the IR id's name part**, not the display name (plan 1 follow-up). Use `TfLabelAllocator` and `GcpNameAllocator` with the slug after `kind:`. A GCP name that differs from the IR `name` → `info / GEN-RENAMED`.
- **Every** resolved-IR decision (`resolution`) and every non-`info` finding tied to an object appears in that resource's HCL comment header.
- **No silent mapping.** Every field of every IR kind that a generator consumes must be declared in that generator's `FIELD_COVERAGE`. A test fails on any undeclared field and on any unknown rule id (details below).
- **Exit codes:** `0` ok; `1` error; `2` declined, or refused because of problems, a stale lock or a missing plan. There is **no** auto-approve flag.
- Rule failures (`DifferenceRuleError`) are never swallowed. `resolve_ir` returns `ResolvedIR(document, superseded)`, and every superseded decision is listed in the report and the plan summary.
- Credentials are never written to `.easymigrate/`.
- Commits end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Use `/home/sudhan/.local/bin/uv run ...`.

## Part A: engine, HCL fixes, generator, coverage

### A1. OpenTofu engine: `src/easymigrate/engine/tofu.py`
- **Pinned version:** pin `TOFU_VERSION` to the **latest stable OpenTofu 1.x** release. Look it up with `gh api repos/opentofu/opentofu/releases/latest`.
- **Checksums:** fill `TOFU_SHA256: dict[str, str]` (`"linux_amd64"`, `"linux_arm64"`, `"darwin_amd64"`, `"darwin_arm64"`, `"windows_amd64"` → sha256 of `tofu_<v>_<os>_<arch>.zip`) from that release's `tofu_<v>_SHA256SUMS` asset. Record the source URL in a comment.
- **`ensure_tofu(cache_dir: Path | None = None, *, downloader=None) -> Path`:**
  - If `EASYMIGRATE_TOFU_PATH` is set, use it after checking `tofu version -json` is ≥ `TOFU_MIN_VERSION` (`"1.6.0"`).
  - Otherwise use the cached `~/.cache/easymigrate/tofu/<v>/tofu`.
  - Otherwise download the zip via the injectable `downloader(url) -> bytes`, **verify sha256 before extracting** (a mismatch → `TofuError` and nothing is written), extract, then chmod 0o755.
- **`TofuError(EasyMigrateError)`:** the message names the fix, e.g. suggest `EASYMIGRATE_TOFU_PATH` when the download fails.
- **`class Tofu(binary: Path, workdir: Path, *, runner=subprocess.run, env: dict | None = None)`** with `init(backend_config: dict[str,str])`, `validate()`, `plan(out: Path) -> str` (plan text), `show_json(plan: Path) -> dict` and `apply(plan: Path, stream: Callable[[str], None])`.
  - Every call uses `-no-color -input=false`.
  - `TF_PLUGIN_CACHE_DIR` is `~/.cache/easymigrate/plugins`.
  - A non-zero exit → `TofuError` with the tool's stderr as-is, plus a hint when the text matches a known cause:
    - `SERVICE_DISABLED` or `has not been used in project` → "enable the API: `gcloud services enable compute.googleapis.com --project P`"
    - `403` or `Permission` → "the service account lacks permission"
- **Tests** use a fake downloader and runner. Cover:
  - checksum mismatch rejected and nothing cached
  - cache reused, with no second download
  - the env override, with a version check that fails on a too-old version
  - argument lists for each command
  - error hint mapping

### A2. HCL builder fixes: `providers/gcp/hcl.py` (plan 1 follow-ups)
- Escape every control character other than `\n`, `\r` and `\t` as `\uNNNN`.
- Render a list that contains maps across multiple lines with correct indentation:
  ```
  allow = [
    {
      ports    = ["443"]
      protocol = "tcp"
    },
  ]
  ```
  Lists of scalars stay on one line. The expected strings in the tests pin the exact output.

### A3. Generator: `plugins/network/generate_gcp.py`
- **Interface:** `generate(resolved: IRDocument, ctx: GenerateContext) -> HclFile` and `NetworkPlugin.generate_gcp`.
- **`GenerateContext`:** add it to `core/plugin.py` as a dataclass with `project`, `region`, `findings: list[Finding]` and `.info()`. Extend the `Plugin` protocol with `generate_gcp`.
- **Mapping:** exactly spec §5.3, plus these rulings:
  - **Network:** `google_compute_network` with `auto_create_subnetworks=false`, `delete_default_routes_on_create=true` and `routing_mode="REGIONAL"`. There is no DNS-hostname equivalent; declare that in coverage as `not_generated`.
  - **Subnet:** `google_compute_subnetwork` in `var.region`. `target_hints.no_external_ip` → a comment only, since plan-4 networking has no VMs; declare it `not_generated`.
  - **Internet:** one `google_compute_route` `0.0.0.0/0` → `default-internet-gateway` per network that has ≥1 `public` subnet.
  - **NAT:** one `google_compute_router` plus `google_compute_router_nat` per `nat` object. The NAT has `source_subnetwork_ip_ranges_to_nat="LIST_OF_SUBNETWORKS"` and one `subnetwork` block per served subnet (`ALL_IP_RANGES`). A NAT that serves no subnets is skipped with `info / GEN-NAT-UNUSED`.
  - **Firewall policies:**
    - The network tag is `fw-<gcp name of policy>`.
    - Group rules by `(direction, peer kind)` into one `google_compute_firewall` per group, named `<policy>-<in|eg>-<cidr|tag|self>`.
    - Each has `target_tags=[tag]`, `priority=1000`, and one `allow` block per (protocol, ports) combination, de-duplicated and sorted.
    - `all` → `protocol = "all"`. A numeric protocol stays a string.
    - Ingress: `CidrPeer` → `source_ranges`; `PolicyPeer` → `source_tags = [peer tag]`; `SelfPeer` → `source_tags = [own tag]`.
    - Egress: `CidrPeer` → `destination_ranges`; `PolicyPeer` → `destination_ranges = target_hints.egress_ranges[peer]`.
    - Skip exactly one egress rule of `all` to `0.0.0.0/0` (the AWS default).
    - Any `PrefixListPeer`, `ExternalPolicyPeer` or ICMP type left in resolved IR → raise `GenerationError`. The difference rules must already have removed them, and this guards the contract.
  - **Excluded objects:** nothing is generated; the report lists them.
- **Files:** `terraform/providers.tf` (the `required_providers` google source `hashicorp/google`, pinned `~> 6.0` or the latest major; check `gh api repos/hashicorp/terraform-provider-google/releases/latest`), `backend.tf` (`backend "gcs" {}`, with values passed through `-backend-config`), `variables.tf` (`project`, `region`), `terraform.tfvars`, and `network.tf`. Each file starts with a header comment: `Generated by EasyMigrate <version>. Do not edit by hand; re-run easymigrate plan.`
- **Resource headers:** `# from AWS <source.type> <source.id> (<name>)`, then one `# decision <key> → <strategy> (<decided_by>)` line per resolution, then `# ⚠ <finding code>: <message>` lines. `generate` receives a `decided_by` map through the context, `ctx.decided_by: dict[str, str]`.

### A4. No silent mapping: `plugins/network/generate_gcp.py` and `tests/plugins/network/test_field_coverage.py`
- `FIELD_COVERAGE: dict[str, dict[str, str]]` maps IR kind to field name to a declaration. Allowed values:
  - `"exact"`: copied as-is
  - `"metadata"`: for `kind`, `id`, `name`, `labels`, `source`, `resolution` and `excluded`
  - `"rule:<RULE_ID>"`: handled by a difference rule before generation
  - `"hint:<key>"`
  - `"not_generated:<plain reason>"`
  - `"derived:<plain explanation>"`
- **The test:**
  - (a) For every kind in `easymigrate.ir.network`, the set of declared fields equals `model_fields` of that class **exactly**. So adding an IR field without declaring it fails CI.
  - (b) For nested models (`FirewallRule`, `AclRule` and the peers), the fields are declared under `"FirewallRule"` and similar keys.
  - (c) Every `rule:` id exists in `catalogue()`.
  - (d) Every `hint:` key is produced by some rule. Check by grepping rule module source for the key string.
- `docs/field-coverage.md` is generated from `FIELD_COVERAGE` by `scripts/gen_differences_doc.py` (extend it), with the same sync test as the differences doc.

### A5. Generator tests
- **Unit tests per mapping row**, with exact HCL strings for:
  - one network plus a public subnet (route created)
  - private_nat plus NAT
  - an isolated-only network (no route)
  - SG→firewall grouping
  - self and policy peers
  - egress ranges hint
  - the default allow-all egress skipped
  - excluded objects skipped
  - the rename finding
  - header lines
- **Generator golden tests:** for each plan 2 fixture case: normalize → detect → decisions with all defaults and every manual decision acknowledged → `resolve_ir` → generate. Compare against `tests/plugins/network/fixtures/<case>/expected_network.tf`, using `--update-golden` to regenerate.
- **`tofu validate` test**, marked `@pytest.mark.tofu`: for each golden, write the files to a tmp dir, run `ensure_tofu()`, `init -backend=false`, then `validate`. Register the marker in pyproject. CI adds a job step `uv run pytest -m tofu` with `actions/cache` on `~/.cache/easymigrate`. The default `uv run pytest` excludes this marker (`addopts -m "not tofu"`).

## Part B: report, plan, apply, CLI

### B1. Report: `core/report.py`
- `build_report(project) -> Report` uses whichever of ir, differences, decisions, superseded and plan summary exist.
- `write_report(project, report)` writes `report.md` and `report.json`. It runs after analyze, resolve and plan.
- **Sections (spec §6.1):**
  1. Header (source → target)
  2. Coverage line
  3. Scan gaps
  4. Needs your action (manual decisions, excluded objects, superseded decisions)
  5. Decisions table (key, strategy, default/user, risk)
  6. Not yet supported
  7. Translated counts per kind
  8. Plan summary (add, change, destroy), when present

  `info` findings appear only in md and json.
- The terminal rendering reuses the same `Report` object (`cli.render_report`).
- **Tests:** section presence and ordering, md snapshot for a small fixture, json schema keys.

### B2. `plan`: `core/pipeline.plan()`
- **Signature:** `plan(project, *, gcp_project, gcp_region=None, confirm_bucket: Callable[[str], bool], tofu=None, gcp=None) -> PlanSummary`.
- **Steps:**
  1. **Region:** the map `REGION_MAP` in `core/regions.py` covers every AWS commercial region. It maps to the nearest GCP region; for example `ap-south-1→asia-south1`, `us-east-1→us-east4`, `us-west-2→us-west1` and `eu-west-1→europe-west1`. The full list is chosen by the implementer and documented. An unknown region without `--gcp-region` → `EasyMigrateError`.
  2. **Readiness:** `readiness_problems` non-empty → raise `NotReady(problems)`, which is exit 2 with the problems listed.
  3. **Resolve:** `resolve_ir` → write `resolved_ir.json` and keep `superseded`.
  4. **Credentials:** `gcp.credentials_ok()` (`google.auth.default()`) → otherwise `EasyMigrateError` with the `gcloud auth application-default login` hint.
  5. **State bucket:** `gcp.ensure_bucket(name, confirm)`, with name `<project>-easymigrate-tfstate`. A missing bucket calls `confirm_bucket(name)`: False → exit 2; True → create with versioning plus uniform bucket-level access in `gcp_region`. This lives in `providers/gcp/state_bucket.py` and uses `google-cloud-storage`; add the dependency.
  6. **Generate:** write `terraform/*` (remove stale `*.tf` first).
  7. **Tofu:** `ensure_tofu`, `init(backend_config={"bucket": name, "prefix": "easymigrate"})`, `plan(out=tfplan)`, write `plan.txt`, then `show_json` → counts of create/update/delete/replace plus a list of changed or destroyed addresses.
  8. **Lock:** `plan.lock.json` = sha256 of `ir.json`, `decisions.yaml`, `resolved_ir.json`, every `terraform/*.tf` and `tfplan`, plus `gcp_project` and `gcp_region`. Hash files by bytes.
  9. **Config and report:** save `config.target`, then write the report.
- **`PlanSummary`:** counts, changed or destroyed addresses, superseded, findings, and the plan text path.
- **Registry:** `registry.gcp_target()` returns the adapter that wraps `state_bucket` and auth. Core never imports `google`.

### B3. `apply`: `core/pipeline.apply()`
- **Signature:** `apply(project, *, confirm: Callable[[PlanSummary], bool], stream: Callable[[str], None], tofu=None) -> ApplyResult`.
- **Steps:**
  1. A missing `tfplan` or lock, or any hash mismatch → `PlanStale` (exit 2, "run `easymigrate plan` again").
  2. Rebuild the summary from `show_json` and the lock.
  3. `confirm(summary)` is False → `Declined` (exit 2).
  4. `tofu.apply(tfplan, stream)`.
  5. Write the report.
- **Partial failure:** a `TofuError` propagates (exit 1) with the message "state is safe in the bucket; re-run `easymigrate plan` then `apply`".

### B4. CLI
- **`easymigrate plan --gcp-project P [--gcp-region R]`:**
  - prints superseded warnings, a summary table (add, change, destroy), the highlighted destroy and replace addresses, and the path of `plan.txt`
  - the bucket prompt is `State bucket <name> does not exist. Create it in <region>? [y/N]`
- **`easymigrate apply`:**
  - prints the summary again, highlighting `destroy` and `replace` in red
  - prompts `Apply these changes to project <P>? [y/N]`
  - streams the output
  - exit codes as above
- After `analyze` and `resolve`, the report is also written. Their terminal output doesn't change beyond one line: `Report: .easymigrate/report.md`.
- README usage gains `resolve`, `plan` and `apply`, plus the GCP credentials and roles needed (`roles/compute.networkAdmin`, `roles/storage.admin` on the project for the bucket).

### B5. Testbed and live test
- `testbed/aws/main.tf` gains:
  - a private subnet
  - a NAT gateway plus EIP and a private route table
  - two security groups referencing each other
  - a custom NACL on the private subnet

  Keep EKS and RDS resources as they are. Add `testbed/aws/README.md` cost notes for NAT.
- `tests/live/README.md` holds a manual, never-in-CI script `tests/live/run.sh`:
  1. `terraform -chdir=testbed/aws apply`
  2. in a temp dir: `easymigrate scan` → `analyze` → `resolve --accept-defaults --acknowledge-manual` → `plan --gcp-project $GCP_PROJECT` → `apply`
  3. `gcloud compute networks list`
  4. `tofu destroy` in `.easymigrate/terraform`
  5. `terraform -chdir=testbed/aws destroy`

  Don't run it; the user runs it.

### B6. Required tests
- **plan:**
  - not ready → exit 2 listing the problems
  - unknown region
  - bucket declined → exit 2
  - bucket created when confirmed (fake gcp)
  - stale `*.tf` removed
  - the lock contains every hash
  - superseded carried
  - a fake `Tofu` records init backend_config and the plan path
- **apply:**
  - lock mismatch after editing `terraform/network.tf` → exit 2
  - missing tfplan → exit 2
  - declined → exit 2 and tofu apply is not called
  - confirmed → apply called with the tfplan path and output streamed
  - a TofuError → exit 1 with the recovery message
- **CLI:** plan and apply through `CliRunner` with `input=` for the prompts, and fakes injected via monkeypatching `easymigrate.core.registry.gcp_target` and `easymigrate.engine.tofu.ensure_tofu`
- **Report:** covered in B1.

## Execution
- **Part A** is one implementer pass with one review. **Part B** is one implementer pass with one review.
- Each part ends green: `pytest`, `ruff format --check` and `ruff check`. The Part A reviewer additionally runs `uv run pytest -m tofu` once, since it needs the network.
- Commit in logical chunks. Don't push until the controller merges.
