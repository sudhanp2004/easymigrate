"""Tiny builders for AWS-shaped API dicts, used by normalizer unit tests."""

from typing import Any

from easymigrate.core.plugin import Snapshot

OWNER = "123456789012"


def _tags(name: str | None, extra: dict[str, str] | None = None) -> list[dict[str, str]]:
    tags = dict(extra or {})
    if name is not None:
        tags["Name"] = name
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def vpc(
    vpc_id: str,
    cidr: str = "10.0.0.0/16",
    *,
    name: str | None = None,
    is_default: bool = False,
    extra_cidrs: tuple[str, ...] = (),
    ipv6: tuple[str, ...] = (),
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "VpcId": vpc_id,
        "CidrBlock": cidr,
        "CidrBlockAssociationSet": [
            {"CidrBlock": block, "CidrBlockState": {"State": "associated"}}
            for block in (cidr, *extra_cidrs)
        ],
        "Ipv6CidrBlockAssociationSet": [
            {"Ipv6CidrBlock": block, "Ipv6CidrBlockState": {"State": "associated"}}
            for block in ipv6
        ],
        "IsDefault": is_default,
        "OwnerId": OWNER,
        "State": "available",
        "Tags": _tags(name, tags),
    }


def subnet(
    subnet_id: str,
    vpc_id: str,
    cidr: str | None = "10.0.1.0/24",
    *,
    zone: str = "ap-south-1a",
    name: str | None = None,
    map_public_ip: bool = False,
    ipv6: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "SubnetId": subnet_id,
        "VpcId": vpc_id,
        "AvailabilityZone": zone,
        "MapPublicIpOnLaunch": map_public_ip,
        "Ipv6CidrBlockAssociationSet": (
            [{"Ipv6CidrBlock": ipv6, "Ipv6CidrBlockState": {"State": "associated"}}] if ipv6 else []
        ),
        "State": "available",
        "Tags": _tags(name),
    }
    if cidr is not None:
        data["CidrBlock"] = cidr
    return data


def route(destination: str, **target: str) -> dict[str, Any]:
    key = (
        "DestinationPrefixListId"
        if destination.startswith("pl-")
        else ("DestinationIpv6CidrBlock" if ":" in destination else "DestinationCidrBlock")
    )
    return {key: destination, "State": "active", "Origin": "CreateRoute", **target}


