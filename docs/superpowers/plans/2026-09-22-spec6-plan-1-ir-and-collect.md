# Spec 6, Plan 1 of 4: Storage IR, Collection, Normalization

> Lean plan. Spec §2–§4.

- `ir/storage.py` (`storage_bucket` + lifecycle, CORS, website, Object Lock models); registered.
- `plugins/storage/collect_aws.py`: region filter via `GetBucketLocation` (legacy `None`/`EU`
  decoded), eighteen per-bucket calls with "not configured" → `null`, denied → scan gap; account
  Block Public Access.
- `normalize.py`: effective Block Public Access, public via policy/ACL, KMS key (AWS-managed alias
  dropped), lifecycle filters (prefix/tags/size), Object Lock years → days, notifications by kind.
- `StoragePlugin` (no requirements) registered before Database; placeholder generator with coverage.
- Tests: moto collection (region filter, not-configured, denied call), normalization, coverage.
