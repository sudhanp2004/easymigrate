# Spec 6, Plan 3 of 4: Generation and Report

> Lean plan. Spec §6, §7, §8.

- `google_storage_bucket` (+ `google_storage_bucket_iam_member` for public read); no `force_destroy`.
- Two GCS constraints resolved in the rules, not left to apply: Autoclass refuses storage-class lifecycle transitions (S3-INTELLIGENT-TIERING defaults to standard when transitions exist, and drops them if Autoclass is chosen); (the draft also assumed a retention policy cannot coexist with versioning; the live run disproved it, so S3-OBJECT-LOCK keeps versioning). The generator guards the Autoclass conflict.
- `## Storage` report section; three golden fixtures; provider-schema validation.
