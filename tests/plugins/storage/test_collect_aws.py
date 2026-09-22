"""S3 collection (spec 6 §3). Raw responses only."""

import json

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.storage.collect_aws import bucket_region, collect
from easymigrate.providers.aws.source import connect


@pytest.fixture
def aws(aws_credentials):
    with mock_aws():
        yield connect(region="ap-south-1")


def _bucket(aws, name, region="ap-south-1"):
    s3 = aws.session.client("s3", region_name=region)
    kwargs = (
        {}
        if region == "us-east-1"
        else {"CreateBucketConfiguration": {"LocationConstraint": region}}
    )
    s3.create_bucket(Bucket=name, **kwargs)
    return aws.client("s3")


def test_only_buckets_in_the_scanned_region_are_described(aws):
    _bucket(aws, "here")
    _bucket(aws, "there", region="eu-west-2")
    ctx = CollectContext(plugin="storage")

    files = collect(aws, ctx)

    assert list(files["storage.buckets"]) == ["here"]
    assert [f.code for f in ctx.findings] == ["S3-BUCKETS-ELSEWHERE"]
    assert "1 in eu-west-2" in ctx.findings[0].message


def test_configured_and_unconfigured_settings_are_recorded(aws):
    s3 = _bucket(aws, "app-logs")
    s3.put_bucket_versioning(Bucket="app-logs", VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_lifecycle_configuration(
        Bucket="app-logs",
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "tmp/"},
                    "Expiration": {"Days": 30},
                }
            ]
        },
    )
    s3.put_bucket_policy(
        Bucket="app-logs",
        Policy=json.dumps(
            {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": "arn:aws:s3:::app-logs/*",
                    }
                ]
            }
        ),
    )

    entry = collect(aws, CollectContext(plugin="storage"))["storage.buckets"]["app-logs"]

    assert entry["versioning"]["Status"] == "Enabled"
    assert entry["lifecycle"]["Rules"][0]["ID"] == "expire"
    assert entry["cors"] is None  # not configured is null, never a gap
    assert entry["website"] is None
    assert "Policy" in entry["policy"]
    assert "ResponseMetadata" not in entry["versioning"]


def test_a_denied_call_is_a_scan_gap_and_the_bucket_is_still_captured(aws, monkeypatch):
    _bucket(aws, "data")
    original = type(aws).client

    def client(self, service):
        c = original(self, service)

        def deny(**kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetBucketCors")

        c.meta.events.register("before-call.s3.GetBucketCors", deny)
        return c

    monkeypatch.setattr(type(aws), "client", client)
    ctx = CollectContext(plugin="storage")
    files = collect(aws, ctx)

    assert files["storage.buckets"]["data"]["cors"] is None
    assert "Grant s3:GetBucketCORS" in [f.suggestion for f in ctx.findings]


@pytest.mark.parametrize(
    ("location", "region"), [(None, "us-east-1"), ("EU", "eu-west-1"), ("ap-south-1", "ap-south-1")]
)
def test_bucket_region_decodes_legacy_locations(location, region):
    assert bucket_region(location) == region
