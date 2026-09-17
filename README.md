# EasyMigrate

Migrates cloud infrastructure from AWS to GCP. It scans the live source cloud, builds an intent-level intermediate representation, shows every difference between the clouds with safe default handling, generates Terraform, and applies it only after you approve.

## Usage

    # 1. Read a live AWS region (read-only access is enough)
    easymigrate scan --region ap-south-1 --aws-profile my-readonly-profile
    #    or: easymigrate scan --region ap-south-1 --aws-role-arn arn:aws:iam::123456789012:role/EasyMigrateScan

    # 2. Turn the scan into the intent-level IR and detect AWS→GCP differences
    #    (.easymigrate/ir.json, .easymigrate/differences.json)
    easymigrate analyze

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

    # 5. Review the plan and apply it (prompts for confirmation; no auto-approve flag)
    easymigrate apply

These commands work in the current directory and keep their output in `.easymigrate/`. That folder holds account metadata such as account IDs and ARNs; keep it private. It is added to `.gitignore` automatically inside a git repository. After every stage that has enough information, a report is written to `.easymigrate/report.md` and `.easymigrate/report.json`.

Scanned families: networking (VPCs, subnets, route tables, internet/NAT gateways, security groups, network ACLs) and databases (RDS instances and Multi-AZ DB clusters).

Permissions `scan` needs: `ec2:DescribeVpcs`, `ec2:DescribeVpcAttribute`, `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`, `ec2:DescribeInternetGateways`, `ec2:DescribeNatGateways`, `ec2:DescribeSecurityGroups`, `ec2:DescribeNetworkAcls`, `rds:DescribeDBInstances`, `rds:DescribeDBSubnetGroups`, `rds:DescribeDBParameterGroups`, `rds:DescribeDBParameters`, `rds:DescribeDBClusters`, `ec2:DescribeInstanceTypes`, `tag:GetResources`, `sts:GetCallerIdentity`. Missing permissions are reported as scan gaps rather than failing the scan.

`plan` and `apply` need GCP Application Default Credentials (`gcloud auth application-default login`) for a principal with, on the destination project: `roles/compute.networkAdmin` (create the generated networking resources) and `roles/storage.admin` (create and use the `<project>-easymigrate-tfstate` state bucket), plus `roles/cloudsql.admin` and `roles/servicenetworking.networksAdmin` when the scan found RDS instances. EasyMigrate runs OpenTofu inside the application; you never install or invoke `tofu` yourself.

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

Design: `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md`,
`docs/superpowers/specs/2026-09-17-local-ui-design.md` for the local UI.
