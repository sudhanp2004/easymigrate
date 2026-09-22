"""Collect raw S3 bucket configuration for one region (spec 6 §3). No interpretation.

`ListBuckets` is global, so each bucket's region comes from `GetBucketLocation`; only buckets in
the scanned region are described.
"""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.ir.base import Finding
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

STORAGE_SNAPSHOT_FILES = ("storage.buckets", "storage.account_public_access_block")

# (snapshot key, boto3 method, IAM action, error codes meaning "not configured")
_BUCKET_CALLS: tuple[tuple[str, str, str, frozenset[str]], ...] = (
    ("versioning", "get_bucket_versioning", "s3:GetBucketVersioning", frozenset()),
    (
        "encryption",
        "get_bucket_encryption",
        "s3:GetEncryptionConfiguration",
        frozenset({"ServerSideEncryptionConfigurationNotFoundError"}),
    ),
    (
        "public_access_block",
        "get_public_access_block",
        "s3:GetBucketPublicAccessBlock",
        frozenset({"NoSuchPublicAccessBlockConfiguration"}),
    ),
    (
        "policy_status",
        "get_bucket_policy_status",
        "s3:GetBucketPolicyStatus",
        frozenset({"NoSuchBucketPolicy"}),
    ),
    ("policy", "get_bucket_policy", "s3:GetBucketPolicy", frozenset({"NoSuchBucketPolicy"})),
    ("acl", "get_bucket_acl", "s3:GetBucketAcl", frozenset()),
    (
        "ownership",
        "get_bucket_ownership_controls",
        "s3:GetBucketOwnershipControls",
        frozenset({"OwnershipControlsNotFoundError"}),
    ),
    (
        "lifecycle",
        "get_bucket_lifecycle_configuration",
        "s3:GetLifecycleConfiguration",
        frozenset({"NoSuchLifecycleConfiguration"}),
    ),
    ("cors", "get_bucket_cors", "s3:GetBucketCORS", frozenset({"NoSuchCORSConfiguration"})),
    (
        "website",
        "get_bucket_website",
        "s3:GetBucketWebsite",
        frozenset({"NoSuchWebsiteConfiguration"}),
    ),
    ("logging", "get_bucket_logging", "s3:GetBucketLogging", frozenset()),
    (
        "object_lock",
        "get_object_lock_configuration",
        "s3:GetBucketObjectLockConfiguration",
        frozenset({"ObjectLockConfigurationNotFoundError"}),
    ),
    (
        "replication",
        "get_bucket_replication",
        "s3:GetReplicationConfiguration",
        frozenset({"ReplicationConfigurationNotFoundError"}),
    ),
    (
        "notifications",
        "get_bucket_notification_configuration",
        "s3:GetBucketNotification",
        frozenset(),
    ),
    ("request_payment", "get_bucket_request_payment", "s3:GetBucketRequestPayment", frozenset()),
    (
        "accelerate",
        "get_bucket_accelerate_configuration",
        "s3:GetAccelerateConfiguration",
        frozenset(),
    ),
    (
        "intelligent_tiering",
        "list_bucket_intelligent_tiering_configurations",
        "s3:GetIntelligentTieringConfiguration",
        frozenset(),
    ),
    ("tagging", "get_bucket_tagging", "s3:GetBucketTagging", frozenset({"NoSuchTagSet"})),
)

# Everything a boto3 response carries that is transport, not configuration.
_TRANSPORT = {"ResponseMetadata"}


def bucket_region(location: str | None) -> str:
    """`GetBucketLocation` reports us-east-1 as no constraint and eu-west-1 as the legacy `EU`."""
    if not location:
        return "us-east-1"
    return "eu-west-1" if location == "EU" else location


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    s3 = connection.client("s3")
    files: dict[str, Any] = {"storage.buckets": {}, "storage.account_public_access_block": None}
    try:
        buckets = s3.list_buckets().get("Buckets", [])
    except Exception as exc:
        if is_access_denied(exc):
            ctx.access_denied("ListBuckets", "s3:ListAllMyBuckets")
            return files
        raise

    elsewhere: dict[str, int] = {}
    for bucket in sorted(buckets, key=lambda b: b["Name"]):
        name = bucket["Name"]
        try:
            location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied("GetBucketLocation", "s3:GetBucketLocation")
                continue
            raise
        region = bucket_region(location)
        if region != connection.region:
            elsewhere[region] = elsewhere.get(region, 0) + 1
            continue
        files["storage.buckets"][name] = _describe(s3, name, region, ctx)

    if elsewhere:
        where = ", ".join(f"{count} in {region}" for region, count in sorted(elsewhere.items()))
        ctx.findings.append(
            Finding(
                level="info",
                code="S3-BUCKETS-ELSEWHERE",
                message=(
                    f"Buckets in other regions are not part of this scan ({where}); scan each "
                    "region to migrate its buckets."
                ),
            )
        )

    files["storage.account_public_access_block"] = _account_block(connection, ctx)
    return files


def _describe(s3: Any, name: str, region: str, ctx: CollectContext) -> dict[str, Any]:
    entry: dict[str, Any] = {"Name": name, "Region": region}
    for key, method, action, not_configured in _BUCKET_CALLS:
        try:
            response = getattr(s3, method)(Bucket=name)
        except Exception as exc:
            code = _error_code(exc)
            if code in not_configured:
                entry[key] = None
                continue
            if is_access_denied(exc):
                ctx.access_denied(method, action)
                entry[key] = None
                continue
            raise
        entry[key] = {k: v for k, v in response.items() if k not in _TRANSPORT}
    return entry


def _account_block(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any] | None:
    try:
        response = connection.client("s3control").get_public_access_block(
            AccountId=connection.account_id
        )
    except Exception as exc:
        if _error_code(exc) == "NoSuchPublicAccessBlockConfiguration":
            return None
        if is_access_denied(exc):
            ctx.access_denied("GetPublicAccessBlock", "s3:GetAccountPublicAccessBlock")
            return None
        raise
    return response.get("PublicAccessBlockConfiguration")
