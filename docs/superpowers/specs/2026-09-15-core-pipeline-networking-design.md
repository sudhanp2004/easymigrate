# EasyMigrate — Core Pipeline + Networking (Spec 1)

- **Date:** 2026-09-15
- **Status:** Draft, awaiting review
- **Scope:** the core engine, CLI, intent-level IR, findings report, and review-then-apply flow, built end to end for **networking** only.

## 1. Product context

EasyMigrate migrates cloud infrastructure between providers:

1. **Scan** the live source cloud: every resource, rule and instance.
2. **Analyze:** normalize it into a cloud-neutral, intent-level intermediate representation (IR).
3. **Plan:** generate destination Terraform and show it to the user.
4. **Apply:** deploy it only after the user explicitly approves.

### Decisions already made

| # | Decision |
|---|---|
| D1 | The IR is **intent-level**: it describes what infrastructure does, not the source cloud's resource shapes. |
| D2 | Anything that doesn't translate is **shown to the user** with an explanation. Nothing is dropped silently. |
| D3 | The source of truth is the **live cloud state** read through APIs, not the customer's existing IaC. |
| D4 | Generate Terraform → show the plan → **apply only after explicit approval**. |
| D5 | **AWS → GCP only** until it is solid. |
| D6 | **CLI first.** Core logic is a library that a later web app reuses. |
| D7 | Access: a read-only AWS IAM role (or keys); a GCP service account for the destination project. |
| D8 | Secrets are not readable from AWS. New ones are generated, stored in GCP Secret Manager and reported; users may rotate them later. (This first applies in the RDS spec, not here.) |
| D9 | Python. The destination's Terraform state lives in a GCS bucket in the customer's own GCP project. |
| D10 | Infrastructure migration comes first. Data and content migration is a later, assisted, paid service. |

### MVP resource roadmap (one spec each)

1. **Networking** (this spec): VPC, subnets, route tables, internet gateway, NAT gateway, security groups, NACLs
2. RDS (Postgres, MySQL)
3. EKS
4. EC2
5. S3 buckets (configuration only)
6. ALB/NLB, and IAM (reported only)

Anything else found during a scan is listed as *not yet supported*.

### Out of scope for this spec

- Every resource type except networking
- Data migration
- The web app
- Destination clouds other than GCP
- Cutover, DNS switching and rollback orchestration

## 2. Architecture

Plugin-per-resource-family (approach "A"). Each family has three parts:

- **collector:** calls the AWS API and saves the raw responses to the snapshot
- **normalizer:** turns the snapshot into IR objects plus findings
- **generator:** turns IR objects into GCP Terraform

The core runs these steps and knows nothing about specific resource families. IR objects refer to each other by `id`, and those references also give Terraform its dependency order.

### 2.1 Module layout

```
easymigrate/
├── pyproject.toml              # package; `easymigrate` console script
├── src/easymigrate/
│   ├── cli.py                  # Typer commands: scan / analyze / plan / apply
│   ├── core/
│   │   ├── pipeline.py         # runs the stages, calls plugin parts
│   │   ├── project.py          # reads and writes .easymigrate/
│   │   ├── registry.py         # plugin discovery
│   │   └── report.py           # findings report (terminal, md, json)
│   ├── ir/
│   │   ├── base.py             # IntentObject, SourceRef, Finding, IRDocument
│   │   └── network.py          # Network, Subnet, Nat, FirewallPolicy, CustomRoute
│   ├── providers/
│   │   ├── aws/session.py      # boto3 session (role or profile), retry config
│   │   └── gcp/
│   │       ├── hcl.py          # HCL builder
│   │       └── terraform.py    # terraform binary checks, state bucket, init/plan/apply runner
│   └── plugins/
│       └── network/
│           ├── collect_aws.py
│           ├── normalize.py
│           └── generate_gcp.py
├── docs/findings.md            # catalogue of finding codes
├── testbed/aws/                # AWS test stack (moved from ./main.tf)
├── prototype/                  # current scanner.py / mapper.py / compiler.py, kept as reference
├── scripts/anonymize_fixture.py
└── tests/
```

**Dependency rules**
- `cli` imports only `core`.
- `core` imports `ir` and the plugin interface. It never imports boto3 or GCP code directly.
- Plugins import `ir` and `providers`.
- The future web backend replaces `cli` and calls the same `core`.

**Plugin interface** (`core/registry.py`):

```python
class Plugin(Protocol):
    name: str                                                      # "network"
    def collect_aws(self, session, region) -> dict[str, Any]: ...  # file name → raw JSON
    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]: ...
    def generate_gcp(self, objects: list[IntentObject], ctx: GenerateContext) -> HclFile: ...
```

