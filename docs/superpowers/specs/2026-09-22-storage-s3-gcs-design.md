# EasyMigrate — Object storage: S3 → Cloud Storage (Spec 6)

Status: draft 2026-09-22
Builds on specs 1 (pipeline), 2 (`Decision.params`), 4 and 5 (the family pattern). The UI gains the
kind without change. Design calls were delegated (§9 records each with its reason).

## 1. Scope

Fifth family, **configuration only** (PRODUCT.md roadmap): every S3 bucket in the scanned region
becomes a Cloud Storage bucket with the closest equivalent configuration. **No objects are copied.**

### In scope

Bucket name and location, versioning, default encryption, Block Public Access and public
reachability, object ownership / ACL mode, lifecycle rules, Intelligent-Tiering, CORS, static
website configuration, server access logging, Object Lock default retention, requester pays, tags.
Replication, event notifications, transfer acceleration and bucket policies are named differences
that generate nothing.

### Out of scope

- **Objects.** Buckets are created empty. Copying is Storage Transfer Service's job, a later,
  assisted product (the same boundary as specs 2 and 4). `S3-OBJECTS-NOT-COPIED` states it at high
  risk.
- Translating bucket policies and ACL grants into IAM (IAM is report-only on the roadmap).
- S3 Access Points, Multi-Region Access Points, Object Lambda, S3 Express directory buckets,
  Storage Lens, Batch Operations, inventory/analytics/metrics configurations (each named in the
  report when present, not translated).

## 2. Architecture

```
plugins/storage/      collect_aws.py  normalize.py  generate_gcp.py  __init__.py (StoragePlugin)
ir/storage.py         storage_bucket
differences/rules/aws_gcp/storage.py     S3-* rules
differences/rules/aws_gcp/gcs_facts.py   storage-class map, name rules (dated)
```

`StoragePlugin.requires = frozenset()` — buckets reference no network objects.
`PLUGINS = (Network, Compute, Kubernetes, Storage, Database)`. `aws_resource_types = {"s3"}`.

## 3. Collection

`s3:ListBuckets` is global, so each bucket's region comes from `GetBucketLocation`
(`None` → `us-east-1`, `EU` → `eu-west-1`). Buckets outside the scanned region produce one `info`
finding naming how many and where, not one per bucket. For each bucket in the region, one snapshot
entry holds the raw responses of: `GetBucketVersioning`, `GetBucketEncryption`,
`GetPublicAccessBlock`, `GetBucketPolicyStatus`, `GetBucketPolicy`, `GetBucketAcl`,
`GetBucketOwnershipControls`, `GetBucketLifecycleConfiguration`, `GetBucketCors`,
`GetBucketWebsite`, `GetBucketLogging`, `GetObjectLockConfiguration`, `GetBucketReplication`,
`GetBucketNotificationConfiguration`, `GetBucketRequestPayment`, `GetBucketAccelerateConfiguration`,
`ListBucketIntelligentTieringConfigurations`, `GetBucketTagging`. Snapshot file:
`storage.buckets.json`. The account-level `s3control:GetPublicAccessBlock` goes to
`storage.account_public_access_block.json`.

A "not configured" error (`NoSuchLifecycleConfiguration`, `NoSuchBucketPolicy`,
`ServerSideEncryptionConfigurationNotFoundError`, …) records `null`, never a gap. `AccessDenied`
becomes a `scan_gap` naming the IAM action, and that bucket is still captured with what was read.

## 4. IR — `storage_bucket`

| Field | From |
|---|---|
| `bucket_name` | the S3 name |
| `region` | location |
| `versioning` | `"enabled"` \| `"suspended"` \| `"disabled"` |
| `encryption` | `"sse_s3"` \| `"sse_kms"` \| `"dsse_kms"` |
| `kms_key` | customer-managed key ARN; `None` for the AWS-managed `aws/s3` key |
| `public_access_blocked` | all four Block Public Access flags on, bucket **or** account level |
| `public_via_policy`, `public_via_acl` | `PolicyStatus.IsPublic`; an `AllUsers`/`AuthenticatedUsers` grant |
| `has_policy` | a bucket policy exists |
| `object_ownership` | `BucketOwnerEnforced` \| `BucketOwnerPreferred` \| `ObjectWriter` |
| `lifecycle_rules` | `[{id, enabled, prefix, filtered_by_tags, filtered_by_size, transitions: [{days, storage_class}], expiration_days, noncurrent_transitions, noncurrent_expiration_days, newer_noncurrent_versions, abort_multipart_days}]` |
| `cors_rules` | `[{origins, methods, allowed_headers, expose_headers, max_age}]` |
| `website` | `{index, error, redirect_all, routing_rules}` or `None` |
| `logging_bucket`, `logging_prefix` | server access logging target |
| `object_lock` | `{mode, days}` default retention (years → days), `None` when off; `object_lock_enabled` |
| `replication_destinations` | destination bucket ARNs |
| `notifications` | destination kinds (`sns`, `sqs`, `lambda`, `eventbridge`) |
| `requester_pays`, `acceleration`, `intelligent_tiering` | booleans |

Tags become `labels` through `clean_labels`, as everywhere else.

## 5. Difference catalogue (`S3-*`)

