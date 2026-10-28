# Changelog

All notable changes to EasyMigrate. Versions follow [Semantic Versioning](https://semver.org);
the saved-state schema (`schema_version` in `.easymigrate/` files) is versioned separately
and listed per release.

## Unreleased

- **Hosted free report** at https://easymigrate.sudhan.tech: connect AWS through a read-only
  CloudFormation role (external ID per session, no keys), scan, review differences, choose
  decisions and download the GCP Terraform + report. No GCP access, no apply. Anonymous
  workspaces, encrypted, deleted after 7 days. Deployment: `deploy/README.md`.
- `pipeline.generate()`: the Terraform of `plan` without GCP credentials or OpenTofu.
- Role assumption accepts an external ID.

## 0.1.0 — 2026-10-14

First release. AWS → Google Cloud, infrastructure configuration only (no data copy).
Saved-state schema 1.

- **Pipeline:** `scan` (live, read-only) → `analyze` (intent IR + differences) → `resolve`
  (a strategy per difference, safe defaults) → `plan` (generated Terraform, `tofu plan`) →
  `apply` (exactly the reviewed plan) → `destroy` (undo). Local review UI (`easymigrate ui`).
- **Families:** VPC networking and security groups → VPC, subnets, routes, firewall rules;
  RDS → Cloud SQL (private IP, flags, HA, backups); EC2, EBS, launch templates and Auto
  Scaling groups → Compute Engine VMs, disks, instance templates, managed instance groups;
  EKS → GKE Standard; S3 bucket configuration → Cloud Storage; ALB/NLB → Cloud Load
  Balancing; IAM → report with suggested predefined roles.
- **Scope:** `analyze --vpc/--tag/--exclude` migrates part of an account.
- **Undo:** `destroy` lifts deletion protection on request and clears the private-services
  peering Cloud SQL leaves behind.
- **Saved state:** every `.easymigrate/` file is schema-versioned; older files are migrated
  or rebuilt with a clear message, newer ones refused.
- **Facts check:** `scripts/verify_gcp_facts.py` checks the built-in Google Cloud facts
  (machine types, images, versions, flags, roles) against a live project.

Known limits: no data migration; NLB migration not yet proven live; HTTPS listeners map to
Google-managed certificates, which need the domain's DNS pointed at the new load balancer.