A plugin may read objects produced by earlier plugins, for example a future database plugin referring to `subnet:*`. It does this through `ctx.lookup(id)`. The order plugins run in is declared in the registry, and `network` comes first.

**Libraries:** Typer, Pydantic v2, boto3, Rich, google-cloud-storage (for the state bucket only). The user's installed `terraform` is used, with a minimum version check. No Terraform binary is bundled.

### 2.2 Project directory

The tool creates this folder in the directory where the command is run:

```
.easymigrate/
├── project.yaml         # source account/region, target project/region, tool version
├── snapshot/aws/*.json  # output of scan: raw API responses, one file per collector
├── ir.json              # output of analyze
├── report.md            # output of analyze and plan
├── report.json
├── terraform/*.tf       # output of plan
├── tfplan               # output of plan (binary plan)
├── plan.txt             # output of plan (human-readable)
└── plan.lock.json       # fingerprint of terraform/ and ir.json at plan time
```

Each stage reads only the files the previous stage wrote. Once a scan is saved, `analyze` and `plan` can be re-run without calling AWS again.

### 2.3 CLI

```
easymigrate scan     --aws-profile X | --aws-role-arn ARN   --region ap-south-1
easymigrate analyze
easymigrate plan     --gcp-project Y [--gcp-region asia-south1]
easymigrate apply
```

If no `--gcp-region` is given, it comes from a region map (`ap-south-1 → asia-south1`, and so on). A source region that isn't in the map is an error that asks for `--gcp-region`. The tool never falls back to a default region silently.

## 3. Intent-level IR

### 3.1 Base types (`ir/base.py`)

```python
class SourceRef(BaseModel):
    cloud: Literal["aws"]
    type: str            # "vpc", "subnet", "security_group", ...
    id: str              # native id or ARN
    region: str

class Finding(BaseModel):
    level: Literal["info", "approximated", "untranslated", "unsupported", "scan_gap"]
    code: str            # stable, e.g. "NET-ZONE-REGIONAL"; documented in docs/findings.md
    message: str         # plain-language explanation for the user
    source_field: str | None = None
    suggestion: str | None = None

class IntentObject(BaseModel):
    id: str              # "<kind>:<name>", unique within the document
    kind: str
    name: str
    labels: dict[str, str]   # AWS tags, cleaned to GCP label rules
    source: SourceRef
    findings: list[Finding] = []

class IRDocument(BaseModel):
    schema_version: Literal["1"]
    source: dict             # cloud, account_id, region, scanned_at
    objects: list[IntentObject]
    findings: list[Finding]  # findings not tied to one object: scan_gap, unsupported inventory
```

- **IDs** use the resource's `Name` tag, with characters that aren't allowed replaced. If a resource has no `Name` tag, the native ID is used. Collisions get a numeric suffix.
- **Finding levels:**
  - `info`: a harmless change, such as a renamed resource
  - `approximated`: translated, but it behaves somewhat differently
  - `untranslated`: found and understood, but cannot be recreated automatically
  - `unsupported`: a resource type this release does not handle
  - `scan_gap`: the scan could not read something, for example because of a missing permission

### 3.2 Networking objects (`ir/network.py`)

| Kind | Built from (AWS) | Fields |
|---|---|---|
| `network` | VPC | `cidr_blocks: list[str]`, `dns_hostnames: bool`, `dns_resolution: bool` |
| `subnet` | Subnet + its route table + IGW/NAT routes | `network: id`, `cidr`, `zone_hint`, `internet: "public" \| "private_nat" \| "isolated"`, `map_public_ip: bool` |
| `nat` | NAT gateway | `network: id`, `serves_subnets: list[id]` |
| `firewall_policy` | Security group | `network: id`, `rules: list[FirewallRule]`, `applies_to: list[str]` (native IDs of attached resources, filled in by later plugins) |
| `custom_route` | Any route other than local, IGW or NAT (peering, VPN, TGW, ENI) | `network: id`, `destination`, `target_type`, `target_id` |

```python
class FirewallRule(BaseModel):
    direction: Literal["ingress", "egress"]
    protocol: Literal["tcp", "udp", "icmp", "all"]
    ports: list[str]        # "443", "8000-8080"; empty means all ports
    peer: CidrPeer | PolicyPeer | SelfPeer
    description: str | None
```

### 3.3 Normalization rules

- **Subnet `internet` value.** Look at the route table associated with the subnet, or the VPC's main route table if there is none:
  - a `0.0.0.0/0` route to an IGW → `public`
  - a `0.0.0.0/0` route to a NAT gateway → `private_nat`
  - neither → `isolated`
