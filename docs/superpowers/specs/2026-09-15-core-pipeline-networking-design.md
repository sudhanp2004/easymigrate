# EasyMigrate — Core Pipeline + Networking (Spec 1)

- **Date:** 2026-09-15
- **Status:** Draft rev 2, awaiting review
- **Scope:** the core engine, CLI, intent-level IR, **difference engine**, report, and review-then-apply flow, built end to end for **networking** only.

## 1. Product context

EasyMigrate migrates cloud infrastructure between providers:

1. **Scan** the live source cloud: every resource, rule and instance.
2. **Analyze:** normalize it into a cloud-neutral, intent-level intermediate representation (IR), then detect every difference between the source and target clouds that affects this infrastructure.
3. **Resolve:** show each difference with its handling options. Safe defaults are preselected, and the user can change them.
4. **Plan:** generate destination Terraform from the IR and the decisions, and show the plan.
5. **Apply:** deploy only after the user explicitly approves.

**Handling cross-cloud differences is the product's core value.** It is a first-class component (§4) with its own model, catalogue, tests and documentation, not logic scattered across plugins.

### Decisions already made

| # | Decision |
|---|---|
| D1 | The IR is **intent-level**: it describes what infrastructure does, not the source cloud's resource shapes. |
| D2 | Every difference and every untranslatable item is **shown to the user** with an explanation. Nothing is dropped silently. |
| D3 | The source of truth is the **live cloud state** read through APIs. |
| D4 | Generate → show the plan → **apply only after explicit approval**. |
| D5 | **AWS → GCP only** until it is solid. |
| D6 | **CLI first.** Core logic is a library that a later web app reuses. |
| D7 | Access: a read-only AWS IAM role (or keys); a GCP service account for the destination project. |
| D8 | Secrets are not readable from AWS. New ones are generated, stored in GCP Secret Manager and reported (this first applies in the RDS spec). |
| D9 | Python. The destination's IaC state lives in a GCS bucket in the customer's own GCP project. |
| D10 | Infrastructure first. Data and content migration is a later, assisted, paid service. |
| D11 | **The IaC engine runs inside the application.** Customers never install or run Terraform. The engine is **OpenTofu** (MPL-licensed, compatible with Terraform), which avoids the embedding restrictions of Terraform's BSL licence. Generated files stay standard HCL. |
| D12 | **Differences are a first-class core component** (the difference engine). Each difference offers **choices with safe defaults**: most users accept the defaults, experienced users take control. |

### MVP resource roadmap (one spec each)

1. **Networking** (this spec): VPC, subnets, route tables, internet gateway, NAT gateway, security groups, NACLs
2. RDS (Postgres, MySQL)
3. EKS
4. EC2
5. S3 buckets (configuration only)
6. ALB/NLB, and IAM (reported only)

Anything else found during a scan is listed as *not yet supported*.

**Depth-first.** Each family is finished in full before the next one starts. "Finished" means complete collection, faithful IR, a full difference-rule catalogue with tested strategies, generation, and a live testbed run. The project deliberately does not build thin demo slices across families.

### Out of scope for this spec

- Every resource type except networking
- Data migration
- The web app and running the engine on hosted servers
- Destination clouds other than GCP
- Cutover, rollback, and checking after apply that the new setup behaves the same

## 2. Architecture

Plugin-per-resource-family, plus a separate difference engine.

| Component | Job |
|---|---|
| **Plugin collector** | AWS API → raw snapshot |
| **Plugin normalizer** | snapshot → IR. It is **faithful**: it records what exists, including things GCP can't do, and does not judge translatability. |
| **Difference engine** | IR + target cloud → detected differences → user decisions → **resolved IR** |
| **Plugin generator** | resolved IR → HCL. Simple and mechanical, with no difference-handling logic. |

This split keeps the knowledge in one place. The normalizer knows AWS. The generator knows GCP Terraform. The difference engine alone knows how the two clouds differ and how to bridge them.

