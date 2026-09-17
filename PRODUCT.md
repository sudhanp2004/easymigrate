# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

FastAPI backend + React (Vite) single-page app, chosen by the user on 2026-09-17 over a
server-rendered alternative. The backend reuses the existing Python engine directly; it is
not a rewrite.

Existing engine, unchanged by the UI work: Python 3.13, Typer CLI, Pydantic v2, boto3,
OpenTofu downloaded and run by the application itself. `uv` manages the environment.

## Users

**Primary: a cloud or platform engineer at the customer company, working self-serve.**
They run EasyMigrate locally on their own machine, using their own AWS and GCP
credentials. They are technical and comfortable with infrastructure, but are not expected
to know how GCP differs from AWS — teaching them that, safely, is the product's job.

Their job: move an existing AWS environment to GCP without discovering the differences the
hard way, in production.

They are usually migrating infrastructure they did not originally build, so the tool must
explain what it found as well as what it will do.

## Product Purpose

EasyMigrate reads a live AWS region, builds a cloud-neutral description of what the
infrastructure *does*, identifies every place AWS and GCP genuinely differ, lets the user
choose how each difference is handled, generates OpenTofu, and applies it only after
explicit approval.

Success is a migration the user understands and trusts: no resource silently dropped, no
security posture quietly changed, no surprise at `apply` time.

Explicitly **not** a data migration tool — databases are created empty. Moving data is a
separate, later, assisted service. Not a Terraform importer: it reads live cloud state,
because customers' existing IaC is frequently absent or drifted.

## Positioning

The mechanism a neighbouring product could not truthfully copy is the **difference
engine**. Mapping a VPC to a VPC is commodity. EasyMigrate's value is that it knows GCP
firewall rules cannot target tags in egress, that Cloud SQL has no concept of a security
group, that an AWS subnet is zonal while a GCP subnet is regional — and it turns each of
those into a named difference with strategies, a risk level, and a safe default the user
approves or overrides.

Two rules give that engine its credibility, and both are load-bearing product promises:

- **Nothing is silently dropped.** Anything untranslatable is shown as untranslatable.
- **The tool never invents a credential.** AWS does not return database passwords, so the
  user supplies one; EasyMigrate will not generate one on their behalf.

## Operating Context

Runs on the engineer's own machine. Credentials stay local — AWS read-only access for the
scan, GCP credentials for the destination project. This locality is a deliberate trust
property, not an implementation detail: the tool needs *write* access to the customer's
cloud, and never holding those credentials is materially easier to accept.

The work is stage-by-stage and resumable, not one long command. State lives in a
`.easymigrate/` folder in the working directory: the raw scan, the IR, the detected
differences, the user's decisions, the generated Terraform, the plan, and a written report.
Terraform state lives in a GCS bucket in the customer's own project.

**Operations are slow and uneven, and the interface must assume it.** A scan takes
seconds; `plan` about a minute; `apply` can exceed twenty minutes — a single Cloud SQL
instance took 16m44s in a live run. Apply streams output as it goes and must survive the
user closing the page.

Migrations are also **iterative**: a user re-runs stages as they change decisions, and a
failed `apply` is resumed rather than restarted.

## Capabilities and Constraints

**Five stages, all of which the UI will cover:** `scan` → `analyze` → `resolve` → `plan` →
`apply`. The engineer should not need the terminal after launching the UI.

**Shipped resource families:** networking (VPCs, subnets, route tables, internet and NAT
gateways, security groups, network ACLs) and databases (RDS Postgres/MySQL → Cloud SQL,
including read replicas and the private-IP path). Thirty-one difference rules across the
two.

**Roadmap families, not yet built:** EKS, EC2, S3 (configuration only), load balancers,
and IAM (reported only). Each is finished completely before the next begins, so the UI
must tolerate the set of resource kinds growing over time without redesign.

**Terminology the interface must use consistently, because the engine does:**

- **Scan / snapshot** — raw AWS API responses saved to disk.
- **IR** — the cloud-neutral, intent-level description. Faithful: it records what exists,
  including things GCP cannot do.
- **Difference** — a specific place AWS and GCP disagree, with a stable id like
  `DB-PRIVATE-IP`, an impact statement, and two or more strategies.
- **Strategy** — one way to handle a difference, carrying a **risk** level of none / low /
  medium / high, and sometimes flagged **manual**, meaning EasyMigrate generates nothing
  and the user must act.
- **Decision** — the user's chosen strategy for a difference, optionally with **params**
  (for example the IP range for a Private Services Access connection).
- **Finding** — what the scan could see and what the tool supports; distinct from a
  difference. Levels: info, unsupported, scan gap.
- **Resolved IR** — the IR after decisions are applied; what generation consumes.

**Constraints the UI inherits:**

- A `manual` strategy requires explicit acknowledgement before `plan` will run.
- Decision params currently cannot be entered interactively at all — the user hand-edits
  `decisions.yaml`. Removing that limitation is one of the clearest reasons this UI exists.
- `apply` requires explicit confirmation; there is deliberately no auto-approve flag.
- Some differences are only decidable with information the scan cannot see, so the
  interface must let a user record "I will handle this myself" without pretending it was
  migrated.
- Shipping a React SPA inside a Python package means built assets must be produced at
  release time and included in the wheel; `easymigrate ui` must work from a plain
  `pip`/`uv` install with no node toolchain on the user's machine.

**Undecided, deliberately:** whether the UI is ever hosted for customers. The engine was
built so a web backend could reuse it, but hosting means custody of customers' cloud
credentials and is not committed to.

## Brand Commitments

The name **EasyMigrate**. Nothing else is fixed — no logo, wordmark, palette, typeface or
visual identity exists yet, and none has been promised to anyone.

The engine's existing voice is plain, specific and non-alarmist: it states what happened
and what it will do, names the resource, and suggests the next action. Report text says
things like "public IP, reachable from no authorized networks, so unreachable" rather than
softening them. Future interface copy should match that register.

## Evidence on Hand

**Pre-customer.** No paying customers, design partners, testimonials, case studies, press,
logos or usage figures exist. Nothing downstream may invent them.

What is real and can be shown:

- A working product: 863 automated tests, plus 10 that validate generated Terraform
  against Google's real provider schemas.
- Verified live migrations against the founder's own AWS and GCP accounts — networking on
  2026-09-16, databases on 2026-09-17, including a private-IP Cloud SQL instance reachable
  over a Private Services Access peering, confirmed by connecting to it.
- Real generated artifacts that can be shown as-is: `report.md`, `decisions.yaml`,
  generated `.tf` files, and the difference catalogue in `docs/differences.md`.
- Honest failure data from those live runs — for example, a public database that migrated
  into an unreachable Cloud SQL instance, which then changed the product's defaults.

## Product Principles

1. **Never silently drop or silently change.** Anything untranslatable is shown as
   untranslatable; anything that alters security posture says so before it happens.
2. **The user approves; the tool never assumes.** Safe defaults are offered, never applied
   without consent. Destructive or exposing choices are surfaced, not buried.
3. **Explain the difference, not just the mapping.** The user is not expected to know GCP.
   Every difference carries why it matters and what each strategy actually does.
4. **Credentials and state stay with the customer.** Local execution is a trust property
   of the product, not an implementation convenience.
5. **Truth over reassurance.** The product reports what it found and what failed, in plain
   words, including when the answer is unwelcome.

## Accessibility & Inclusion

No product-specific requirement has been established yet. The users are professional
engineers on desktop machines; no standard has been committed to.
