# Undo: `easymigrate destroy` (2026-10-10)

Publish blocker 2: once `apply` had run there was no way back except `tofu destroy` by hand, and
private-IP Cloud SQL then failed every time on the services peering. Design calls were delegated;
the reasons are recorded here.

## What it does

`easymigrate destroy [--yes] [--remove-protection] [--delete-state-bucket]`

1. Refuses a project that was never planned (no target in `project.yaml`, or no `terraform/*.tf`).
2. Checks GCP credentials, runs `tofu init` against the state bucket, reads the state with
   `tofu show -json` (child modules included).
3. Lists every managed resource and asks (`--yes` required off a terminal, like `apply`). Empty
   state: nothing to ask, says so.
4. Deletion protection: resources with `deletion_protection = true`, or Cloud SQL with
   `settings.deletion_protection_enabled`, stop the run before anything changes unless
   `--remove-protection` is given. With it, the generated `.tf` files have those lines set to
   `false` and `tofu apply -target=<protected>` lifts the switch; the state is re-read and the run
   stops if any is still on.
5. `tofu plan -destroy -out=destroy.tfplan`, then apply that plan.
6. If it fails on the services networking connection ("Producer services ... still using this
   connection") and the state still holds one, the peering is removed through the Compute API
   (`networks.removePeering`, waiting on the operation) and steps 5 run once more.
7. Removes `tfplan`, `plan.lock.json` and `destroy.tfplan`, so the old plan can never be applied
   to the emptied project. `--delete-state-bucket` then deletes the state bucket with every object
   version.

## Decisions

- **Scope is the state, nothing else.** EasyMigrate never changes AWS, so undo means deleting what
  its own Terraform state holds. Nothing is looked up by name or label in the GCP project.
- **Protection is opt-in to remove.** It came from the customer's AWS settings, so it is treated as
  a deliberate signal: name the resources, require the flag.
- **No data is ever deleted.** Generated buckets have no `force_destroy`; a bucket with objects
  fails the destroy and the error says to empty it by hand. Same rule as the rest of the product
  (config only, data later).
- **Peering removal is reactive.** Only after the documented failure, and only peerings recorded in
  state. A proactive delete before Cloud SQL is gone would break the database's connectivity mid
  destroy for no benefit.
- **Editing the generated `.tf` in place** is safe: `plan` deletes and regenerates every `.tf`, and
  the old plan lock is removed anyway. An override file cannot do it, because Terraform overrides
  replace a whole nested `settings` block.
- **CLI only for now.** The UI keeps its plan/apply flow; a destroy button needs its own
  confirmation design.

## Live test (2026-10-10)

AWS testbed, scoped with `analyze --vpc`: VPC, two subnets, three security groups, private MySQL
8.0 with `DeletionProtection` and an EC2 instance with termination protection (both set by a
temporary override). EasyMigrate planned and applied 12 GCP resources (Cloud SQL took 16 min);
both protections were confirmed on in GCP.

- `destroy --yes` refused before changing anything and named the VM and the Cloud SQL instance.
- `destroy --yes --remove-protection --delete-state-bucket`: protection lifted on both (2 s and
  12 s), 10 resources destroyed, then the services connection failed exactly as in every earlier
  teardown. The peering was removed through the Compute API and the second pass destroyed the
  remaining 2 (the refresh found the connection gone). State bucket deleted. GCP afterwards held
  no Cloud SQL, VMs, addresses, firewall rules or networks besides the project's `default`.
- Found and fixed in the run: a second `destroy` after the bucket was deleted printed tofu's raw
  "bucket doesn't exist" error. It now checks the bucket first (never creating it) and reports
  nothing to destroy.