### 2.1 Module layout

```
easymigrate/
├── pyproject.toml
├── src/easymigrate/
│   ├── cli.py                    # Typer: scan / analyze / resolve / plan / apply
│   ├── core/
│   │   ├── pipeline.py           # stage orchestration
│   │   ├── project.py            # reads and writes .easymigrate/
│   │   ├── registry.py           # plugin and rule discovery
│   │   └── report.py             # report (terminal, md, json)
│   ├── ir/
│   │   ├── base.py               # IntentObject, SourceRef, Finding, IRDocument
│   │   └── network.py            # Network, Subnet, Nat, FirewallPolicy, NetworkAcl, CustomRoute
│   ├── differences/
│   │   ├── model.py              # DifferenceRule, Strategy, Difference, Decision
│   │   ├── engine.py             # detect → apply decisions → resolved IR
│   │   ├── decisions.py          # decisions.yaml load/save/staleness
│   │   └── rules/
│   │       └── aws_gcp/
│   │           └── network.py    # the networking rule catalogue (§4.4)
│   ├── engine/
│   │   └── tofu.py               # OpenTofu download/verify/cache, init/plan/apply runner
│   ├── providers/
│   │   ├── aws/session.py        # boto3 session (role or profile), retry config
│   │   └── gcp/
│   │       ├── hcl.py            # HCL builder
│   │       └── state_bucket.py   # GCS state bucket check/create
│   └── plugins/
│       └── network/
│           ├── collect_aws.py
│           ├── normalize.py
│           └── generate_gcp.py
├── docs/differences.md           # generated from rule metadata
├── testbed/aws/                  # AWS test stack (moved from ./main.tf)
├── prototype/                    # current scanner.py / mapper.py / compiler.py, kept as reference
├── scripts/
│   ├── anonymize_fixture.py
│   └── gen_differences_doc.py
└── tests/
```

**Dependency rules**
- `cli` imports only `core`.
- `core` imports `ir`, `differences`, `engine` and the plugin interface. It never imports boto3 or GCP code directly.
- Plugins import `ir` and `providers`. They never import `differences`.
- Difference rules import only `ir`.
- The future web backend replaces `cli` and calls the same `core`.

**Plugin interface** (`core/registry.py`):

```python
class Plugin(Protocol):
    name: str                                                          # "network"
    def collect_aws(self, session, region) -> dict[str, Any]: ...      # file name → raw JSON
    def normalize(self, snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]: ...
    def generate_gcp(self, resolved: IRDocument, ctx: GenerateContext) -> HclFile: ...
```

Plugins run in the order declared in the registry, with `network` first. Later plugins refer to earlier objects through `ctx.lookup(id)`.

**Libraries:** Typer, Pydantic v2, PyYAML, boto3, Rich, google-cloud-storage (state bucket only).

### 2.2 Project directory

```
.easymigrate/
├── project.yaml         # source account/region, target project/region, tool version
├── snapshot/aws/*.json  # output of scan
├── ir.json              # output of analyze: faithful IR
├── differences.json     # output of analyze: detected differences
├── decisions.yaml       # output of resolve: chosen strategy per difference (editable by hand)
├── resolved_ir.json     # output of plan: IR after the decisions were applied
├── report.md            # output of analyze, resolve and plan
├── report.json
├── terraform/*.tf       # output of plan
├── tfplan               # output of plan (binary)
├── plan.txt             # output of plan (human-readable)
└── plan.lock.json       # fingerprints of everything apply depends on
```

Each stage reads only the outputs of earlier stages. Once a scan is saved, later stages can be re-run offline.

### 2.3 CLI

```
easymigrate scan     --aws-profile X | --aws-role-arn ARN   --region ap-south-1
easymigrate analyze
easymigrate resolve  [--accept-defaults [--acknowledge-manual]]
easymigrate plan     --gcp-project Y [--gcp-region asia-south1]
easymigrate apply
```

