"""Storage normalizer: snapshot → `storage_bucket` (spec 6 §4). Faithful capture only."""

from __future__ import annotations

import json
from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, SourceRef
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.ir.storage import (
    CorsRule,
    LifecycleRule,
    ObjectLockRetention,
    StorageBucket,
    Transition,
    Website,
)

_PUBLIC_GRANTEES = {
    "http://acs.amazonaws.com/groups/global/AllUsers",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
}
_BLOCK_FLAGS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
_NOTIFICATION_KINDS = (
    ("TopicConfigurations", "sns"),
    ("QueueConfigurations", "sqs"),
    ("LambdaFunctionConfigurations", "lambda"),
    ("EventBridgeConfiguration", "eventbridge"),
)


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    buckets: dict[str, Any] = snapshot.get("storage.buckets") or {}
    account_block = snapshot.get("storage.account_public_access_block") or {}
    account_blocked = all(account_block.get(flag) for flag in _BLOCK_FLAGS)
    ids = allocate_ids("storage_bucket", [(name, name) for name in buckets])
    return [
        _bucket(ids[name], entry, ctx, account_blocked=account_blocked)
        for name, entry in sorted(buckets.items())
    ]


def _bucket(
    object_id: str, entry: dict[str, Any], ctx: NormalizeContext, *, account_blocked: bool
) -> StorageBucket:
    name = entry["Name"]
    block = ((entry.get("public_access_block") or {}).get("PublicAccessBlockConfiguration")) or {}
    blocked = account_blocked or all(block.get(flag) for flag in _BLOCK_FLAGS)
    encryption, kms_key = _encryption(entry.get("encryption"))
    lock = entry.get("object_lock") or {}
    lock_config = lock.get("ObjectLockConfiguration") or {}
    ownership_rules = ((entry.get("ownership") or {}).get("OwnershipControls") or {}).get(
        "Rules"
    ) or [{"ObjectOwnership": "ObjectWriter"}]
    tags = {t["Key"]: t["Value"] for t in (entry.get("tagging") or {}).get("TagSet", [])}
    return StorageBucket(
        id=object_id,
        name=name,
        labels=clean_labels(tags),
        source=SourceRef(cloud="aws", type="s3-bucket", id=name, region=entry.get("Region")),
        bucket_name=name,
        region=entry.get("Region") or ctx.region,
        versioning=_versioning(entry.get("versioning")),
        encryption=encryption,
        kms_key=kms_key,
        public_access_blocked=blocked,
        public_via_policy=bool(
            ((entry.get("policy_status") or {}).get("PolicyStatus") or {}).get("IsPublic")
        ),
        public_via_acl=any(
            (grant.get("Grantee") or {}).get("URI") in _PUBLIC_GRANTEES
            for grant in (entry.get("acl") or {}).get("Grants", [])
        ),
        has_policy=_has_policy(entry.get("policy")),
        object_ownership=ownership_rules[0].get("ObjectOwnership", "ObjectWriter"),
        lifecycle_rules=[_rule(r) for r in (entry.get("lifecycle") or {}).get("Rules", [])],
        cors_rules=[_cors(r) for r in (entry.get("cors") or {}).get("CORSRules", [])],
        website=_website(entry.get("website")),
        logging_bucket=((entry.get("logging") or {}).get("LoggingEnabled") or {}).get(
            "TargetBucket"
        ),
        logging_prefix=((entry.get("logging") or {}).get("LoggingEnabled") or {}).get(
            "TargetPrefix"
        ),
        object_lock_enabled=lock_config.get("ObjectLockEnabled") == "Enabled",
        object_lock=_retention(lock_config),
        replication_destinations=sorted(
            {
                (rule.get("Destination") or {}).get("Bucket", "")
                for rule in (
                    (entry.get("replication") or {}).get("ReplicationConfiguration") or {}
                ).get("Rules", [])
            }
            - {""}
        ),
        # EventBridge is enabled by an empty object, so presence, not truthiness, counts.
        notifications=sorted(
            kind
            for key, kind in _NOTIFICATION_KINDS
            if (entry.get("notifications") or {}).get(key) is not None
            and (kind == "eventbridge" or (entry.get("notifications") or {}).get(key))
        ),
        requester_pays=(entry.get("request_payment") or {}).get("Payer") == "Requester",
        acceleration=(entry.get("accelerate") or {}).get("Status") == "Enabled",
        # Most buckets use Intelligent-Tiering through a lifecycle transition, with no
        # tiering configuration at all (review, 2026-10-08).
        intelligent_tiering=bool(
            (entry.get("intelligent_tiering") or {}).get("IntelligentTieringConfigurationList")
        )
        or any(
            t.get("StorageClass") == "INTELLIGENT_TIERING"
            for r in (entry.get("lifecycle") or {}).get("Rules", [])
            for t in r.get("Transitions") or []
        ),
    )


