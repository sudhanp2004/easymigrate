"""AWS→GCP storage rules (spec 6 §5)."""

from __future__ import annotations

import pytest

from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.gcs_facts import name_problem, sanitized
from easymigrate.differences.rules.aws_gcp.storage import (
    S3Acceleration,
    S3AccessLogging,
    S3Acls,
    S3BucketName,
    S3BucketPolicy,
    S3Cors,
    S3EncryptionKms,
    S3IntelligentTiering,
    S3Lifecycle,
    S3Notifications,
    S3ObjectLock,
    S3ObjectsNotCopied,
    S3PublicAccess,
    S3Replication,
    S3Website,
    gcs_lifecycle,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.storage import (
    CorsRule,
    LifecycleRule,
    ObjectLockRetention,
    StorageBucket,
    Transition,
    Website,
)


def _bucket(name="assets", **kw):
    return StorageBucket(
        id=f"storage_bucket:{name.replace('.', '-')}",
        name=name,
        source=SourceRef(cloud="aws", type="s3-bucket", id=name, region="ap-south-1"),
        bucket_name=name,
        region="ap-south-1",
        object_ownership=kw.pop("object_ownership", "BucketOwnerEnforced"),
        **kw,
    )


def _doc(*buckets):
    return IRDocument(source={"cloud": "aws"}, objects=list(buckets))


def _only(rule, doc):
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    return diffs[0]


def _hints(doc, obj_id="storage_bucket:assets"):
    return doc.get(obj_id).target_hints


def test_every_bucket_is_stated_empty_at_high_risk():
    rule = S3ObjectsNotCopied()
    assert rule.risk_for(_only(rule, _doc(_bucket())), "note_only") == "high"


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("assets", None),
        ("www.example.com", "dotted"),
        ("goog-data", "goog"),
        ("my-google-x", "google"),
        ("1.2.3.4", "dotted"),
    ],
)
def test_gcs_name_rules(name, problem):
    found = name_problem(name)
    assert (found is None) if problem is None else (problem in found)


def test_sanitized_names_are_valid():
    for name in ("www.example.com", "goog-data", "my-google-x"):
        assert name_problem(sanitized(name)) is None


def test_valid_names_are_kept():
    rule = S3BucketName()
    doc = _doc(_bucket())
    diff = _only(rule, doc)
    assert rule.default_strategy(diff) == "keep_name"
    assert _hints(rule.apply(doc, diff, "keep_name")) == {"gcs_name": "assets"}


def test_dotted_names_must_be_renamed():
    rule = S3BucketName()
    doc = _doc(_bucket("www.example.com"))
    diff = _only(rule, doc)
    assert [s.id for s in rule.allowed_strategies(diff)] == ["rename"]
    params = rule.default_params(diff, "rename")
    assert params == {"name": "www-example-com"}
    result = rule.apply(doc, diff, "rename", params)
    assert _hints(result, "storage_bucket:www-example-com") == {"gcs_name": "www-example-com"}


def test_rename_rejects_invalid_or_duplicate_names():
    rule = S3BucketName()
    doc = _doc(_bucket(), _bucket("logs"))
    diff = rule.detect(doc)[0]
    with pytest.raises(ValueError, match="dotted"):
        rule.validate_params(diff, "rename", {"name": "a.b.c"})
    with pytest.raises(ValueError, match="another scanned bucket"):
        rule.validate_params(diff, "rename", {"name": "logs"})


def test_public_bucket_defaults_private_and_can_stay_public():
    rule = S3PublicAccess()
    doc = _doc(_bucket(public_via_policy=True))
    diff = _only(rule, doc)
    assert rule.default == "private"
    assert _hints(rule.apply(doc, diff, "private")) == {"public": False}
    assert rule.risk_for(diff, "public_read") == "high"
    assert _hints(rule.apply(doc, diff, "public_read")) == {"public": True}


def test_blocked_or_private_buckets_are_not_public_differences():
    assert (
        S3PublicAccess().detect(_doc(_bucket(public_via_acl=True, public_access_blocked=True)))
        == []
    )
    assert S3PublicAccess().detect(_doc(_bucket())) == []


def test_acl_enabled_bucket_defaults_to_uniform_access():
    rule = S3Acls()
    doc = _doc(_bucket(object_ownership="ObjectWriter"))
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "uniform_access")) == {"uniform_access": True}
    assert _hints(rule.apply(doc, diff, "fine_grained")) == {"uniform_access": False}
    assert rule.detect(_doc(_bucket())) == []


def test_lifecycle_maps_classes_expiry_noncurrent_and_multipart():
    rule = LifecycleRule(
        id="archive",
        prefix="logs/",
        transitions=[
            Transition(days=30, storage_class="STANDARD_IA"),
            Transition(days=90, storage_class="GLACIER"),
        ],
        expiration_days=365,
        noncurrent_expiration_days=7,
        newer_noncurrent_versions=2,
        abort_multipart_days=3,
    )
    rules, dropped = gcs_lifecycle(rule)
    assert dropped == []
    actions = [(r["action"]["type"], r["action"].get("storage_class")) for r in rules]
    assert actions == [
        ("SetStorageClass", "NEARLINE"),
        ("SetStorageClass", "ARCHIVE"),
        ("Delete", None),
        ("Delete", None),
        ("AbortIncompleteMultipartUpload", None),
    ]
    archive = rules[1]["condition"]
    assert archive == {
        "matches_prefix": ["logs/"],
        "age": 90,
        "with_state": "LIVE",
        "matches_storage_class": ["STANDARD", "NEARLINE", "COLDLINE"],
    }
    assert rules[3]["condition"] == {
        "matches_prefix": ["logs/"],
        "with_state": "ARCHIVED",
        "days_since_noncurrent_time": 7,
        "num_newer_versions": 2,
    }


