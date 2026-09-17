# EasyMigrate — orientation for an AI agent

Written for an agent picking this repo up cold. It explains what the product is, why it is
built this way, what actually exists today, and where the sharp edges are. The `README.md`
covers *how to run it*; this file covers *how to think about it*.

Last updated 2026-09-17, after spec 2 (RDS → Cloud SQL) was built and proven live.

---

## 1. What the product is

EasyMigrate migrates **cloud infrastructure** from AWS to GCP. A user points it at a live
AWS region; it produces Terraform (OpenTofu) that recreates the equivalent infrastructure
in a GCP project, and applies it only after the user approves.

It is a startup MVP, not a research project. The founder is the only developer. Decisions
favour shipping a narrow thing that genuinely works over a broad thing that demos.

**What it is not:**

- **Not a data migration tool.** Databases are created empty. Moving rows is a later,
  paid, assisted service. Do not add data-copying features without an explicit decision.
- **Not a Terraform importer.** It reads the *live cloud*, not the customer's existing IaC.
  Customers often have no IaC, or IaC that has drifted; live state is the only truth.
- **Not multi-cloud.** AWS → GCP only, in one direction, until that is solid.

### The commercial thesis

Infrastructure migration is the wedge: it is lightweight, automatable, and the part
customers dread. The real difficulty — and the product's moat — is **handling the places
where the two clouds genuinely differ**. Anyone can map a VPC to a VPC. The value is in
knowing that GCP firewall rules cannot target tags in egress, that Cloud SQL has no concept
of a security group, that an AWS subnet is zonal while a GCP subnet is regional — and in
presenting each of those as a decision the user can understand and approve.

That is why the **difference engine is a first-class core component**, not a per-plugin
afterthought.

---

## 2. The pipeline

Five CLI stages, each reading only the outputs of earlier ones. Once a scan is saved,
every later stage runs offline.

```
scan      live AWS APIs          → .easymigrate/snapshot/aws/*.json
analyze   snapshot               → ir.json (faithful IR) + differences.json
resolve   differences            → decisions.yaml (a strategy per difference)
plan      IR + decisions         → resolved_ir.json + terraform/*.tf + tfplan
apply     tfplan                 → real GCP resources (after explicit confirmation)
```

The separation that matters most:

| Component | Knows about | Must not know about |
|---|---|---|
| **Collector** | AWS APIs | GCP |
| **Normalizer** | AWS shapes → IR | GCP, translatability |
| **Difference engine** | how AWS and GCP differ | AWS SDKs, HCL |
| **Generator** | resolved IR → GCP HCL | AWS, difference logic |

This is enforced by `tests/test_dependency_rules.py`. If you find yourself wanting GCP
knowledge in a normalizer, or difference logic in a generator, the design is telling you
the change belongs somewhere else.

### Why "intent-level" IR

The IR describes **what the infrastructure does**, not what AWS calls it. A subnet's IR
does not record "has a route table with a 0.0.0.0/0 route to an IGW"; it records
`internet: "public"`. Route tables and internet gateways never become IR objects at all —
they are folded into the properties they imply.

This is what makes cross-cloud mapping tractable: the generator asks "is this subnet
public?", not "which of AWS's seven routing constructs produced this?".

---

## 3. The difference engine (read this part twice)

Everything the two clouds disagree about flows through one mechanism.

- A **`DifferenceRule`** has a stable id (`NET-ZONE-REGIONAL`, `DB-TIER`), a title, an
  impact statement, and a tuple of **strategies**.
- A **`Strategy`** has an id, an effect description, a **risk level** (`none`/`low`/
  `medium`/`high`), and a `manual` flag. `manual` means EasyMigrate generates nothing and
  the user must act — and it always requires explicit acknowledgement.
- `rule.detect(ir)` returns **`Difference`** objects, each keyed `<RULE-ID>@<subject-id>`.
- The user picks a strategy per difference in `resolve`, stored as **`Decision`** objects in
  `decisions.yaml` (hand-editable, validated on load).
- `rule.apply(ir, diff, strategy, params)` is a **pure function** IR → IR. It either
  rewrites structure, attaches **`target_hints`** for the generator, or marks the subject
  `excluded`.
- The result is **`resolved_ir.json`**: the same schema plus `target_hints` and a
  `resolution` list recording which decisions shaped each object.

Two invariants to preserve:

1. **Findings vs Differences.** A `Finding` describes what the *scan* could see and what
   the *tool* supports (`SCAN-ACCESS-DENIED`, `UNSUPPORTED-RESOURCE`). Every way the two
   *clouds* differ is a `Difference`. Do not blur these — a difference the user can decide
   about must never be demoted to a log line.