def route_table(
    table_id: str,
    vpc_id: str,
    routes: list[dict[str, Any]] = (),
    *,
    subnets: tuple[str, ...] = (),
    main: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    associations = [
        {
            "Main": False,
            "SubnetId": sid,
            "RouteTableId": table_id,
            "AssociationState": {"State": "associated"},
        }
        for sid in subnets
    ]
    if main:
        associations.append(
            {"Main": True, "RouteTableId": table_id, "AssociationState": {"State": "associated"}}
        )
    local = {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"}
    return {
        "RouteTableId": table_id,
        "VpcId": vpc_id,
        "Associations": associations,
        "Routes": [local, *routes],
        "Tags": _tags(name),
    }


def nat_gateway(
    nat_id: str, vpc_id: str, subnet_id: str, *, state: str = "available", name: str | None = None
) -> dict[str, Any]:
    return {
        "NatGatewayId": nat_id,
        "VpcId": vpc_id,
        "SubnetId": subnet_id,
        "State": state,
        "Tags": _tags(name),
    }


def permission(
    protocol: str,
    from_port: int | None = None,
    to_port: int | None = None,
    *,
    cidrs: tuple[str, ...] = (),
    ipv6: tuple[str, ...] = (),
    groups: tuple[dict[str, str], ...] = (),
    prefix_lists: tuple[str, ...] = (),
    description: str | None = None,
) -> dict[str, Any]:
    def described(entry: dict[str, Any]) -> dict[str, Any]:
        return {**entry, "Description": description} if description else entry

    data: dict[str, Any] = {
        "IpProtocol": protocol,
        "IpRanges": [described({"CidrIp": c}) for c in cidrs],
        "Ipv6Ranges": [described({"CidrIpv6": c}) for c in ipv6],
        "UserIdGroupPairs": [described(dict(g)) for g in groups],
        "PrefixListIds": [described({"PrefixListId": p}) for p in prefix_lists],
    }
    if from_port is not None:
        data["FromPort"] = from_port
    if to_port is not None:
        data["ToPort"] = to_port
    return data


ALLOW_ALL_EGRESS = permission("-1", cidrs=("0.0.0.0/0",))


def security_group(
    group_id: str,
    vpc_id: str,
    group_name: str,
    *,
    ingress: list[dict[str, Any]] = (),
    egress: list[dict[str, Any]] | None = None,
    name: str | None = None,
    owner: str = OWNER,
) -> dict[str, Any]:
    return {
        "GroupId": group_id,
        "GroupName": group_name,
        "VpcId": vpc_id,
        "OwnerId": owner,
        "Description": group_name,
        "IpPermissions": list(ingress),
        "IpPermissionsEgress": [ALLOW_ALL_EGRESS] if egress is None else list(egress),
        "Tags": _tags(name),
    }


def acl_entry(
    number: int,
    *,
    protocol: str = "-1",
    action: str = "allow",
    egress: bool = False,
    cidr: str = "0.0.0.0/0",
    ports: tuple[int, int] | None = None,
    icmp: tuple[int, int] | None = None,
) -> dict[str, Any]:
    key = "Ipv6CidrBlock" if ":" in cidr else "CidrBlock"
    data: dict[str, Any] = {
        "RuleNumber": number,
        "Protocol": protocol,
        "RuleAction": action,
        "Egress": egress,
        key: cidr,
    }
    if ports is not None:
        data["PortRange"] = {"From": ports[0], "To": ports[1]}
    if icmp is not None:
        data["IcmpTypeCode"] = {"Type": icmp[0], "Code": icmp[1]}
    return data


DEFAULT_ACL_ENTRIES = [
    acl_entry(100, egress=False),
    acl_entry(32767, action="deny", egress=False),
    acl_entry(100, egress=True),
    acl_entry(32767, action="deny", egress=True),
]


def network_acl(
    acl_id: str,
    vpc_id: str,
    entries: list[dict[str, Any]],
    *,
    subnets: tuple[str, ...] = (),
    is_default: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "NetworkAclId": acl_id,
        "VpcId": vpc_id,
        "IsDefault": is_default,
        "Entries": entries,
        "Associations": [{"NetworkAclId": acl_id, "SubnetId": sid} for sid in subnets],
        "Tags": _tags(name),
    }


def snapshot(
    *,
    vpcs: list[dict[str, Any]] = (),
    subnets: list[dict[str, Any]] = (),
    route_tables: list[dict[str, Any]] | None = (),
    nat_gateways: list[dict[str, Any]] = (),
    security_groups: list[dict[str, Any]] = (),
    network_acls: list[dict[str, Any]] = (),
    vpc_attributes: dict[str, dict[str, bool]] | None = None,
    omit: tuple[str, ...] = (),
) -> Snapshot:
    """Build a Snapshot. `vpc_attributes` defaults to support on / hostnames on for every VPC.
    Pass `route_tables=None` or `omit=("network.vpc_attributes",)` to simulate scan gaps."""
    if vpc_attributes is None:
        vpc_attributes = {
            v["VpcId"]: {"enableDnsSupport": True, "enableDnsHostnames": True} for v in vpcs
        }
    files: dict[str, Any] = {
        "network.vpcs": list(vpcs),
        "network.subnets": list(subnets),
        "network.nat_gateways": list(nat_gateways),
        "network.security_groups": list(security_groups),
        "network.network_acls": list(network_acls),
        "network.internet_gateways": [],
        "network.vpc_attributes": vpc_attributes,
    }
    if route_tables is not None:
        files["network.route_tables"] = list(route_tables)
    for name in omit:
        files.pop(name, None)
    return Snapshot(files)