If no `--gcp-region` is given, it comes from a region map (`ap-south-1 → asia-south1`, and so on). A source region that isn't in the map is an error asking for `--gcp-region`. The tool never falls back to a default region silently.

## 3. Intent-level IR

### 3.1 Base types (`ir/base.py`)

```python
class SourceRef(BaseModel):
    cloud: Literal["aws"]
    type: str            # "vpc", "subnet", "security_group", ...
    id: str              # native id or ARN
    region: str

class Finding(BaseModel):
    level: Literal["info", "unsupported", "scan_gap"]
    code: str            # e.g. "SCAN-ACCESS-DENIED", "GEN-RENAMED"
    message: str
    subject: str | None = None   # IR object id, if tied to one
    suggestion: str | None = None

class IntentObject(BaseModel):
    id: str              # "<kind>:<name>", unique within the document
    kind: str
    name: str
    labels: dict[str, str]
    source: SourceRef

class IRDocument(BaseModel):
    schema_version: Literal["1"]
    source: dict             # cloud, account_id, region, scanned_at
    objects: list[IntentObject]
    findings: list[Finding]  # scan gaps, unsupported inventory, info notes
```

Findings now cover only what the scan could see and what the tool supports. **Every way the source and target clouds differ is a `Difference` (§4), never a Finding.**

- **IDs** use the resource's `Name` tag, with disallowed characters replaced. If there is no `Name` tag, the native ID is used. Collisions get a numeric suffix.

### 3.2 Networking objects (`ir/network.py`)

| Kind | Built from (AWS) | Fields |
|---|---|---|
| `network` | VPC | `cidr_blocks`, `ipv6_cidr_blocks`, `dns_hostnames`, `dns_resolution` |
| `subnet` | Subnet + its route table + IGW/NAT routes | `network`, `cidr`, `ipv6_cidr`, `zone`, `internet: "public" \| "private_nat" \| "isolated"`, `map_public_ip` |
| `nat` | NAT gateway | `network`, `serves_subnets` |
| `firewall_policy` | Security group | `network`, `rules: list[FirewallRule]`, `applies_to` (native IDs of attached resources, filled in by later plugins) |
| `network_acl` | Non-default NACL | `network`, `subnets`, `rules` (number, direction, protocol, ports, cidr, allow/deny) |
| `custom_route` | Any route other than local, IGW or NAT | `network`, `destination`, `target_type` (`peering` \| `vpn` \| `tgw` \| `eni` \| `other`), `target_id` |

```python
class FirewallRule(BaseModel):
    direction: Literal["ingress", "egress"]
    protocol: Literal["tcp", "udp", "icmp", "all"]
    ports: list[str]        # "443", "8000-8080"; empty means all ports
    peer: CidrPeer | PolicyPeer | ExternalPolicyPeer | SelfPeer
    description: str | None
```

### 3.3 Normalization rules (faithful capture only)

- **Subnet `internet` value.** Look at the route table associated with the subnet, or the VPC's main route table if there is none:
  - a `0.0.0.0/0` route to an IGW → `public`
  - a `0.0.0.0/0` route to a NAT gateway → `private_nat`
  - neither → `isolated`
- **IGW and route tables** don't become IR objects. They are folded into `subnet.internet` and `custom_route`.
- **Security groups:** a rule that references a security group in the same VPC → `PolicyPeer`. A reference to another VPC or account → `ExternalPolicyPeer`.
- **NACLs:** the default allow-all NACL is omitted. Any other NACL → `network_acl`.
- The normalizer emits no judgements. Anything it can't represent at all → `info / INTERNAL-UNMODELLED` (strict mode raises).

## 4. Difference engine (core value)

### 4.1 Model (`differences/model.py`)