2. **Rules are pure and deterministic.** `detect` and `apply` never mutate input; the
   engine re-validates the document after each `apply`; differences sort by key; rules
   apply in catalogue order.

### Per-difference strategy sets

Some rules offer different strategies depending on the data. A Postgres 9.6 instance gets
an upgrade pair; a modern one gets the same-major pair. This is handled by overriding
`default_strategy(diff)` and `allowed_strategies(diff)` — **not** by validating inside
`apply`. Five rules do this today. If you add a sixth and forget, the CLI will offer a
strategy the difference disallows and `resolve_ir` will crash later; that exact bug has
been introduced and caught twice.

### Decision params

A decision can carry user-editable values (`Decision.params`), validated by the owning
rule via `default_params` / `validate_params`. Introduced because `DB-PRIVATE-IP` must
choose an IP range for Private Services Access and the user must be able to override it.
Interactive `resolve` **shows** params but never prompts for them — the user edits
`decisions.yaml`.

### No silent mapping

Every IR field a generator could touch must be declared in that generator's
`FIELD_COVERAGE` as `exact` / `metadata` / `rule:<ID>` / `hint:<key>` / `not_generated:<why>`
/ `derived:<why>`. A test fails on any undeclared field. This exists so that an unhandled
cross-cloud difference surfaces in CI rather than at a customer. Adding an IR field without
declaring its fate is a build failure, by design.

---

## 4. What exists today

Two resource families, both proven end to end against real AWS and GCP accounts.

### Networking (spec 1, on `main`)

VPCs, subnets, route tables, internet and NAT gateways, security groups, network ACLs.
Eleven `NET-*` rules. Proven live 2026-09-16.

### Databases (spec 2, branch `spec2-databases`, **unmerged**)

RDS Postgres and MySQL → Cloud SQL, including read replicas and the private-IP path.
Twenty `DB-*` rules covering engine version, tier sizing, connectivity, HA, storage,
backups, PITR, maintenance windows, parameter flags, encryption, IAM auth, replicas.
Proven live twice on 2026-09-17.

Deliberately out of scope: Aurora, MariaDB, Oracle, SQL Server, Db2, RDS Custom — each
reported as *not yet supported* inventory, with a reason.

### Roadmap order

Networking → **RDS** → EKS → EC2 → S3 (config only) → ALB/NLB, IAM (report only).

**Depth-first is a rule, not a preference:** each family is finished completely —
collection, faithful IR, a full difference catalogue with tested strategies, generation,
and a live run — before the next begins. No thin demo slices across families.

---

## 5. Code layout

```
src/easymigrate/
├── cli.py                     Typer CLI; imports only core
├── core/
│   ├── pipeline.py            the five stages
│   ├── project.py             reads/writes .easymigrate/
│   ├── registry.py            composition root — the ONLY core module that may
│   │                          import plugins, providers or the OpenTofu engine
│   ├── plugin.py              Plugin protocol + Collect/Normalize/Generate contexts
│   └── report.py              terminal, markdown and json report
├── ir/                        base.py, network.py, database.py — pydantic, stdlib only
├── differences/
│   ├── model.py               Strategy, DifferenceRule, Difference, Decision
│   ├── engine.py              detect(), resolve_ir(), catalogue()
│   ├── decisions.py           merge, validate, YAML round-trip, readiness
│   └── rules/aws_gcp/         network.py (11 rules), database.py (20),
│                              cloudsql_facts.py (Cloud SQL limits and versions)
├── engine/tofu.py             downloads, verifies and runs OpenTofu in-app
├── plugins/<family>/          collect_aws.py, normalize.py, generate_gcp.py, aws_values.py
└── providers/
    ├── aws/source.py          boto3 session, inventory
    └── gcp/                   hcl.py (HCL builder), naming.py, state_bucket.py, services.py
```

**Dependency rules:** `cli` → `core` only. `core` → `ir`, `differences`, `engine`, plugin
protocol; never boto3 or google SDKs directly (it reaches GCP through the `GcpTarget` seam
in `registry.py`, which tests monkeypatch). Plugins → `ir`, `providers`; never
`differences`. Difference rules → `ir` only.

**Generated docs** — `docs/differences.md` and `docs/field-coverage*.md` are rendered from
code by `scripts/gen_differences_doc.py`. A test fails if the committed copy drifts. Never
hand-edit them.

---

## 6. Non-obvious decisions and their reasons

