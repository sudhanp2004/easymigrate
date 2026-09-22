"""S3 normalization (spec 6 §4)."""

from __future__ import annotations

import json
from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.storage import StorageBucket
from easymigrate.plugins.storage.normalize import normalize

POLICY = json.dumps(
    {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}]}
)


def entry(name: str = "assets", **responses: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"Name": name, "Region": "ap-south-1"}
    base.update(responses)
    return base


def run(*entries, account_block=None) -> list[StorageBucket]:
    files = {
        "storage.buckets": {e["Name"]: e for e in entries},
        "storage.account_public_access_block": account_block,
    }
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=False)
    return normalize(Snapshot(files), ctx)


def test_a_bare_bucket_is_private_sse_s3_unversioned():
    bucket = run(entry())[0]
    assert bucket.id == "storage_bucket:assets"
    assert (bucket.versioning, bucket.encryption, bucket.kms_key) == ("disabled", "sse_s3", None)
    assert bucket.object_ownership == "ObjectWriter"  # no ownership controls means ACLs on
    assert not bucket.public_via_policy and not bucket.has_policy


def test_public_policy_and_acl_are_captured():
    acl = {"Grants": [{"Grantee": {"URI": "http://acs.amazonaws.com/groups/global/AllUsers"}}]}
    bucket = run(
        entry(
            policy={"Policy": POLICY}, policy_status={"PolicyStatus": {"IsPublic": True}}, acl=acl
        )
    )[0]
    assert bucket.has_policy and bucket.public_via_policy and bucket.public_via_acl


def test_block_public_access_counts_at_bucket_or_account_level():
    flags = {
        f: True
        for f in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
    }
    assert run(entry(public_access_block={"PublicAccessBlockConfiguration": flags}))[
        0
    ].public_access_blocked
    assert run(entry(), account_block=flags)[0].public_access_blocked
    assert not run(entry())[0].public_access_blocked


def test_customer_managed_kms_key_is_kept_and_aws_managed_is_not():
    def enc(key):
        return {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": key,
                        }
                    }
                ]
            }
        }

    assert run(entry(encryption=enc("arn:aws:kms:ap-south-1:1:key/k")))[0].kms_key.endswith("key/k")
    assert run(entry(encryption=enc("arn:aws:kms:ap-south-1:1:alias/aws/s3")))[0].kms_key is None


def test_lifecycle_rules_are_captured_faithfully():
    lifecycle = {
        "Rules": [
            {
                "ID": "archive",
                "Status": "Enabled",
                "Filter": {"And": {"Prefix": "logs/", "Tags": [{"Key": "k", "Value": "v"}]}},
                "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}],
                "Expiration": {"Days": 365},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 7, "NewerNoncurrentVersions": 2},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3},
            },
            {"ID": "off", "Status": "Disabled", "Filter": {}, "Expiration": {"Days": 1}},
        ]
    }
    first, second = run(entry(lifecycle=lifecycle))[0].lifecycle_rules
    assert (first.prefix, first.filtered_by_tags) == ("logs/", True)
    assert first.transitions[0].storage_class == "STANDARD_IA"
    assert (first.expiration_days, first.noncurrent_expiration_days) == (365, 7)
    assert (first.newer_noncurrent_versions, first.abort_multipart_days) == (2, 3)
    assert second.enabled is False


def test_website_lock_replication_notifications_and_flags():
    bucket = run(
        entry(
            website={
                "IndexDocument": {"Suffix": "index.html"},
                "ErrorDocument": {"Key": "404.html"},
            },
            object_lock={
                "ObjectLockConfiguration": {
                    "ObjectLockEnabled": "Enabled",
                    "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Years": 1}},
                }
            },
            replication={
                "ReplicationConfiguration": {
                    "Rules": [{"Destination": {"Bucket": "arn:aws:s3:::dr"}}]
                }
            },
            notifications={"QueueConfigurations": [{"QueueArn": "q"}]},
            request_payment={"Payer": "Requester"},
            accelerate={"Status": "Enabled"},
            intelligent_tiering={"IntelligentTieringConfigurationList": [{"Id": "t"}]},
            versioning={"Status": "Suspended"},
            tagging={"TagSet": [{"Key": "team", "Value": "web"}]},
        )
    )[0]
    assert (bucket.website.index, bucket.website.error) == ("index.html", "404.html")
    assert (bucket.object_lock.mode, bucket.object_lock.days) == ("COMPLIANCE", 365)
    assert bucket.replication_destinations == ["arn:aws:s3:::dr"]
    assert bucket.notifications == ["sqs"]
    assert bucket.requester_pays and bucket.acceleration and bucket.intelligent_tiering
    assert bucket.versioning == "suspended"
    assert bucket.labels == {"team": "web"}
