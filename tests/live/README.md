# Live end-to-end test (manual, never in CI)

`run.sh` in this directory drives EasyMigrate against a real AWS testbed and a real
GCP project. It is **not** part of the automated test suite (nothing under
`tests/live/` is collected by pytest, and CI never invokes it) because it creates
billed AWS and GCP resources and needs real credentials for both clouds. Run it
yourself, by hand, when you want to validate a change end to end.

## Prerequisites

- AWS credentials for an account you're willing to create testbed resources in
  (`terraform -chdir=testbed/aws`), and `TF_VAR_db_password` set.
- A GCP project you're willing to create resources in, with Application Default
  Credentials available (`gcloud auth application-default login`) for a principal
  holding `roles/compute.networkAdmin` and `roles/storage.admin` on it.
- `GCP_PROJECT` set to that project's id.
- `terraform` (or `tofu`) on `PATH` to manage the AWS testbed; EasyMigrate manages
  its own OpenTofu for the GCP side.

## What it does

    ./tests/live/run.sh

1. `terraform -chdir=testbed/aws apply` — creates the AWS testbed (VPC, public and
   private subnets, NAT gateway, two cross-referencing security groups, a custom
   NACL, RDS and EKS).
2. In a temporary directory: `easymigrate scan` → `analyze` →
   `resolve --accept-defaults --acknowledge-manual` →
   `plan --gcp-project $GCP_PROJECT` → `apply`.
3. `gcloud compute networks list` — confirm the GCP network was created.
4. `tofu destroy` in `.easymigrate/terraform` — tear down the GCP resources.
5. `terraform -chdir=testbed/aws destroy` — tear down the AWS testbed.

**Cost:** the AWS testbed bills for EKS, RDS, and a NAT gateway plus its Elastic IP
while it exists; the GCP side is cheap (networking only) but still billed. Don't
leave either environment running longer than the test needs.