| Decision | Why |
|---|---|
| **OpenTofu, embedded and downloaded by the app** | Terraform is BSL-licensed; OpenTofu is MPL and safe to embed commercially. Customers never install or run it themselves. |
| **State in a GCS bucket in the customer's own project** | The customer owns their state. EasyMigrate creates the bucket after asking. |
| **Passwords supplied at plan time, never generated** | AWS never returns a master password. An earlier decision (generate one, store in Secret Manager) was overruled: EasyMigrate must never invent a credential. Supplied via `--db-password`, env var, or TTY prompt; written to a 0600 `secrets.auto.tfvars.json`. |
| **No `google_project_service` resources** | Their `disable_on_destroy` default would turn a migration teardown into a project-wide outage. `plan` checks required APIs and prints the exact `gcloud` command instead. |
| **Names derive from the IR id, never the display name** | Display names collide and change. A renamed resource emits a `GEN-RENAMED` finding rather than silently colliding. |
| **`GenerationError` in generators** | Generators hold no difference logic, so meeting something a rule should have handled means a rules bug. Failing loudly there stops a rules bug from becoming wrong infrastructure. |

---

## 7. Testing

`uv run pytest` — 863 tests. `uv run pytest -m tofu` — 10 more that run real
`tofu init`/`validate` against Google's provider schemas (network-dependent, deselected by
default).

Layers: moto-backed collector tests; hand-built snapshot fixtures with golden
`expected_ir.json` and golden `expected_<family>.tf`; per-rule tests (detect positive,
detect negative, every strategy's `apply`, input-unchanged); field-coverage completeness;
CLI tests via Typer's `CliRunner`.

**Two hard-won lessons about tests here:**

1. **Reviews repeatedly found tests that passed while asserting nothing** — a pagination
   test that never paginated, a "no extra API call" test watching a client the code never
   used, a filter test that stayed green when the filter was deleted. When adding a test,
   revert the behaviour and confirm it fails. Inspection is not enough.
2. **Golden files must be written from the spec, not recorded from output.** A golden
   dumped from the implementation proves only that the implementation is stable.

### Live testing

`tests/live/README.md` is the procedure. It creates billed resources on both clouds and is
run by hand, rarely, and torn down the same day. It has repeatedly found what unit tests
cannot — see §8.

---

## 8. What live runs found that tests did not

Worth internalising, because it predicts where the next family will break.

- **Real RDS returns `StorageThroughput: 0` for `gp2`** rather than omitting the field. The
  IR forbids provisioned throughput on gp2, so `analyze` failed outright on an ordinary
  database. Every one of nine hand-built fixtures had omitted the field. **Fixtures encode
  our idea of an API response, not the API.**
- **A security group whose ingress references another security group yields no authorized
  networks**, so a public RDS instance migrated to a Cloud SQL instance with a public IP
  and an empty allow-list — reachable by nobody. Confirmed by connecting and being refused.
  Now fixed: that case defaults to `manual` and appears under "Needs your action".
- **`publicly_accessible` RDS requires an internet gateway attached to its VPC**, and RDS
  validates before the attachment propagates — so the first apply races and must be re-run.
- **A deleted Cloud SQL instance name cannot be reused for about a week** (what
  `DB-NAME-REUSE` warns about).
- **A partially-failed `tofu destroy` strands resources** that are dropped from state but
  left in GCP, so the next `apply` fails with `409 already exists` on each in turn.
- **Cloud SQL takes 10–17 minutes to create** an instance, and the Private Services Access
  connection often refuses to delete for hours afterwards ("producer services are still
  using this connection").

---

## 9. Known gaps

**Before real customers:**

- `_parameters()` in the database normalizer reads only the first of an instance's
  `DBParameterGroups`, while the collector fetches all of them. Extra groups' values are
  dropped before `DB-PARAM-FLAGS` sees them — neither translated nor reported.
- `cloudsql_facts.py` (supported versions, flag allow-list, tier band, limits) is **model
  knowledge as of 2026-09-16, never fetched from Google's docs.** A wrong version enum
  fails at apply. Verify before customers.
- Untagged AWS resources are invisible to the inventory; scans cannot be scoped to
  particular VPCs.

**Product-shaped, not bugs:**

- There is no `easymigrate doctor` to reconcile orphans after a failed destroy.
- Firewall policies attached only to VMs generate with an empty `applies_to` (network
  tags attach to VMs, which the EC2 family will introduce).

---

## 10. Working conventions

- **Discuss before coding.** Specs live in `docs/superpowers/specs/`, implementation plans
  in `docs/superpowers/plans/`. Plans are lean: interfaces, rules and test cases, never
  pre-written code. The spec is the binding authority; a plan argues from it.
- **The user is cost-conscious about tokens.** Prefer few large units of work over many
  small dispatches, and one review at the end over a review per task.
- Commits are authorized freely; force-pushing is not.
- `uv` may not be on `PATH` — use `/home/sudhan/.local/bin/uv run ...`.
- A knowledge graph of this repo lives in `graphify-out/`. Prefer
  `graphify query "<question>"` over grepping when orienting. Rebuild with
  `graphify update .` after changing code (AST-only, no API cost).
