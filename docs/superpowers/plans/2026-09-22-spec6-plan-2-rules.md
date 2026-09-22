# Spec 6, Plan 2 of 4: Storage Rules

> Lean plan. Spec §5.

- `gcs_facts.py`: storage-class map, minimum durations, class order, name rules and sanitizer.
- Fifteen `S3-*` rules registered after the Kubernetes rules, `S3-BUCKET-NAME` before `S3-ACCESS-LOGGING`.
- Lifecycle translation (`gcs_lifecycle`) guards each transition with `matches_storage_class` so an object only ever moves colder.
- Tests for every rule, the name rules and the lifecycle translation.
