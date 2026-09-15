from moto import mock_aws

from easymigrate.core.plugin import CollectContext, NormalizeContext, Plugin, Snapshot
from easymigrate.ir import IRDocument
from easymigrate.plugins.network import NetworkPlugin
from easymigrate.providers.aws.source import connect
from tests.plugins.network import aws_builders as aws


def test_plugin_satisfies_protocol_and_declares_resource_types():
    plugin: Plugin = NetworkPlugin()

    assert plugin.name == "network"
    assert "ec2:vpc" in plugin.aws_resource_types
    assert "ec2:natgateway" in plugin.aws_resource_types
    assert "rds:db" not in plugin.aws_resource_types


def test_normalize_returns_every_kind_in_stable_order():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main")],
        subnets=[aws.subnet("subnet-1", "vpc-1", name="app")],
        route_tables=[
            aws.route_table(
                "rtb-1",
                "vpc-1",
                [
                    aws.route("0.0.0.0/0", NatGatewayId="nat-1"),
                    aws.route("172.16.0.0/16", VpcPeeringConnectionId="pcx-1"),
                ],
                subnets=("subnet-1",),
            ),
        ],
        nat_gateways=[aws.nat_gateway("nat-1", "vpc-1", "subnet-1")],
        security_groups=[aws.security_group("sg-1", "vpc-1", "web")],
        network_acls=[aws.network_acl("acl-1", "vpc-1", [aws.acl_entry(10, action="deny")])],
    )
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)

    objects = NetworkPlugin().normalize(snap, ctx)

    assert [o.kind for o in objects] == [
        "network",
        "subnet",
        "nat",
        "firewall_policy",
        "network_acl",
        "custom_route",
    ]
    IRDocument(source={"cloud": "aws"}, objects=objects)


def test_normalize_empty_snapshot():
    ctx = NormalizeContext(account_id="1", region="ap-south-1", strict=True)

    assert NetworkPlugin().normalize(Snapshot({}), ctx) == []


def test_collect_aws_delegates_to_collector(aws_credentials):
    with mock_aws():
        conn = connect(region="ap-south-1")
        files = NetworkPlugin().collect_aws(conn, CollectContext(plugin="network"))

    assert "network.vpcs" in files