```python
class Strategy(BaseModel):
    id: str                  # "keep_1to1"
    title: str
    effect: str              # what the result looks like on GCP
    risk: Literal["none", "low", "medium", "high"]
    risk_note: str | None
    manual: bool = False     # True means EasyMigrate generates nothing; the user must act

class DifferenceRule(ABC):
    id: str                  # "NET-ZONE-REGIONAL", stable forever
    family: str              # "network"
    source: str = "aws"
    target: str = "gcp"
    title: str
    impact: str              # why this difference matters
    strategies: list[Strategy]
    default: str             # strategy id

    @abstractmethod
    def detect(self, ir: IRDocument) -> list["Difference"]: ...

    @abstractmethod
    def apply(self, ir: IRDocument, diff: "Difference", strategy_id: str) -> IRDocument: ...

class Difference(BaseModel):
    key: str                 # "<rule_id>@<subject id>", stable across re-analysis
    rule_id: str
    subjects: list[str]      # IR object ids affected
    context: dict            # values shown to the user (zone, cidrs, …)

class Decision(BaseModel):
    key: str
    strategy: str
    decided_by: Literal["default", "user"]
    acknowledged: bool = False   # required when the chosen strategy is manual
```

- **Rules are Python classes whose metadata is declared as class attributes.** `scripts/gen_differences_doc.py` renders `docs/differences.md` from that metadata, so docs and code can't drift apart.
- **`apply` is a pure function** from IR to IR. Strategies that change structure, such as merging subnets, rewrite the IR. Strategies that keep structure attach target hints (§4.3). Manual strategies mark the subjects as `excluded` with a reason.

### 4.2 Stages

1. **`analyze`:** run every registered rule's `detect` on the IR and write `differences.json`.
2. **`resolve`:**
   1. Load existing `decisions.yaml`.
   2. Add any new differences, defaulted. Previous user decisions are kept when their `key` still exists. Decisions whose difference no longer exists are **dropped and reported**.
   3. **Interactive mode:** for each difference show the title, affected objects, impact, and numbered strategies with effect and risk, with the default marked. Enter accepts the default. Every difference whose chosen strategy is `manual` needs an explicit acknowledgement ("I will handle this myself").
   4. **`--accept-defaults`:** choose every default without prompting, but **still stop** if any unacknowledged manual differences remain, and list them. `--accept-defaults --acknowledge-manual` acknowledges them too, and the report records that they were acknowledged in bulk.
   5. Users may also edit `decisions.yaml` by hand. It is validated on load.
3. **`plan`:** refuses if `decisions.yaml` is missing, stale (differences changed since resolve), or has unacknowledged manual decisions. Otherwise it applies the decisions in rule order to produce `resolved_ir.json`, then generates.

```yaml
# decisions.yaml
schema_version: "1"
decisions:
  - key: NET-ZONE-REGIONAL@subnet:migration-test-subnet-1
    strategy: keep_1to1
    decided_by: default
  - key: NET-NACL@network_acl:restricted-db-acl
    strategy: manual
    decided_by: user
    acknowledged: true
```

### 4.3 Resolved IR and target hints

`resolved_ir.json` has the same schema as the IR, with each object carrying two extra fields:
- **`target_hints: dict`:** instructions for the generator, for example `{"egress_ranges": ["10.0.0.0/16"]}`.
- **`resolution: list[{key, strategy}]`:** the differences that shaped this object. The generator writes these into the HCL comment headers and the report.

Generators read only the resolved IR and its hints.

### 4.4 Networking rule catalogue (AWS → GCP)

