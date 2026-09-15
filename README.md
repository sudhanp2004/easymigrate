# EasyMigrate

Migrates cloud infrastructure from AWS to GCP. It scans the live source cloud, builds an intent-level intermediate representation, shows every difference between the clouds with safe default handling, generates Terraform, and applies it only after you approve.

## Usage

    # 1. Read a live AWS region (read-only access is enough)
    easymigrate scan --region ap-south-1 --aws-profile my-readonly-profile
    #    or: easymigrate scan --region ap-south-1 --aws-role-arn arn:aws:iam::123456789012:role/EasyMigrateScan

    # 2. Turn the scan into the intent-level IR (.easymigrate/ir.json)
    easymigrate analyze

Both commands work in the current directory and keep their output in `.easymigrate/`. That folder holds account metadata such as account IDs and ARNs; keep it private. It is added to `.gitignore` automatically inside a git repository.

Permissions `scan` needs: `ec2:DescribeVpcs`, `ec2:DescribeVpcAttribute`, `ec2:DescribeSubnets`, `ec2:DescribeRouteTables`, `ec2:DescribeInternetGateways`, `ec2:DescribeNatGateways`, `ec2:DescribeSecurityGroups`, `ec2:DescribeNetworkAcls`, `tag:GetResources`, `sts:GetCallerIdentity`. Missing permissions are reported as scan gaps rather than failing the scan.

## Development

    uv sync
    uv run pytest
    uv run ruff check . && uv run ruff format --check .

Design: `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md`