def _versioning(response: dict[str, Any] | None) -> str:
    status = (response or {}).get("Status")
    return {"Enabled": "enabled", "Suspended": "suspended"}.get(status or "", "disabled")


def _encryption(response: dict[str, Any] | None) -> tuple[str, str | None]:
    rules = ((response or {}).get("ServerSideEncryptionConfiguration") or {}).get("Rules", [])
    default = (rules[0] if rules else {}).get("ApplyServerSideEncryptionByDefault") or {}
    algorithm = default.get("SSEAlgorithm", "AES256")
    if algorithm == "AES256":
        return "sse_s3", None
    key = default.get("KMSMasterKeyID")
    # No key, or the AWS-managed alias, means AWS holds the key: nothing to carry.
    if not key or key.endswith("alias/aws/s3"):
        key = None
    return ("dsse_kms" if algorithm == "aws:kms:dsse" else "sse_kms"), key


def _has_policy(response: dict[str, Any] | None) -> bool:
    raw = (response or {}).get("Policy")
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("Statement"))
    except ValueError:
        return True


def _transitions(
    items: list[dict[str, Any]], days_key: str, unsupported: list[str], label: str
) -> list[Transition]:
    """Day-based transitions only. A transition on a calendar date has no Cloud Storage
    condition; reading its missing Days as 0 archived every object at once (review,
    2026-10-08), so it is recorded as unsupported instead."""
    kept = []
    for item in items:
        days = item.get(days_key)
        if days is None:
            unsupported.append(f"{label} to {item.get('StorageClass', '?')} on a date")
            continue
        kept.append(Transition(days=days, storage_class=item.get("StorageClass", "")))
    return kept


def _rule(rule: dict[str, Any]) -> LifecycleRule:
    filt = rule.get("Filter") or {}
    conditions = filt.get("And") or filt
    prefix = conditions.get("Prefix", rule.get("Prefix"))
    expiration = rule.get("Expiration") or {}
    noncurrent = rule.get("NoncurrentVersionExpiration") or {}
    unsupported: list[str] = []
    if expiration.get("Date"):
        unsupported.append("expiration on a date")
    if expiration.get("ExpiredObjectDeleteMarker"):
        unsupported.append("removal of expired delete markers (Cloud Storage has none)")
    transitions = _transitions(rule.get("Transitions") or [], "Days", unsupported, "transition")
    noncurrent_transitions = _transitions(
        rule.get("NoncurrentVersionTransitions") or [],
        "NoncurrentDays",
        unsupported,
        "noncurrent transition",
    )
    return LifecycleRule(
        id=rule.get("ID") or "rule",
        enabled=rule.get("Status") == "Enabled",
        prefix=prefix or None,
        filtered_by_tags=bool(conditions.get("Tag") or conditions.get("Tags")),
        filtered_by_size=bool(
            conditions.get("ObjectSizeGreaterThan") or conditions.get("ObjectSizeLessThan")
        ),
        transitions=transitions,
        expiration_days=expiration.get("Days"),
        noncurrent_transitions=noncurrent_transitions,
        noncurrent_expiration_days=noncurrent.get("NoncurrentDays"),
        newer_noncurrent_versions=noncurrent.get("NewerNoncurrentVersions"),
        abort_multipart_days=(rule.get("AbortIncompleteMultipartUpload") or {}).get(
            "DaysAfterInitiation"
        ),
        unsupported=unsupported,
    )


def _cors(rule: dict[str, Any]) -> CorsRule:
    return CorsRule(
        origins=rule.get("AllowedOrigins") or [],
        methods=rule.get("AllowedMethods") or [],
        allowed_headers=rule.get("AllowedHeaders") or [],
        expose_headers=rule.get("ExposeHeaders") or [],
        max_age=rule.get("MaxAgeSeconds"),
    )


def _website(response: dict[str, Any] | None) -> Website | None:
    if not response:
        return None
    redirect = response.get("RedirectAllRequestsTo") or {}
    return Website(
        index=(response.get("IndexDocument") or {}).get("Suffix"),
        error=(response.get("ErrorDocument") or {}).get("Key"),
        redirect_all=redirect.get("HostName"),
        routing_rules=len(response.get("RoutingRules") or []),
    )


def _retention(config: dict[str, Any]) -> ObjectLockRetention | None:
    default = (config.get("Rule") or {}).get("DefaultRetention") or {}
    if not default.get("Mode"):
        return None
    days = default.get("Days") or (default.get("Years") or 0) * 365
    return ObjectLockRetention(mode=default["Mode"], days=days)