- **IGW and route tables** don't become IR objects. They are folded into `subnet.internet` and `custom_route`.
- **Subnets map 1:1 with their CIDRs unchanged**, so apps with hardcoded IPs keep working. Every subnet gets `approximated / NET-ZONE-REGIONAL`.
- **Security groups** become `firewall_policy`. A rule that references another security group becomes a `PolicyPeer` pointing at that policy's id. A reference to a security group in another VPC or account → `untranslated / NET-SG-XVPC-REF`.
- **NACLs:** the default allow-all NACL is ignored. Any other NACL → `untranslated / NET-NACL` on each subnet it's associated with.
- **Custom routes** → a `custom_route` object carrying `untranslated / NET-CUSTOM-ROUTE`. Peering routes use the more specific code `NET-PEERING`.
- **IPv6** (VPC or subnet IPv6 CIDRs) → `untranslated / NET-IPV6`.
- **Multiple VPCs** are fully supported. Each one becomes its own `network`.
- **CIDRs that overlap across VPCs** are allowed, because GCP networks are isolated from each other. `info / NET-CIDR-OVERLAP` records it.

## 4. Generation, review and apply

### 4.1 HCL builder (`providers/gcp/hcl.py`)

This is a small builder for blocks, attributes, references and comments. It escapes strings correctly and writes output in a fixed, stable order so the expected-output test files don't churn. Generated files never use string templating.

Every resource gets a comment header showing its origin and its non-`info` findings:

```hcl
# from AWS subnet subnet-09125e7961d295170 (migration-test-subnet-1)
# ⚠ approximated NET-ZONE-REGIONAL: GCP subnets span the region
resource "google_compute_subnetwork" "migration_test_subnet_1" {
  name          = "migration-test-subnet-1"
  ip_cidr_range = "10.0.1.0/24"
  region        = var.region
  network       = google_compute_network.migration_test_vpc.id
}
```

**Naming**
- Terraform address: the IR `id` cleaned to `[a-z0-9_]`, with a numeric suffix on collision.
- GCP name: lowercase, `[a-z]([-a-z0-9]{0,61}[a-z0-9])?`. If a name has to change → `info / GEN-RENAMED`.

**Output files** in `.easymigrate/terraform/`:
- `providers.tf`: google provider, pinned version
- `backend.tf`: GCS backend
- `variables.tf`: `project`, `region`
- `terraform.tfvars`: written from the CLI flags
- `<plugin>.tf`: one per plugin

### 4.2 Networking → GCP mapping

