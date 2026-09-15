import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from easymigrate.core.plugin import CollectContext
from easymigrate.plugins.network.collect_aws import NETWORK_SNAPSHOT_FILES, collect
from easymigrate.providers.aws import source
from easymigrate.providers.aws.source import connect


def _build_network(ec2):
    vpc = ec2.create_vpc(
        CidrBlock="10.0.0.0/16",
        TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
    )["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")[
        "Subnet"
    ]["SubnetId"]
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.create_nat_gateway(SubnetId=subnet, AllocationId=eip)
    ec2.create_security_group(GroupName="web", Description="web", VpcId=vpc)
    ec2.create_network_acl(VpcId=vpc)
    return vpc


def _deny(connection_cls, monkeypatch, operation: str, code: str = "UnauthorizedOperation"):
    original = connection_cls.client

    def client(self, service):
        c = original(self, service)

        def raise_error(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)

        c.meta.events.register(f"before-call.ec2.{operation}", raise_error)
        return c

    monkeypatch.setattr(connection_cls, "client", client)


def test_collects_every_network_file(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        vpc = _build_network(conn.client("ec2"))
        ctx = CollectContext(plugin="network")
        files = collect(conn, ctx)

    assert sorted(files) == sorted(NETWORK_SNAPSHOT_FILES)
    assert ctx.findings == []
    assert vpc in [v["VpcId"] for v in files["network.vpcs"]]
    assert files["network.vpc_attributes"][vpc] == {
        "enableDnsSupport": True,
        "enableDnsHostnames": True,
    }
    assert len(files["network.nat_gateways"]) == 1
    assert "web" in [g["GroupName"] for g in files["network.security_groups"]]


def test_lists_are_sorted_by_id(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        ec2 = conn.client("ec2")
        for cidr in ("10.1.0.0/16", "10.2.0.0/16", "10.3.0.0/16"):
            ec2.create_vpc(CidrBlock=cidr)
        files = collect(conn, CollectContext(plugin="network"))

    ids = [v["VpcId"] for v in files["network.vpcs"]]
    assert ids == sorted(ids)


def test_access_denied_skips_only_that_file(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _build_network(conn.client("ec2"))
        _deny(source.AwsConnection, monkeypatch, "DescribeSubnets")
        ctx = CollectContext(plugin="network")
        files = collect(conn, ctx)

    assert "network.subnets" not in files
    assert "network.vpcs" in files
    assert [f.suggestion for f in ctx.findings] == ["Grant ec2:DescribeSubnets"]


def test_denied_vpc_attributes_drops_the_whole_attribute_file(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _build_network(conn.client("ec2"))
        _deny(source.AwsConnection, monkeypatch, "DescribeVpcAttribute", code="AccessDenied")
        ctx = CollectContext(plugin="network")
        files = collect(conn, ctx)

    assert "network.vpc_attributes" not in files
    assert "network.vpcs" in files
    assert [f.suggestion for f in ctx.findings] == ["Grant ec2:DescribeVpcAttribute"]


def test_other_errors_propagate(aws_credentials, monkeypatch):
    with mock_aws():
        conn = connect(region="ap-south-1")
        _deny(source.AwsConnection, monkeypatch, "DescribeVpcs", code="InvalidParameterValue")

        with pytest.raises(ClientError, match="InvalidParameterValue"):
            collect(conn, CollectContext(plugin="network"))
