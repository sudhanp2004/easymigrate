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

## Spec 2: the database run (RDS → Cloud SQL)

The testbed now carries two databases — a **public Postgres** (`test-postgres-db`) and a
**private MySQL** (`test-mysql-db`) in the DB subnet group — so one run exercises both
Cloud SQL connectivity paths: public IP with authorized networks, and private IP over a
Private Services Access connection.

### Extra prerequisites

- The GCP principal also needs `roles/cloudsql.admin` and
  `roles/servicenetworking.networksAdmin`.
- Two APIs enabled on the project: `sqladmin.googleapis.com` and
  `servicenetworking.googleapis.com`. `easymigrate plan` checks these and prints the
  exact `gcloud services enable` command if either is missing — it deliberately does not
  enable them for you, because a generated `google_project_service` would disable the API
  for the whole project on teardown.

### Procedure

1. `terraform -chdir=testbed/aws apply` with `TF_VAR_db_password` set.
2. `easymigrate scan --region ap-south-1` → `analyze` → `resolve --accept-defaults
   --acknowledge-manual`.
3. Read `.easymigrate/report.md` **before planning**. Check the Databases section says
   what you expect: the tier each instance mapped to, private IP with its PSA range for
   the MySQL one, public IP with authorized networks for the Postgres one, and the
   dropped `max_heap_table_size` parameter.
4. `easymigrate plan --gcp-project $GCP_PROJECT --db-password
   database_instance:test-postgres-db=<pw> --db-password database_instance:test-mysql-db=<pw>`.
   EasyMigrate never invents a database password; supply one per instance here, by flag,
   by `EASYMIGRATE_DB_PASSWORD_<SLUG>` env var, or at the prompt on a terminal.
5. `easymigrate apply`.

### Verifying

- **Public Postgres:** `psql "host=<public ip> user=dbadmin dbname=testdb"` from an
  address inside an authorized network, using the password from step 4.
- **Private MySQL:** it has no public address by design. Create a small Compute Engine VM
  in the migrated VPC and connect from there:
  `gcloud compute instances create psa-check --zone <zone> --subnet <migrated subnet>`,
  then `mysql -h <private ip> -u dbadmin -p testdb`. Delete the VM afterwards.
- Confirm the PSA plumbing exists:
  `gcloud compute addresses list --global` and
  `gcloud services vpc-peerings list --network=<migrated network>`.

### Teardown — part of the procedure, not an afterthought

1. Delete the verification VM, if you made one.
2. `tofu destroy` in `.easymigrate/terraform`. **Expect this to be slow, and to need a
   retry.** Observed on the 2026-09-17 run: the Cloud SQL instances delete fine, then the
   service networking connection fails with *"Producer services (e.g. CloudSQL, Cloud
   Memstore, etc.) are still using this connection"* — the producer side has not released
   the peering yet. It is not a real failure; wait a few minutes and run `tofu destroy`
   again until it reports `Destroy complete`. Cloud SQL instances with
   `deletion_protection` set must have it cleared first.
3. `terraform -chdir=testbed/aws destroy`.

**Timing, measured on the 2026-09-17 run:** a Cloud SQL instance takes 10-17 minutes to
create (the MySQL one took 16m44s), so budget ~25 minutes for `apply` and another 10-15
for teardown including the retry above.

**Cost:** two `db.t3.micro` RDS instances and two Cloud SQL instances bill per hour on
both sides simultaneously. Run this once, deliberately, and tear it down the same day.

**Targeting a cheaper subset:** the testbed's EKS cluster and NAT gateway dominate its
cost and add ~25 minutes, and spec 2 touches neither. `terraform apply
-target=aws_db_instance.postgres -target=aws_db_instance.mysql` pulls in only the VPC,
subnets and groups the databases need. Note that a `publicly_accessible` RDS instance
requires an internet gateway attached to its VPC, so add
`-target=aws_route_table_association.sub1 -target=aws_route_table_association.sub2`
(which pulls in the gateway) and expect the first apply to race: RDS validates the
gateway before the attachment propagates, so re-run the apply once.

**Verifying the private path without internet access:** a VM in a migrated subnet has no
egress unless the source subnets had an internet gateway, so `apt install mysql-client`
will fail. Probe with bash instead, which needs no packages — MySQL sends its version
handshake on connect, so readable bytes prove the database answered:

    exec 3<>/dev/tcp/<private-ip>/3306 && head -c 60 <&3 | tr -cd '[:print:]'

Write the result to `/dev/console` from the startup script and read it back with the
serial-port API; guest attributes proved unreliable.

**If a destroy fails partway, finish it before re-running.** A stranded firewall rule or
subnet is left in GCP but dropped from state, and the next `apply` fails with
`409 already exists` on each one in turn until they are deleted by hand.

**A name you delete is not free again immediately:** a deleted Cloud SQL instance name
cannot be reused for about a week, so a second run needs different IR ids (rename the
testbed instances) or a wait.
