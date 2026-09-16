#!/usr/bin/env bash
# Manual, never-in-CI end-to-end test against real AWS and GCP. See README.md in
# this directory before running this. Nothing in this repo's test suite or CI
# invokes this script; run it yourself, by hand.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -z "${GCP_PROJECT:-}" ]]; then
  echo "Set GCP_PROJECT to the GCP project id to migrate into." >&2
  exit 1
fi
if [[ -z "${TF_VAR_db_password:-}" ]]; then
  echo "Set TF_VAR_db_password for the testbed RDS instance." >&2
  exit 1
fi

echo "==> 1. Applying the AWS testbed"
terraform -chdir="${repo_root}/testbed/aws" init
terraform -chdir="${repo_root}/testbed/aws" apply -auto-approve

workdir="$(mktemp -d)"
echo "==> 2. Running EasyMigrate in ${workdir}"
(
  cd "${workdir}"
  uv run --project "${repo_root}" easymigrate scan --region ap-south-1
  uv run --project "${repo_root}" easymigrate analyze
  uv run --project "${repo_root}" easymigrate resolve --accept-defaults --acknowledge-manual
  uv run --project "${repo_root}" easymigrate plan --gcp-project "${GCP_PROJECT}"
  uv run --project "${repo_root}" easymigrate apply
)

echo "==> 3. Confirming the GCP network exists"
gcloud compute networks list --project "${GCP_PROJECT}"

echo "==> 4. Destroying the GCP resources"
tofu -chdir="${workdir}/.easymigrate/terraform" destroy -auto-approve

echo "==> 5. Destroying the AWS testbed"
terraform -chdir="${repo_root}/testbed/aws" destroy -auto-approve

echo "==> Done. Workdir was ${workdir}"
