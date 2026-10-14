# EasyMigrate

Migrates cloud infrastructure from AWS to GCP. It scans the live source cloud, builds an intent-level intermediate representation, shows every difference between the clouds with safe default handling, generates Terraform, and applies it only after you approve.

## Install

Python 3.12 or newer. From a checkout or a release wheel:

    uv tool install ".[ui]"          # from the repository root
    uv tool install "easymigrate-0.1.0-py3-none-any.whl[ui]"

`[ui]` adds the local review UI; leave it out for the CLI alone. `easymigrate --version`
prints the version; changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Usage

    # 1. Read a live AWS region (read-only access is enough)
    easymigrate scan --region ap-south-1 --aws-profile my-readonly-profile
    #    or: easymigrate scan --region ap-south-1 --aws-role-arn arn:aws:iam::123456789012:role/EasyMigrateScan

    # 2. Turn the scan into the intent-level IR and detect AWS→GCP differences
    #    (.easymigrate/ir.json, .easymigrate/differences.json)
    easymigrate analyze

    #    A scan reads the whole region. To migrate only part of it, scope the analysis
    #    (saved with the project; re-run analyze to change it, --clear-scope to remove):
    easymigrate analyze --vpc vpc-0abc123          # only this VPC and what runs in it
    easymigrate analyze --tag app=shop             # only resources carrying this tag
    easymigrate analyze --exclude i-0123456789     # leave one resource out
    #    Everything left out is listed in the report. --vpc does not apply to buckets,
    #    which belong to no VPC; narrow those with --tag or --exclude.

    # 3. Choose a strategy per difference (safe defaults preselected) and save
    #    .easymigrate/decisions.yaml
    easymigrate resolve
    #    or, to accept every default and stop only on unacknowledged manual differences:
    easymigrate resolve --accept-defaults
    #    or to also acknowledge those in bulk:
    easymigrate resolve --accept-defaults --acknowledge-manual

    # 4. Generate GCP Terraform from the resolved decisions and run `tofu plan`
    #    (.easymigrate/terraform/, .easymigrate/plan.txt, .easymigrate/plan.lock.json)
    easymigrate plan --gcp-project my-gcp-project
    #    or, when the AWS region has no built-in GCP mapping:
    easymigrate plan --gcp-project my-gcp-project --gcp-region asia-south1
    # A migrated database needs a password: AWS never returns the original, and
    # EasyMigrate never invents one. Supply it by flag, by EASYMIGRATE_DB_PASSWORD_<SLUG>,
    # or at the prompt when you are on a terminal.
    easymigrate plan --gcp-project my-gcp-project --db-password database_instance:prod-db=s3cret

    # 5. Review the plan and apply it (prompts for confirmation; --yes off a terminal)
    easymigrate apply

    # Undo: delete everything EasyMigrate created on GCP. AWS is never touched.
    easymigrate destroy [--remove-protection] [--delete-state-bucket]

`destroy` deletes only what is in EasyMigrate's own Terraform state, after listing it and asking. Deletion protection carried over from AWS (RDS `DeletionProtection`, EC2 termination protection) stops it until you pass `--remove-protection`. It never deletes data: a Cloud Storage bucket that holds objects makes it stop and say so. The private services peering that private-IP Cloud SQL leaves behind, which `tofu destroy` cannot remove, is removed for you.

These commands work in the current directory and keep their output in `.easymigrate/`. That folder holds account metadata such as account IDs and ARNs; keep it private. It is added to `.gitignore` automatically inside a git repository. After every stage that has enough information, a report is written to `.easymigrate/report.md` and `.easymigrate/report.json`.

## What it migrates

| AWS | Google Cloud |
|---|---|
| VPCs, subnets, route tables, internet/NAT gateways, security groups, network ACLs | VPC network, subnets, routes, Cloud NAT, firewall rules |
| RDS instances and Multi-AZ clusters (MySQL, PostgreSQL) | Cloud SQL with private IP, flags, HA and backups |
| EC2 instances, EBS volumes, Elastic IPs, launch templates, Auto Scaling groups | Compute Engine VMs, disks, static addresses, instance templates, managed instance groups and autoscalers |
| EKS clusters and node groups | GKE Standard clusters and node pools |
| S3 bucket configuration (not objects) | Cloud Storage buckets |
| Application and Network Load Balancers | Cloud Load Balancing |
| IAM roles and instance profiles | A report with suggested predefined roles (nothing is created) |

Infrastructure configuration only: EasyMigrate never copies data (database rows, disk
contents, bucket objects) and never changes anything in AWS. Every AWS→GCP difference it
finds is listed with a safe default and the alternatives, in `.easymigrate/report.md`.

## Permissions

**AWS (scan):** read-only. The least-privilege policy is in
[`docs/aws-scan-policy.json`](docs/aws-scan-policy.json); the AWS managed `ReadOnlyAccess`
policy also works. A missing permission becomes a scan gap in the report rather than a failed
scan.

**Google Cloud (plan, apply, destroy):** Application Default Credentials
(`gcloud auth application-default login`) for a principal with, on the destination project,
`roles/storage.admin` (the `<project>-easymigrate-tfstate` state bucket and migrated buckets),
`roles/compute.admin` (networks, VMs, load balancers) and, when the scan found them,
`roles/cloudsql.admin` + `roles/servicenetworking.networksAdmin` (RDS) and
`roles/container.admin` + `roles/iam.serviceAccountUser` (EKS). `plan` checks that the needed
APIs are enabled and names the `gcloud services enable` command if not. EasyMigrate runs
OpenTofu itself; you never install or invoke `tofu`.

## Local UI (preview)

    uv sync --extra ui
    easymigrate ui

Starts a local web server that drives the same five stages as the CLI, in a browser,
against the `.easymigrate/` folder in the current directory — the CLI and UI can be used
interchangeably on one project. It binds `127.0.0.1` only (there is no flag to change
this: exposing it on a network would let anyone on it apply infrastructure), opens the
browser at a URL carrying a per-run token, and requires that token on every mutating
request so a page from another site can never drive it. Use `--port` to pick a fixed
port instead of an ephemeral one, and `--no-browser` to print the URL without opening it.
`easymigrate ui` requires the optional `ui` extra (FastAPI, uvicorn); every other command
works without it.

## Development

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .

### Frontend

The local UI's frontend is a small Vite/React/TypeScript app in `frontend/`. Its build
output is committed to `src/easymigrate/ui/static/` and shipped in the wheel, so
`easymigrate ui` works from a plain `pip`/`uv` install with no node toolchain on the
user's machine — node is only needed to change the UI itself.

    cd frontend
    npm install
    npm run dev      # local dev server against a running `easymigrate ui` backend
    npm run build     # outputs into ../src/easymigrate/ui/static/ — commit the result
    npm run lint
    npm test

CI fails if the committed `static/` output has drifted from a fresh `npm run build`
(the `frontend` job in `.github/workflows/ci.yml`), so always rebuild and commit after
changing anything under `frontend/src/`.

Saved project files carry a schema version (`easymigrate/ir/versioning.py`). Changing a model
they are read into fails `tests/ir/test_schema_version.py` until the version is bumped, with a
migration step when old files can be carried forward.

Before a customer engagement, check the built-in Google Cloud facts against a live project:

    uv run python scripts/verify_gcp_facts.py --project my-gcp-project

Design documents are in `docs/superpowers/specs/`, one per family, starting with
`2026-09-15-core-pipeline-networking-design.md`.

## Licence

Proprietary; see [LICENSE](LICENSE).