| Rule | Detects | Strategies (**default** first) |
|---|---|---|
| `NET-ZONE-REGIONAL` | any subnet (AWS subnets are single-zone; GCP subnets span the region) | **`keep_1to1`**: one GCP subnet per AWS subnet, same CIDRs, zone isolation lost at the subnet level (risk low) · `merge_per_network`: one regional subnet per network covering the smallest CIDR that contains them; IPs may change (risk high) |
| `NET-ISOLATION-VM-LEVEL` | an `isolated` subnet in a network that also has a `public` subnet (GCP routes are network-wide) | **`no_external_ip`**: isolation depends on resources in that subnet having no external IP; later plugins enforce this (risk medium) · `manual` |
| `NET-EGRESS-TAG` | an egress rule with a `PolicyPeer` (GCP egress rules can't target tags) | **`peer_attached_ranges`**: destination = CIDRs of the peer's attached resources, or the peer's whole network if not yet known (risk medium) · `manual` |
| `NET-SG-XVPC-REF` | `ExternalPolicyPeer` | **`manual`**: the rule is left out; recreate it after migrating the peer network |
| `NET-NACL` | `network_acl` | **`manual`**: not generated; stateless subnet ACLs have no GCP equivalent |
| `NET-PEERING` | `custom_route` with `target_type = peering` | **`manual`**: set up VPC peering after migrating |
| `NET-CUSTOM-ROUTE` | any other `custom_route` | **`manual`** |
| `NET-IPV6` | IPv6 CIDRs on a network or subnet | **`ipv4_only`**: migrate IPv4 only (risk medium) · `manual` |
| `NET-CIDR-OVERLAP` | two networks with overlapping CIDRs | **`keep`**: allowed on GCP because networks are isolated; connecting them later will conflict (risk none) |

Rules with a single strategy still appear in `resolve`, so the user sees and approves every difference.

## 5. IaC engine, generation, review and apply

### 5.1 OpenTofu inside the application (`engine/tofu.py`)

- The pinned OpenTofu version and the SHA-256 checksums for each OS and architecture ship inside the package.
- **First use:** download the official release archive for the current OS and architecture, **verify the checksum**, unpack it to `~/.cache/easymigrate/tofu/<version>/`, and reuse it after that. A checksum mismatch → hard error, and nothing is executed.
- The provider plugin cache is shared at `~/.cache/easymigrate/plugins/`, so providers download once.
- **Air-gapped override:** `EASYMIGRATE_TOFU_PATH` points to a local binary. Its version is still checked.
- The runner calls `tofu` with `-no-color -input=false`. It captures and streams output through Rich. Users never see or call `tofu` directly.
- Generated files stay standard HCL and also work with Terraform. `providers.tf` pins `hashicorp/google`, which OpenTofu resolves through its registry.

### 5.2 HCL builder (`providers/gcp/hcl.py`)

- A small builder for blocks, attributes, references and comments. It escapes strings correctly and writes output in a fixed, stable order. Generated files never use string templating.
- Every resource gets a comment header showing its origin and the decisions that shaped it:

```hcl
# from AWS subnet subnet-09125e7961d295170 (migration-test-subnet-1)
# decision NET-ZONE-REGIONAL → keep_1to1 (default)
resource "google_compute_subnetwork" "migration_test_subnet_1" {
  name          = "migration-test-subnet-1"
  ip_cidr_range = "10.0.1.0/24"
  region        = var.region
  network       = google_compute_network.migration_test_vpc.id
}
```

**Naming**
- Terraform address: the IR `id` cleaned to `[a-z0-9_]`, with a numeric suffix on collision.
- GCP name: `[a-z]([-a-z0-9]{0,61}[a-z0-9])?`. If a name has to change → `info / GEN-RENAMED`.

**Output files** in `.easymigrate/terraform/`: `providers.tf`, `backend.tf` (GCS), `variables.tf`, `terraform.tfvars`, and one `<plugin>.tf` per plugin.

### 5.3 Networking → GCP mapping (from resolved IR)

| Resolved IR | GCP Terraform |
|---|---|
| `network` | `google_compute_network`: `auto_create_subnetworks = false`, `delete_default_routes_on_create = true`, `routing_mode = "REGIONAL"` |
| `subnet` | `google_compute_subnetwork` |
| any `public` subnet in the network | one `google_compute_route` for `0.0.0.0/0` → `default-internet-gateway` |
| `nat` / subnets with `private_nat` | `google_compute_router` + `google_compute_router_nat` limited to those subnetworks |
| `firewall_policy` | a network tag `fw-<policy-name>`; one `google_compute_firewall` per (direction, peer kind) group, with `target_tags = [tag]`. `CidrPeer` → ranges. `PolicyPeer` (ingress) → `source_tags`. `SelfPeer` → own tag. Egress `PolicyPeer` → `target_hints.egress_ranges`. |
| AWS default allow-all egress rule | skipped, because GCP already allows all egress by default |
| objects marked `excluded` | nothing generated; listed in the report under *Needs your action* |

Attaching network tags to resources is the job of later plugins such as EC2 and EKS.

### 5.4 `plan`

1. Preflight:
   - OpenTofu is ready (downloaded or cached and verified).
   - GCP credentials are available.
   - The required APIs (`compute.googleapis.com`) are enabled. If not, report them with the command to enable them.
2. Check decisions (§4.2), then build `resolved_ir.json`.
3. Ensure the state bucket `<project>-easymigrate-tfstate` exists. If it is missing, **ask for confirmation**, then create it with versioning and uniform bucket-level access.
4. Generate `terraform/`.
5. Run `tofu init`, then `tofu plan -out=tfplan`, and save `plan.txt`.
6. Write `plan.lock.json` with the SHA-256 of `ir.json`, `decisions.yaml`, `resolved_ir.json`, every file in `terraform/`, and `tfplan`.
7. Print the summary and update the report.

### 5.5 `apply`

1. **Refuse** (exit 2) if `tfplan` is missing or `plan.lock.json` doesn't match the current files.
2. Print the summary again: decisions taken, *Needs your action* items, and add/change/destroy counts. Resources to be **changed or destroyed are highlighted**.
3. Prompt: `Apply these changes to project <X>? [y/N]`. Anything other than `y` or `yes` → exit 2. There is **no auto-approve flag**.
4. Run `tofu apply tfplan` and stream its output.
5. If it fails partway, state is safe in GCS, and re-running `plan` then `apply` continues from where it stopped.

## 6. Report and error handling

### 6.1 Report

It is shown in the terminal (Rich) and written as `report.md` and `report.json`.

- **Header:** source → target.
- **Coverage line:** counts of resources found, translated, handled by decision, needing action, and not yet supported, plus scan gaps.
- **Sections, most serious first:**
  1. **Scan gaps**
  2. **Needs your action:** manual decisions, with what to do
  3. **Decisions:** each difference, the chosen strategy, default or user, and the risk
  4. **Not yet supported**
  5. **Translated**

  `info` findings appear only in the md and json files.
- **Unsupported inventory:** during `scan`, the Resource Groups Tagging API (`GetResources`) lists resources of every service. Types with no plugin → `unsupported`. The report states that untagged resources of unsupported services may be missing.

### 6.2 Error handling

| Situation | Behaviour |
|---|---|
| Missing or invalid credentials, unknown region | Stop with a clear message and the fix; exit 1 |
| `AccessDenied` on one API | `scan_gap` finding (with the IAM action), and the scan continues |
| Throttling | botocore `adaptive` retry mode; paginators on every list call |
| Unexpected data in the normalizer | `info / INTERNAL-UNMODELLED`, and the run continues. In strict mode (tests) it raises. |
| A rule's `detect` or `apply` raises | Stop with the rule ID and the error (exit 1). A broken rule must never quietly produce different infrastructure. |
| OpenTofu download fails or checksum mismatch | Stop; suggest `EASYMIGRATE_TOFU_PATH` for offline use |
| OpenTofu error | Show its output as-is, plus a hint for known causes (API disabled, missing permission) |
| A stage's input is missing or stale | Name the command to run first; exit 1 |

**Exit codes:**
- `0` success
- `1` error
- `2` the user declined, or apply refused because of a lock mismatch or unresolved decisions

### 6.3 Protecting sensitive data

- Credentials are never written to `.easymigrate/`.
- If the working directory is inside a git repo, `.easymigrate/` is added to `.gitignore` on the first run.
- The CLI warns once that `.easymigrate/` holds account metadata.

## 7. Testing

1. **Normalizer golden tests.** `tests/fixtures/aws/network/<case>/snapshot/*.json` → compare with `expected_ir.json`. Cases:
   - `basic_public`
   - `private_with_nat`
   - `sg_references_sg`
   - `custom_nacl`
   - `peering_route`
   - `multi_vpc`
   - `ipv6`
   - `external_sg_ref`

   Tests run in strict mode. `pytest --update-golden` regenerates the expected files for review in git.
2. **Difference rule tests (the core value, held to the highest bar).** For every rule:
   - **detect:** positive and negative IR fixtures → expected differences
   - **apply:** every strategy → expected resolved IR
   - **metadata:** the default is one of the strategies; the ID is unique; every strategy has an effect and a risk

   A CI check fails if `docs/differences.md` is out of date with the rule metadata.
3. **Decision tests.**
   - defaults are filled in
   - user decisions survive re-analysis when the key still exists
   - orphaned decisions are dropped and reported
   - `plan` refuses unacknowledged manual decisions
   - hand-edited `decisions.yaml` is validated
4. **Generator golden tests.** Resolved IR → expected `.tf` files. HCL builder unit tests cover escaping, ordering and collisions.
5. **Collector tests.** `moto` provides fake AWS resources. botocore `Stubber` injects `AccessDenied` responses for `scan_gap` handling.
6. **IaC validity.** `tofu init -backend=false && tofu validate` on every generated golden output.
7. **Engine tests.** Checksum verification rejects a tampered archive. The cache is reused. `EASYMIGRATE_TOFU_PATH` overrides the download.
8. **Pipeline tests.** moto → `scan` → `analyze` → `resolve --accept-defaults --acknowledge-manual` → `plan`, with a fake runner. `apply` is tested for:
   - refusing on a lock mismatch
   - exit 2 when the user declines
   - no way to bypass the prompt
9. **Live test (manual, never in CI).** Create `testbed/aws`, run the full pipeline into a scratch GCP project, check the result, destroy both sides.

**Testbed:** `main.tf` moves to `testbed/aws/` and gains a private subnet with a NAT gateway, two security groups that reference each other, and a custom NACL. A NAT gateway is billed hourly, so create the testbed only while testing.

**Tooling**
- pytest and ruff.
- GitHub Actions runs lint, unit tests, `tofu validate` and the difference-docs check on every push. OpenTofu and provider downloads are cached.

## 8. Migrating from the prototype

- `scanner.py`, `mapper.py` and `compiler.py` move to `prototype/`, unchanged. They are deleted as the RDS and EKS plugins replace them.
- `main.tf` and `.terraform.lock.hcl` move to `testbed/aws/`. Local `terraform.tfstate` and `.terraform/` must be moved by hand along with them, because they aren't tracked by git; otherwise the live testbed's state is lost.

## 9. Done when

- On the extended testbed, `easymigrate scan → analyze → resolve → plan → apply` creates the matching GCP network, subnets, NAT and firewall rules in a scratch project, with no Terraform or OpenTofu installed on the machine beforehand.
- `resolve` presents every networking difference from §4.4 that the testbed triggers. Changing `NET-ZONE-REGIONAL` to `merge_per_network` produces a different, valid plan.
- The report shows the decisions, the *Needs your action* items (for example the custom NACL), and RDS and EKS as not yet supported.
- All tests pass in CI, including `tofu validate` on every golden output and the difference-docs check.
