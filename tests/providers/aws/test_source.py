import pytest
from botocore.stub import Stubber
from moto import mock_aws

from easymigrate.providers.aws import source
from easymigrate.providers.aws.source import (
    AwsConnectionError,
    Inventory,
    arn_resource_type,
    connect,
    inventory,
    is_access_denied,
)


def test_connect_returns_account_and_region(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")

    assert conn.account_id == "123456789012"
    assert conn.region == "ap-south-1"


def test_client_uses_region_and_adaptive_retries(aws_credentials):
    with mock_aws():
        client = connect(region="ap-south-1").client("ec2")

    assert client.meta.region_name == "ap-south-1"
    # botocore stores max_attempts=10 retries as 11 total attempts
    assert client.meta.config.retries == {"mode": "adaptive", "total_max_attempts": 11}


def test_connect_assumes_role(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1", role_arn="arn:aws:iam::123456789012:role/scanner")
        creds = conn.session.get_credentials().get_frozen_credentials()

    assert creds.access_key != "testing"
    assert creds.token


def test_unknown_region_is_rejected(aws_credentials):
    with pytest.raises(AwsConnectionError, match="unknown AWS region 'mars-1'"):
        connect(region="mars-1")


def test_missing_profile_is_rejected(aws_credentials):
    with pytest.raises(AwsConnectionError, match="AWS profile 'nope' not found"):
        connect(region="ap-south-1", profile="nope")


def test_missing_credentials_are_rejected(aws_credentials, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    with pytest.raises(AwsConnectionError, match="no AWS credentials found"):
        connect(region="ap-south-1")


@pytest.mark.parametrize(
    "arn, expected",
    [
        ("arn:aws:ec2:ap-south-1:123456789012:vpc/vpc-1", "ec2:vpc"),
        ("arn:aws:ec2:ap-south-1:123456789012:security-group/sg-1", "ec2:security-group"),
        ("arn:aws:lambda:ap-south-1:123456789012:function:handler", "lambda:function"),
        ("arn:aws:rds:ap-south-1:123456789012:db:test-postgres-db", "rds:db"),
        ("arn:aws:s3:::my-bucket", "s3"),
        ("not-an-arn", "unknown"),
    ],
)
def test_arn_resource_type(arn, expected):
    assert arn_resource_type(arn) == expected


def test_inventory_groups_tagged_resources_by_type(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        ec2 = conn.client("ec2")
        for name in ("b", "a"):
            ec2.create_vpc(
                CidrBlock="10.0.0.0/16",
                TagSpecifications=[
                    {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": name}]}
                ],
            )
        result = inventory(conn)

    assert result.access_denied is False
    assert list(result.arns_by_type) == ["ec2:vpc"]
    assert len(result.arns_by_type["ec2:vpc"]) == 2
    assert result.arns_by_type["ec2:vpc"] == sorted(result.arns_by_type["ec2:vpc"])


def test_inventory_reports_access_denied(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        client = conn.session.client("resourcegroupstaggingapi", region_name="ap-south-1")
        stubber = Stubber(client)
        stubber.add_client_error("get_resources", service_error_code="AccessDeniedException")
        monkeypatch.setattr(source.AwsConnection, "client", lambda self, service: client)
        with stubber:
            result = inventory(conn)

    assert result == Inventory(arns_by_type={}, access_denied=True)


def test_is_access_denied_recognises_codes():
    from botocore.exceptions import ClientError

    def error(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "m"}}, "DescribeVpcs")

    assert is_access_denied(error("UnauthorizedOperation"))
    assert is_access_denied(error("AccessDenied"))
    assert is_access_denied(error("AccessDeniedException"))
    assert not is_access_denied(error("Throttling"))
    assert not is_access_denied(ValueError("x"))