def test_tag_filtered_and_disabled_rules_are_dropped_and_raise_risk():
    rule = S3Lifecycle()
    doc = _doc(
        _bucket(
            lifecycle_rules=[
                LifecycleRule(id="tagged", filtered_by_tags=True, expiration_days=1),
                LifecycleRule(id="off", enabled=False, expiration_days=1),
                LifecycleRule(id="ok", expiration_days=10),
            ]
        )
    )
    diff = _only(rule, doc)
    assert len(diff.context["dropped"]) == 2
    assert rule.risk_for(diff, "map_rules") == "medium"
    assert len(_hints(rule.apply(doc, diff, "map_rules"))["lifecycle"]) == 1


def test_intelligent_tiering_becomes_autoclass():
    rule = S3IntelligentTiering()
    doc = _doc(_bucket(intelligent_tiering=True))
    assert _hints(rule.apply(doc, _only(rule, doc), "autoclass")) == {"autoclass": True}


def test_cors_fires_only_when_request_headers_are_restricted():
    restricted = _bucket(
        cors_rules=[CorsRule(origins=["*"], methods=["GET"], allowed_headers=["x-a"])]
    )
    open_ = _bucket(cors_rules=[CorsRule(origins=["*"], methods=["GET"], allowed_headers=["*"])])
    assert len(S3Cors().detect(_doc(restricted))) == 1
    assert S3Cors().detect(_doc(open_)) == []


def test_website_carries_pages_at_high_risk():
    rule = S3Website()
    doc = _doc(_bucket(website=Website(index="index.html", error="404.html")))
    diff = _only(rule, doc)
    assert rule.risk_for(diff, "website_config") == "high"
    assert _hints(rule.apply(doc, diff, "website_config"))["website"] == {
        "main_page_suffix": "index.html",
        "not_found_page": "404.html",
    }


def test_access_logging_needs_a_migrated_target():
    rule = S3AccessLogging()
    doc = _doc(_bucket(logging_bucket="logs", logging_prefix="a/"), _bucket("logs"))
    diff = rule.detect(doc)[0]
    assert _hints(rule.apply(doc, diff, "usage_logs"))["logging"] == {
        "target": "storage_bucket:logs",
        "prefix": "a/",
    }
    lonely = _only(rule, _doc(_bucket(logging_bucket="elsewhere")))
    assert [s.id for s in rule.allowed_strategies(lonely)] == ["omit"]


def test_object_lock_becomes_an_unlocked_retention_policy_by_default():
    rule = S3ObjectLock()
    doc = _doc(
        _bucket(
            object_lock_enabled=True, object_lock=ObjectLockRetention(mode="COMPLIANCE", days=30)
        )
    )
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "retention_unlocked"))["retention"] == {
        "days": 30,
        "locked": False,
    }
    assert rule.risk_for(diff, "retention_locked") == "high"


@pytest.mark.parametrize(
    ("rule", "bucket"),
    [
        (S3BucketPolicy(), _bucket(has_policy=True)),
        (S3Replication(), _bucket(replication_destinations=["arn:aws:s3:::dr"])),
        (S3Notifications(), _bucket(notifications=["sqs"])),
    ],
)
def test_manual_rules_keep_the_bucket(rule, bucket):
    doc = _doc(bucket)
    diff = _only(rule, doc)
    assert rule.strategy(rule.default).manual is True
    assert rule.apply(doc, diff, "manual").get(bucket.id).excluded is None


def test_kms_and_acceleration_fire_only_when_present():
    assert len(S3EncryptionKms().detect(_doc(_bucket(kms_key="arn:kms")))) == 1
    assert S3EncryptionKms().detect(_doc(_bucket())) == []
    assert len(S3Acceleration().detect(_doc(_bucket(acceleration=True)))) == 1


def test_bucket_name_runs_before_access_logging():
    ids = [rule.id for rule in AWS_GCP_RULES]
    assert ids.index("S3-BUCKET-NAME") < ids.index("S3-ACCESS-LOGGING")


def test_object_lock_retention_turns_versioning_off_and_per_object_keeps_it():
    rule = S3ObjectLock()
    doc = _doc(
        _bucket(
            versioning="enabled",
            object_lock_enabled=True,
            object_lock=ObjectLockRetention(mode="GOVERNANCE", days=30),
        )
    )
    diff = _only(rule, doc)
    assert _hints(rule.apply(doc, diff, "retention_unlocked"))["versioning_off"] is True
    assert _hints(rule.apply(doc, diff, "object_retention")) == {"object_retention": True}


def test_autoclass_is_not_the_default_when_lifecycle_moves_classes():
    lifecycle = S3Lifecycle()
    tiering = S3IntelligentTiering()
    bucket = _bucket(
        intelligent_tiering=True,
        lifecycle_rules=[
            LifecycleRule(
                id="a",
                transitions=[Transition(days=30, storage_class="GLACIER")],
                expiration_days=90,
            )
        ],
    )
    doc = _doc(bucket)
    doc = lifecycle.apply(doc, _only(lifecycle, doc), "map_rules")
    diff = _only(tiering, doc)
    assert tiering.default_strategy(diff) == "standard"
    assert tiering.risk_for(diff, "autoclass") == "medium"
    hints = _hints(tiering.apply(doc, diff, "autoclass"))
    assert [r["action"]["type"] for r in hints["lifecycle"]] == ["Delete"]
