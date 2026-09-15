"""Collect raw AWS networking API responses for one region (spec §2.1, collector)."""

from typing import Any

from easymigrate.core.plugin import CollectContext
from easymigrate.providers.aws.source import AwsConnection, is_access_denied

# (snapshot name, boto3 method, response list key, id field, IAM action)
_LISTS = (
    ("network.vpcs", "describe_vpcs", "Vpcs", "VpcId", "ec2:DescribeVpcs"),
    ("network.subnets", "describe_subnets", "Subnets", "SubnetId", "ec2:DescribeSubnets"),
    (
        "network.route_tables",
        "describe_route_tables",
        "RouteTables",
        "RouteTableId",
        "ec2:DescribeRouteTables",
    ),
    (
        "network.internet_gateways",
        "describe_internet_gateways",
        "InternetGateways",
        "InternetGatewayId",
        "ec2:DescribeInternetGateways",
    ),
    (
        "network.nat_gateways",
        "describe_nat_gateways",
        "NatGateways",
        "NatGatewayId",
        "ec2:DescribeNatGateways",
    ),
    (
        "network.security_groups",
        "describe_security_groups",
        "SecurityGroups",
        "GroupId",
        "ec2:DescribeSecurityGroups",
    ),
    (
        "network.network_acls",
        "describe_network_acls",
        "NetworkAcls",
        "NetworkAclId",
        "ec2:DescribeNetworkAcls",
    ),
)
_VPC_ATTRIBUTES = (
    ("enableDnsSupport", "EnableDnsSupport"),
    ("enableDnsHostnames", "EnableDnsHostnames"),
)

NETWORK_SNAPSHOT_FILES = (*(entry[0] for entry in _LISTS), "network.vpc_attributes")


def _api_name(method: str) -> str:
    return "".join(part.title() for part in method.split("_"))


def collect(connection: AwsConnection, ctx: CollectContext) -> dict[str, Any]:
    ec2 = connection.client("ec2")
    files: dict[str, Any] = {}
    for name, method, key, id_field, action in _LISTS:
        items: list[dict[str, Any]] = []
        try:
            for page in ec2.get_paginator(method).paginate():
                items.extend(page.get(key, []))
        except Exception as exc:
            if is_access_denied(exc):
                ctx.access_denied(_api_name(method), action)
                continue
            raise
        files[name] = sorted(items, key=lambda item: item[id_field])

    if "network.vpcs" in files:
        attributes = _collect_vpc_attributes(ec2, files["network.vpcs"], ctx)
        if attributes is not None:
            files["network.vpc_attributes"] = attributes
    return files


def _collect_vpc_attributes(
    ec2: Any, vpcs: list[dict[str, Any]], ctx: CollectContext
) -> dict[str, dict[str, bool]] | None:
    attributes: dict[str, dict[str, bool]] = {}
    for vpc in vpcs:
        vpc_id = vpc["VpcId"]
        entry: dict[str, bool] = {}
        for attribute, response_key in _VPC_ATTRIBUTES:
            try:
                response = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=attribute)
            except Exception as exc:
                if is_access_denied(exc):
                    ctx.access_denied("DescribeVpcAttribute", "ec2:DescribeVpcAttribute")
                    return None
                raise
            entry[attribute] = bool(response[response_key]["Value"])
        attributes[vpc_id] = entry
    return attributes
