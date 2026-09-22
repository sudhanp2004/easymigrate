"""AWS→GCP object-storage difference rules (spec 6 §5).

Rules decide the Cloud Storage shape and record it in `target_hints`;
`plugins/storage/generate_gcp.py` renders it.
"""

from __future__ import annotations

from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.compute import (
    _AcknowledgeOnly,
    _replace_object,
    _set_hint,
    _subject,
)
from easymigrate.differences.rules.aws_gcp.gcs_facts import (
    CLASS_ORDER,
    MIN_STORAGE_DAYS,
    STORAGE_CLASS,
    name_problem,
    sanitized,
)
from easymigrate.ir.base import IRDocument
from easymigrate.ir.storage import LifecycleRule, StorageBucket


def _buckets(ir: IRDocument) -> list[StorageBucket]:
    return sorted(ir.of_kind(StorageBucket), key=lambda b: b.id)


class S3ObjectsNotCopied(_AcknowledgeOnly, DifferenceRule):
    id = "S3-OBJECTS-NOT-COPIED"
    family = "storage"
    title = "The bucket is created empty"
    impact = (
        "EasyMigrate migrates bucket configuration, not data. No object, version or metadata is "
        "copied; Storage Transfer Service moves the contents once the bucket exists."
    )
    strategies = (
        Strategy(
            id="note_only",
            title="Understood",
            effect="the bucket exists with its configuration and holds nothing",
            risk="high",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([b.id]) for b in _buckets(ir)]


class S3BucketName(DifferenceRule):
    id = "S3-BUCKET-NAME"
    family = "storage"
    title = "Bucket names live in a different global namespace"
    impact = (
        "Cloud Storage bucket names are global, like S3's, but a separate namespace: the same name "
        "may already belong to someone else, and dotted names or names mentioning Google are not "
        "accepted without extra steps. Applications address the bucket through a new endpoint "
        "either way."
    )
    strategies = (
        Strategy(
            id="keep_name",
            title="Keep the name",
            effect="the bucket keeps its S3 name; apply fails clearly if another project owns it",
            risk="low",
        ),
        Strategy(
            id="rename",
            title="Rename the bucket",
            effect="the bucket gets the name you give; anything naming the old bucket must change",
            risk="low",
        ),
    )
    default = "keep_name"
    param_docs = {"name": "Cloud Storage bucket name"}

    def detect(self, ir: IRDocument) -> list[Difference]:
        names = [b.bucket_name for b in _buckets(ir)]
        return [
            self.difference(
                [b.id],
                bucket_name=b.bucket_name,
                problem=name_problem(b.bucket_name),
                suggested=sanitized(b.bucket_name),
                others=sorted(n for n in names if n != b.bucket_name),
            )
            for b in _buckets(ir)
        ]

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("problem"):
            return (self.strategy("rename"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return "rename" if diff.context.get("problem") else self.default

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        return {"name": diff.context["suggested"]} if strategy_id == "rename" else {}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if strategy_id != "rename":
            return
        name = params.get("name")
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        problem = name_problem(name)
        if problem:
            raise ValueError(f"name {name!r}: {problem}")
        if name in diff.context.get("others", []):
            raise ValueError(f"name {name!r} is another scanned bucket's name")

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {bucket.id}")
        if strategy_id == "keep_name":
            return _replace_object(doc, _set_hint(bucket, "gcs_name", bucket.bucket_name))
        params = {**self.default_params(diff, strategy_id), **(params or {})}
        self.validate_params(diff, strategy_id, params)
        return _replace_object(doc, _set_hint(bucket, "gcs_name", params["name"]))


class S3PublicAccess(DifferenceRule):
    id = "S3-PUBLIC-ACCESS"
    family = "storage"
    title = "A publicly readable bucket becomes private unless you say otherwise"
    impact = (
        "The bucket's policy or ACL lets anyone read it. Recreating that exposure without asking "
        "would publish data the user may not have reviewed, so the default is private."
    )
    strategies = (
        Strategy(
            id="private",
            title="Private",
            effect=(
                "public access prevention is enforced; anonymous readers, including website "
                "visitors, lose access"
            ),
            risk="medium",
        ),
        Strategy(
            id="public_read",
            title="Public read",
            effect="allUsers can read every object, as in the source",
            risk="high",
        ),
    )
    default = "private"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([b.id], via_policy=b.public_via_policy, via_acl=b.public_via_acl)
            for b in _buckets(ir)
            if (b.public_via_policy or b.public_via_acl) and not b.public_access_blocked
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id not in {"private", "public_read"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(bucket, "public", strategy_id == "public_read"))


class S3BucketPolicy(_AcknowledgeOnly, DifferenceRule):
    id = "S3-BUCKET-POLICY"
    family = "storage"
    title = "The bucket policy is not translated"
    impact = (
        "S3 bucket policies grant access to AWS principals and conditions that have no direct "
        "Cloud Storage equivalent. Public read is handled by S3-PUBLIC-ACCESS; every other "
        "statement must be recreated as IAM on the new bucket."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Recreate access yourself",
            effect="no IAM bindings are generated from the policy; grant what each consumer needs",
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([b.id]) for b in _buckets(ir) if b.has_policy]


class S3Acls(DifferenceRule):
    id = "S3-ACLS"
    family = "storage"
    title = "ACL-enabled buckets become uniform-access buckets"
    impact = (
        "The bucket still honours object ACLs. AWS itself now disables ACLs on new buckets; "
        "Cloud Storage's equivalent is uniform bucket-level access. No ACL grant is copied either "
        "way."
    )
    strategies = (
        Strategy(
            id="uniform_access",
            title="Uniform bucket-level access",
            effect="access is IAM only; per-object ACLs no longer apply",
            risk="medium",
        ),
        Strategy(
            id="fine_grained",
            title="Fine-grained ACLs",
            effect="object ACLs remain possible on the new bucket; none are copied",
            risk="low",
        ),
    )
    default = "uniform_access"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([b.id], ownership=b.object_ownership)
            for b in _buckets(ir)
            if b.object_ownership != "BucketOwnerEnforced"
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id not in {"uniform_access", "fine_grained"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(
            doc, _set_hint(bucket, "uniform_access", strategy_id == "uniform_access")
        )


class S3EncryptionKms(DifferenceRule):
    id = "S3-ENCRYPTION-KMS"
    family = "storage"
    title = "A customer-managed KMS key does not carry to GCP"
    impact = (
        "Objects were encrypted with the customer's AWS KMS key. Cloud Storage encrypts at rest "
        "with Google-managed keys by default; a customer-managed key needs Cloud KMS."
    )
    strategies = (
        Strategy(
            id="google_managed",
            title="Google-managed encryption",
            effect="objects are encrypted at rest with Google-managed keys",
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="create a Cloud KMS key and set the bucket's default encryption yourself",
            risk="low",
            manual=True,
        ),
    )
    default = "google_managed"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([b.id], kms_key=b.kms_key) for b in _buckets(ir) if b.kms_key]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        if strategy_id not in {"google_managed", "manual"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return ir.model_copy(deep=True)


def _colder_than(target: str) -> list[str]:
    return list(CLASS_ORDER[: CLASS_ORDER.index(target)])


def gcs_lifecycle(rule: LifecycleRule) -> tuple[list[dict[str, Any]], list[str]]:
    """(Cloud Storage lifecycle rules, what could not be carried) for one S3 rule."""
    if not rule.enabled:
        return [], [f"{rule.id}: disabled, not generated"]
    if rule.filtered_by_tags or rule.filtered_by_size:
        what = "object tags" if rule.filtered_by_tags else "object size"
        return [], [f"{rule.id}: filters by {what}, which Cloud Storage lifecycle cannot match"]
    base: dict[str, Any] = {"matches_prefix": [rule.prefix]} if rule.prefix else {}
    out: list[dict[str, Any]] = []
    dropped: list[str] = []

    def transition(days: int, s3_class: str, state: str, age_key: str) -> None:
        target = STORAGE_CLASS.get(s3_class)
        if target is None:
            dropped.append(
                f"{rule.id}: transition to {s3_class} has no storage class (see autoclass)"
            )
            return
        out.append(
            {
                "action": {"type": "SetStorageClass", "storage_class": target},
                "condition": {
                    **base,
                    age_key: days,
                    "with_state": state,
                    "matches_storage_class": _colder_than(target),
                },
            }
        )

    for t in rule.transitions:
        transition(t.days, t.storage_class, "LIVE", "age")
    for t in rule.noncurrent_transitions:
        transition(t.days, t.storage_class, "ARCHIVED", "days_since_noncurrent_time")
    if rule.expiration_days is not None:
        out.append(
            {
                "action": {"type": "Delete"},
                "condition": {**base, "age": rule.expiration_days, "with_state": "LIVE"},
            }
        )
    if rule.noncurrent_expiration_days is not None or rule.newer_noncurrent_versions is not None:
        condition: dict[str, Any] = {**base, "with_state": "ARCHIVED"}
        if rule.noncurrent_expiration_days is not None:
            condition["days_since_noncurrent_time"] = rule.noncurrent_expiration_days
        if rule.newer_noncurrent_versions is not None:
            condition["num_newer_versions"] = rule.newer_noncurrent_versions
        out.append({"action": {"type": "Delete"}, "condition": condition})
    if rule.abort_multipart_days is not None:
        out.append(
            {
                "action": {"type": "AbortIncompleteMultipartUpload"},
                "condition": {**base, "age": rule.abort_multipart_days},
            }
        )
    return out, dropped


class S3Lifecycle(DifferenceRule):
    id = "S3-LIFECYCLE"
    family = "storage"
    title = "Lifecycle rules map to Cloud Storage lifecycle actions"
    impact = (
        "Transitions, expirations and multipart clean-up carry, with S3 storage classes mapped to "
        "the nearest Cloud Storage class. Rules filtered by object tags or size cannot be "
        "expressed and are dropped. Cloud Storage bills minimum storage durations of 30, 90 and "
        "365 days for Nearline, Coldline and Archive."
    )
    strategies = (
        Strategy(
            id="map_rules",
            title="Map each rule",
            effect="every expressible rule is generated; the rest are listed in the report",
            risk="low",
        ),
    )
    default = "map_rules"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for bucket in _buckets(ir):
            if not bucket.lifecycle_rules:
                continue
            dropped = [d for r in bucket.lifecycle_rules for d in gcs_lifecycle(r)[1]]
            classes = sorted(
                {
                    a["action"]["storage_class"]
                    for r in bucket.lifecycle_rules
                    for a in gcs_lifecycle(r)[0]
                    if a["action"]["type"] == "SetStorageClass"
                }
            )
            diffs.append(
                self.difference(
                    [bucket.id],
                    dropped=dropped,
                    minimum_days={c: MIN_STORAGE_DAYS[c] for c in classes},
                )
            )
        return diffs

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        dropped = [d for d in diff.context.get("dropped", []) if "disabled" not in d]
        return "medium" if dropped else "low"

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id != "map_rules":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        rules = [g for r in bucket.lifecycle_rules for g in gcs_lifecycle(r)[0]]
        return _replace_object(doc, _set_hint(bucket, "lifecycle", rules))


class S3IntelligentTiering(DifferenceRule):
    id = "S3-INTELLIGENT-TIERING"
    family = "storage"
    title = "Intelligent-Tiering becomes Autoclass"
    impact = (
        "Both move objects between classes by access pattern. Autoclass works per bucket rather "
        "than per configuration, and its tiers and pricing differ."
    )
    strategies = (
        Strategy(
            id="autoclass",
            title="Enable Autoclass",
            effect="Cloud Storage moves each object to the class its access pattern warrants",
            risk="low",
        ),
        Strategy(
            id="standard",
            title="Stay in Standard",
            effect="objects stay in Standard unless a lifecycle rule moves them",
            risk="low",
        ),
    )
    default = "autoclass"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([b.id]) for b in _buckets(ir) if b.intelligent_tiering]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id == "standard":
            return doc
        if strategy_id != "autoclass":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(bucket, "autoclass", True))


class S3Cors(_AcknowledgeOnly, DifferenceRule):
    id = "S3-CORS"
    family = "storage"
    title = "CORS request-header allow-lists do not carry"
    impact = (
        "Cloud Storage CORS has origins, methods, exposed headers and max age, but no allow-list "
        "of request headers: any request header is accepted."
    )
    strategies = (
        Strategy(
            id="map_cors",
            title="Map the CORS rules",
            effect="origins, methods, exposed headers and max age carry; allowed headers do not",
            risk="low",
        ),
    )
    default = "map_cors"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([b.id])
            for b in _buckets(ir)
            if any(r.allowed_headers and r.allowed_headers != ["*"] for r in b.cors_rules)
        ]


class S3Website(DifferenceRule):
    id = "S3-WEBSITE"
    family = "storage"
    title = "Static website hosting has no s3-website endpoint on GCP"
    impact = (
        "Index and error pages carry, but Cloud Storage serves a website only through a load "
        "balancer or a bucket named after a verified domain. The bucket-website URL users know "
        "stops existing, and routing or redirect rules are not carried."
    )
    strategies = (
        Strategy(
            id="website_config",
            title="Carry index and error pages",
            effect=(
                "the bucket gets the same index and error pages; serving it needs a load "
                "balancer or a domain-named bucket, which this family does not create"
            ),
            risk="high",
        ),
        Strategy(id="omit", title="Do not configure", effect="no website settings", risk="medium"),
    )
    default = "website_config"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [b.id],
                redirect_all=b.website.redirect_all,
                routing_rules=b.website.routing_rules,
            )
            for b in _buckets(ir)
            if b.website is not None
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id == "omit":
            return doc
        if strategy_id != "website_config":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        site = {"main_page_suffix": bucket.website.index, "not_found_page": bucket.website.error}
        return _replace_object(doc, _set_hint(bucket, "website", site))