| IR | GCP Terraform |
|---|---|
| `network` | `google_compute_network`: `auto_create_subnetworks = false`, `delete_default_routes_on_create = true`, `routing_mode = "REGIONAL"` |
| `subnet` | `google_compute_subnetwork` |
| any subnet in the network with `internet = public` | one `google_compute_route` for `0.0.0.0/0` → `default-internet-gateway` |
| `nat` / subnets with `private_nat` | `google_compute_router` + `google_compute_router_nat` limited to those subnetworks (`LIST_OF_SUBNETWORKS`) |
| subnet with `internet = isolated`, in a network that has an internet route | `approximated / NET-ISOLATION-VM-LEVEL`: GCP routes are network-wide, so isolation depends on VMs having no external IP |
| `firewall_policy` | a network tag `fw-<policy-name>`; one `google_compute_firewall` per (direction, peer kind) group, with `target_tags = [tag]`. `CidrPeer` → `source_ranges` / `destination_ranges`. `PolicyPeer` → `source_tags`. `SelfPeer` → `source_tags = [own tag]`. An egress rule with a `PolicyPeer` → `approximated / NET-EGRESS-TAG` (GCP egress can't target tags), generated as the CIDR ranges of the subnets the peer policy's `applies_to` resources sit in. In this spec nothing fills in `applies_to` yet, so it falls back to the peer's whole network CIDR blocks, and the finding message says so. |
| AWS default allow-all egress rule | skipped, because GCP already allows all egress by default |

Attaching network tags to resources is the job of later plugins such as EC2 and EKS.

### 4.3 `plan`

1. Preflight:
   - `terraform` is installed and at least the minimum version.
   - GCP credentials are available.
   - The required APIs (`compute.googleapis.com`) are enabled. If not, report them with the command to enable them.
2. Ensure the state bucket `<project>-easymigrate-tfstate` exists. If it is missing, **ask for confirmation**, then create it with versioning and uniform bucket-level access.
3. Generate `terraform/`.
4. Run `terraform init`, then `terraform plan -out=tfplan`, and save `plan.txt`.
5. Write `plan.lock.json` containing the SHA-256 of every file in `terraform/`, of `ir.json`, and of `tfplan`.
6. Print the summary: add, change and destroy counts plus the top findings, and update the report.

### 4.4 `apply`

1. **Refuse** (exit 2) if `tfplan` is missing or `plan.lock.json` no longer matches the current files. The message tells the user to re-run `plan`.
2. Print the summary again. Resources that would be **changed or destroyed are highlighted**.
3. Prompt: `Apply these changes to project <X>? [y/N]`. Anything other than `y` or `yes` → exit 2. The MVP has **no auto-approve flag**.
4. Run `terraform apply tfplan` and stream its output.
5. If it fails partway, state is safe in GCS, and re-running `plan` then `apply` continues from where it stopped.

## 5. Report and error handling

### 5.1 Report

It is shown in the terminal (Rich) and written as `report.md` and `report.json` after `analyze` and `plan`.

- **Header:** source (account and region) → target (project and region).
- **Coverage line:** counts of resources found, translated, approximated, untranslated and not yet supported, plus scan gaps.
- **Sections, most serious first:**
  1. scan gaps
  2. untranslated
  3. approximated
  4. not yet supported
  5. translated

  Each finding shows the object, code, message and suggestion. `info` findings appear only in the md and json files.
- **Unsupported inventory:** during `scan`, the AWS Resource Groups Tagging API (`GetResources`) lists resources of every service. Resource types with no plugin → `unsupported`. The report states plainly that untagged resources of unsupported services may be missing from this list.
- **Codes:** every code is documented in `docs/findings.md`: what it means, why it happens, and what to do.

### 5.2 Error handling

| Situation | Behaviour |
|---|---|
| Missing or invalid credentials, unknown region | Stop with a clear message and the fix; exit 1 |
| `AccessDenied` on one API | That collector records `scan_gap` (with the IAM action), and the scan continues |
| Throttling | botocore `adaptive` retry mode; paginators on every list call |
| The normalizer meets unexpected data | `untranslated / INTERNAL-*` on that object, and the run continues. In strict mode (tests) it raises. |
| Terraform error | Show Terraform's output as-is, plus a hint for known causes (API disabled, missing permission) |
| A stage's input is missing or stale | Name the command to run first; exit 1 |

**Exit codes:**
- `0` success, even when there are findings
- `1` error
- `2` the user declined, or apply refused because of a lock mismatch

### 5.3 Protecting sensitive data

- Credentials are never written to `.easymigrate/`.
- If the working directory is inside a git repo, `.easymigrate/` is added to `.gitignore` on the first run.
- The CLI warns once that `.easymigrate/` holds account metadata: the account ID, ARNs and endpoints.

## 6. Testing

1. **Normalizer golden tests (the main safety net).** `tests/fixtures/aws/network/<case>/snapshot/*.json` → compare with `expected_ir.json`. Cases:
   - `basic_public`
   - `private_with_nat`
   - `sg_references_sg`
   - `custom_nacl`
   - `peering_route`
   - `multi_vpc`
   - `ipv6`

   Tests run in strict mode. `pytest --update-golden` regenerates the expected files for review in git.
2. **Generator golden tests.** IR → expected `.tf` files. HCL builder unit tests cover escaping, ordering and name collisions.
3. **Collector tests.** `moto` provides fake AWS resources. botocore `Stubber` injects `AccessDenied` responses to check `scan_gap` handling.
4. **Terraform validity.** `terraform init -backend=false && terraform validate` on every generated golden output.
5. **Pipeline tests.** moto → `scan` → `analyze` → `plan`, with a fake Terraform runner. `apply` is tested for:
   - refusing on a lock mismatch
   - exit 2 when the user declines
   - no way to bypass the prompt
6. **Live test (manual, never in CI).** `tests/live/`: create `testbed/aws`, run the full pipeline into a scratch GCP project, check the result, destroy both sides.

**Testbed:** `main.tf` moves to `testbed/aws/` and gains a private subnet with a NAT gateway, two security groups that reference each other, and a custom NACL. A NAT gateway is billed hourly, so create the testbed only while testing.

**Tooling**
- pytest and ruff.
- GitHub Actions runs lint, unit tests and `terraform validate` on every push. Terraform provider downloads are cached.
- `scripts/anonymize_fixture.py` replaces account IDs, ARNs and endpoints before a real scan is saved as a fixture.

## 7. Migrating from the prototype

- `scanner.py`, `mapper.py` and `compiler.py` move to `prototype/`, unchanged. They are deleted as the RDS and EKS plugins replace them.
- `main.tf` and `.terraform.lock.hcl` move to `testbed/aws/`. Local `terraform.tfstate` and `.terraform/` must move with them, or the testbed's state is lost; this is done by hand, because they aren't tracked by git.

## 8. Done when

- On the extended testbed, `easymigrate scan → analyze → plan → apply` creates the matching GCP network, subnets, NAT and firewall rules in a scratch project.
- The report lists every networking finding, and lists RDS and EKS as not yet supported.
- All golden, collector and pipeline tests pass in CI, and `terraform validate` passes on every generated output.
