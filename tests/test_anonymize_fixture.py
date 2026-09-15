import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from anonymize_fixture import FIXTURE_ACCOUNT, anonymize_files  # noqa: E402


def test_ids_accounts_ips_and_times_are_replaced_consistently():
    files = {
        "network.vpcs": [
            {
                "VpcId": "vpc-0a1b2c3d4e5f60718",
                "OwnerId": "538342414628",
                "CidrBlock": "10.0.0.0/16",
            }
        ],
        "network.subnets": [
            {
                "SubnetId": "subnet-09125e7961d295170",
                "VpcId": "vpc-0a1b2c3d4e5f60718",
                "SubnetArn": "arn:aws:ec2:ap-south-1:538342414628:subnet/subnet-09125e7961d295170",
            }
        ],
        "network.nat_gateways": [
            {
                "NatGatewayId": "nat-0123456789abcdef0",
                "CreateTime": datetime(2026, 9, 15, tzinfo=UTC),
                "NatGatewayAddresses": [{"PublicIp": "13.232.1.2"}],
            }
        ],
        "network.vpc_attributes": {"vpc-0a1b2c3d4e5f60718": {"enableDnsSupport": True}},
    }

    result = anonymize_files(files, "538342414628")

    vpc_id = result["network.vpcs"][0]["VpcId"]
    assert vpc_id == "vpc-00000000000000001"
    assert result["network.subnets"][0]["VpcId"] == vpc_id
    assert result["network.vpc_attributes"] == {vpc_id: {"enableDnsSupport": True}}
    assert result["network.vpcs"][0]["OwnerId"] == FIXTURE_ACCOUNT
    assert result["network.vpcs"][0]["CidrBlock"] == "10.0.0.0/16"
    assert result["network.subnets"][0]["SubnetArn"] == (
        "arn:aws:ec2:ap-south-1:123456789012:subnet/subnet-00000000000000001"
    )
    nat = result["network.nat_gateways"][0]
    assert nat["CreateTime"] == "2026-01-01T00:00:00+00:00"
    assert nat["NatGatewayAddresses"] == [{"PublicIp": "203.0.113.1"}]