class S3AccessLogging(DifferenceRule):
    id = "S3-ACCESS-LOGGING"
    family = "storage"
    title = "Server access logs become Cloud Storage usage logs"
    impact = (
        "Cloud Storage writes hourly usage logs, in its own CSV format, to a bucket you name. The "
        "target must be migrated too; log consumers must read the new format."
    )
    strategies = (
        Strategy(
            id="usage_logs",
            title="Write usage logs to the migrated target",
            effect="usage logs go to the migrated target bucket, in Cloud Storage's format",
            risk="low",
        ),
        Strategy(id="omit", title="No logging", effect="access is not logged", risk="low"),
    )
    default = "usage_logs"

    def detect(self, ir: IRDocument) -> list[Difference]:
        by_name = {b.bucket_name: b.id for b in _buckets(ir)}
        return [
            self.difference(
                [b.id],
                target=b.logging_bucket,
                target_id=by_name.get(b.logging_bucket or ""),
                prefix=b.logging_prefix,
            )
            for b in _buckets(ir)
            if b.logging_bucket
        ]

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("target_id") is None:
            return (self.strategy("omit"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return "omit" if diff.context.get("target_id") is None else self.default

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {bucket.id}")
        if strategy_id == "omit":
            return doc
        logging = {"target": diff.context["target_id"], "prefix": diff.context.get("prefix")}
        return _replace_object(doc, _set_hint(bucket, "logging", logging))


class S3ObjectLock(DifferenceRule):
    id = "S3-OBJECT-LOCK"
    family = "storage"
    title = "Object Lock default retention becomes a bucket retention policy"
    impact = (
        "A Cloud Storage retention policy keeps every object for a minimum period. Unlocked, an "
        "admin can remove it — weaker than S3 Compliance mode. Locked, it can never be shortened "
        "or removed, and the bucket cannot be deleted until every object ages out."
    )
    strategies = (
        Strategy(
            id="retention_unlocked",
            title="Unlocked retention policy",
            effect="objects are kept for the same period; an admin can still remove the policy",
            risk="medium",
        ),
        Strategy(
            id="retention_locked",
            title="Locked retention policy",
            effect="objects are kept for the same period, irreversibly",
            risk="high",
        ),
        Strategy(
            id="omit",
            title="No retention",
            effect="objects can be deleted at any time",
            risk="high",
        ),
    )
    default = "retention_unlocked"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([b.id], mode=b.object_lock.mode, days=b.object_lock.days)
            for b in _buckets(ir)
            if b.object_lock is not None
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, bucket = _subject(ir, diff)
        if bucket is None:
            return doc
        if strategy_id == "omit":
            return doc
        if strategy_id not in {"retention_unlocked", "retention_locked"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        retention = {"days": diff.context["days"], "locked": strategy_id == "retention_locked"}
        return _replace_object(doc, _set_hint(bucket, "retention", retention))


class S3Replication(_AcknowledgeOnly, DifferenceRule):
    id = "S3-REPLICATION"
    family = "storage"
    title = "Replication is not reproduced"
    impact = (
        "Cloud Storage has no per-rule replication to another bucket. A dual-region bucket, or a "
        "Storage Transfer Service job, covers the same need; neither is generated."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect="no replication exists on the new bucket",
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([b.id], destinations=b.replication_destinations)
            for b in _buckets(ir)
            if b.replication_destinations
        ]


class S3Notifications(_AcknowledgeOnly, DifferenceRule):
    id = "S3-NOTIFICATIONS"
    family = "storage"
    title = "Event notifications are not reproduced"
    impact = (
        "S3 sends events to SNS, SQS, Lambda or EventBridge. Cloud Storage notifications go to "
        "Pub/Sub topics this family does not create, and nothing consumes them yet."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect="no notifications are configured; whatever reacted to new objects stops",
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([b.id], kinds=b.notifications) for b in _buckets(ir) if b.notifications
        ]


class S3Acceleration(_AcknowledgeOnly, DifferenceRule):
    id = "S3-ACCELERATION"
    family = "storage"
    title = "Transfer acceleration has no equivalent"
    impact = "Cloud Storage has no accelerated endpoint; its global network serves the same need."
    strategies = (
        Strategy(
            id="note_only",
            title="Understood",
            effect="clients use the standard Cloud Storage endpoint",
            risk="low",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([b.id]) for b in _buckets(ir) if b.acceleration]