| Rule | Detects | Strategies (**default** first) → hints |
|---|---|---|
| `S3-OBJECTS-NOT-COPIED` | every bucket | **`note_only`** (high): the bucket is created empty; copy objects with Storage Transfer Service |
| `S3-BUCKET-NAME` | every bucket | **`keep_name`** (low; absent when the name is invalid for GCS) → `gcs_name` · `rename` (low; param `name`). GCS names share one global namespace separate from S3's, so a kept name can still be taken — `apply` then fails with a clear error. Dotted names need domain verification in GCS and names starting with `goog` or containing `google` are forbidden, so those default to `rename` with dots → hyphens |
| `S3-PUBLIC-ACCESS` | bucket readable by the public (policy or ACL, not blocked) | **`private`** (medium): anonymous readers lose access · `public_read` (high) → `allUsers` gets `roles/storage.objectViewer` |
| `S3-BUCKET-POLICY` | `has_policy` | **`manual`**, not excluding: statements are not translated; grant IAM on the new bucket |
| `S3-ACLS` | `object_ownership` ≠ `BucketOwnerEnforced` | **`uniform_access`** (medium): uniform bucket-level access; object ACLs no longer apply · `fine_grained` (low): ACLs stay possible, none are copied |
| `S3-ENCRYPTION-KMS` | `kms_key` set | **`google_managed`** (low) · `manual` |
| `S3-LIFECYCLE` | any lifecycle rule | **`map_rules`** (low; medium when anything is dropped): transitions map by `STANDARD_IA`/`ONEZONE_IA`→`NEARLINE`, `GLACIER_IR`→`COLDLINE`, `GLACIER`/`DEEP_ARCHIVE`→`ARCHIVE`; expiration → `Delete` by age; noncurrent expiration → `Delete` by `days_since_noncurrent_time` / `num_newer_versions`; multipart abort → `AbortIncompleteMultipartUpload`; prefix → `matches_prefix`. Tag and size filters have no GCS condition, so those rules are dropped and named. Disabled rules are not generated. GCS minimum storage durations (30/90/365 days) differ from S3's and are stated |
| `S3-INTELLIGENT-TIERING` | `intelligent_tiering` | **`autoclass`** (low) · `standard` (low) |
| `S3-CORS` | a rule restricting `allowed_headers` | **`map_cors`** (low): GCS CORS has no request-header allow-list, so any header is accepted |
| `S3-WEBSITE` | `website` set | **`website_config`** (high): index/error pages carry, but the `s3-website` endpoint does not exist on GCS — serving needs a load balancer or a domain-named bucket; redirect rules are not carried · `omit` |
| `S3-ACCESS-LOGGING` | `logging_bucket` set | **`usage_logs`** (low; when the target bucket is in the scan) → GCS usage logs to the migrated target, in GCS's CSV format · `omit` (low); only `omit` when the target is not migrated |
| `S3-OBJECT-LOCK` | `object_lock` default retention | **`retention_unlocked`** (medium): a bucket retention policy of the same length that an admin can still remove, unlike S3 Compliance mode · `retention_locked` (high): irreversible once applied · `omit` (high) |
| `S3-REPLICATION` | replication destinations | **`manual`**, not excluding: no replication is generated; use a dual-region bucket or Storage Transfer Service |
| `S3-NOTIFICATIONS` | notifications | **`manual`**, not excluding: Pub/Sub notifications need topics this family does not create |
| `S3-ACCELERATION` | `acceleration` | **`note_only`** (low): GCS has no transfer acceleration; its global network serves the same purpose |

## 6. Generation (`terraform/storage.tf`)

`google_storage_bucket` per live bucket: `name` (hint), `location = upper(var.region)`,
`storage_class = "STANDARD"`, `uniform_bucket_level_access`, `public_access_prevention`
(`"enforced"` unless the bucket is public or `public_read` was chosen; `"inherited"` otherwise),
`versioning`, `lifecycle_rule` blocks, `cors`, `website`, `logging` (target's generated name),
`retention_policy`, `requester_pays`, `autoclass`, `labels`. `force_destroy` is **not** set: a
bucket holding objects must not be deleted by a teardown, as in S3.
`google_storage_bucket_iam_member` (`allUsers`, `roles/storage.objectViewer`) for `public_read`.
`GenerationError` for a missing `gcs_name`, and for two buckets resolving to the same name.

## 7. Report

`## Storage` section per bucket: GCS name (and the rename when there was one), public or private,
versioning, lifecycle rules carried and dropped, what else was not carried. Closing paragraph: the
buckets are empty; copy data with Storage Transfer Service.

## 8. Testing

Unit tests per rule; moto collection tests (region filter, not-configured errors, a denied call);
normalization; golden `expected_storage.tf` for: a plain private bucket, a public website bucket,
a versioned bucket with a full lifecycle, a KMS + Object Lock + replication bucket, a dotted name;
provider-schema validation; a live run.

## 9. Decisions taken (delegated 2026-09-22)

- **D1 Configuration only**, per the roadmap; objects are Storage Transfer Service's job.
- **D2 Keep the bucket name when GCS allows it.** Applications and people know buckets by name;
  a rename is one strategy away, and a taken name fails loudly at apply.
- **D3 Private by default** when the source was public, like every other family's public default.
- **D4 Uniform bucket-level access** for ACL-enabled buckets: AWS itself defaults new buckets to
  `BucketOwnerEnforced`, and no ACL grants are copied either way.
- **D5 Object Lock → unlocked retention policy.** A locked GCS retention policy is irreversible;
  EasyMigrate never makes an irreversible choice by default.
- **D6 No `force_destroy`.** A bucket that gained objects after migration must not vanish on
  teardown.
- **D7 Only the scanned region's buckets.** Every family scans one region; others are counted.

## 10. Plans

1. IR, collection, normalization, plugin registration.
2. The sixteen `S3-*` rules and `gcs_facts.py`.
3. Generation, report section, goldens, provider-schema tests.
4. Testbed buckets and live run.
