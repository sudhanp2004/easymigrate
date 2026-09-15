"""Networks, subnets, NAT gateways and custom routes from an AWS snapshot (spec §3.2–§3.3).

Faithful capture only: nothing here decides whether GCP can reproduce what it records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import SourceRef
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.ir.network import CustomRoute, Nat, Network, Subnet
from easymigrate.plugins.network import aws_values as av

_LIVE_NAT_STATES = frozenset({"pending", "available"})
# Route keys naming a target, checked in order; the first present wins.
_TYPED_TARGETS = (
    ("VpcPeeringConnectionId", "peering"),
    ("TransitGatewayId", "tgw"),
    ("NetworkInterfaceId", "eni"),
    ("InstanceId", "eni"),
)
_OTHER_TARGETS = (
    "GatewayId",
    "NatGatewayId",
    "EgressOnlyInternetGatewayId",
    "VpcEndpointId",
    "CarrierGatewayId",
    "LocalGatewayId",
    "CoreNetworkArn",
)


@dataclass
class Topology:
    networks: list[Network]
    subnets: list[Subnet]
    nats: list[Nat]
    routes: list[CustomRoute]
    network_ids: dict[str, str]
    subnet_ids: dict[str, str]


def build_topology(snapshot: Snapshot, ctx: NormalizeContext) -> Topology:
    vpcs = snapshot.get("network.vpcs") or []
    route_tables_raw = snapshot.get("network.route_tables")
    route_tables = route_tables_raw or []

    network_ids = allocate_ids("network", [(av.name_tag(v), v["VpcId"]) for v in vpcs])
    networks = [
        _network(v, network_ids[v["VpcId"]], snapshot.get("network.vpc_attributes"), ctx)
        for v in vpcs
    ]

    subnets_raw = [
        s
        for s in snapshot.get("network.subnets") or []
        if s["VpcId"] in network_ids and _has_ipv4(s, ctx)
    ]
    subnet_ids = allocate_ids("subnet", [(av.name_tag(s), s["SubnetId"]) for s in subnets_raw])

    if route_tables_raw is None and subnets_raw:
        ctx.info(
            "NET-ROUTES-UNKNOWN",
            "Route tables could not be read, so every subnet is recorded as isolated.",
            suggestion="Grant ec2:DescribeRouteTables and scan again.",
        )
    tables = _RouteTables(route_tables)

    subnets = [
        _subnet(s, subnet_ids[s["SubnetId"]], network_ids[s["VpcId"]], tables.of(s), ctx)
        for s in subnets_raw
    ]
    nats = _nats(snapshot, network_ids, subnet_ids, subnets_raw, tables, ctx)
    routes = _custom_routes(route_tables, network_ids, subnet_ids, subnets_raw, tables, ctx)
    return Topology(networks, subnets, nats, routes, network_ids, subnet_ids)


class _RouteTables:
    def __init__(self, tables: list[dict[str, Any]]) -> None:
        self.by_subnet: dict[str, dict[str, Any]] = {}
        self.main: dict[str, dict[str, Any]] = {}
        for table in tables:
            for association in table.get("Associations") or []:
                state = (association.get("AssociationState") or {}).get("State", "associated")
                if state != "associated":
                    continue
                if association.get("Main"):
                    self.main[table["VpcId"]] = table
                elif association.get("SubnetId"):
                    self.by_subnet[association["SubnetId"]] = table

    def of(self, subnet: dict[str, Any]) -> dict[str, Any] | None:
        return self.by_subnet.get(subnet["SubnetId"]) or self.main.get(subnet["VpcId"])


def _source(ctx: NormalizeContext, type_: str, native_id: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native_id, region=ctx.region)


def _network(
    vpc: dict[str, Any],
    ir_id: str,
    attributes: dict[str, dict[str, bool]] | None,
    ctx: NormalizeContext,
) -> Network:
    vpc_id = vpc["VpcId"]
    cidrs = av.associated_cidrs(
        vpc.get("CidrBlockAssociationSet"), "CidrBlock", "CidrBlockState"
    ) or [av.cidr(vpc["CidrBlock"])]
    if attributes is not None and vpc_id in attributes:
        dns_support = attributes[vpc_id]["enableDnsSupport"]
        dns_hostnames = attributes[vpc_id]["enableDnsHostnames"]
    else:
        dns_support, dns_hostnames = True, False
        ctx.info(
            "NET-DNS-ATTRIBUTES-UNKNOWN",
            f"DNS settings for {vpc_id} could not be read; assuming AWS defaults "
            "(DNS resolution on, DNS hostnames off).",
            subject=ir_id,
            suggestion="Grant ec2:DescribeVpcAttribute and scan again.",
        )
    if vpc.get("IsDefault"):
        ctx.info(
            "NET-DEFAULT-VPC",
            f"{vpc_id} is this region's default VPC. Default VPCs are often unused; "
            "check whether it needs migrating.",
            subject=ir_id,
        )
    return Network(
        id=ir_id,
        name=av.name_tag(vpc) or vpc_id,
        labels=clean_labels(av.tags(vpc)),
        source=_source(ctx, "vpc", vpc_id),
        cidr_blocks=cidrs,
        ipv6_cidr_blocks=av.associated_cidrs(
            vpc.get("Ipv6CidrBlockAssociationSet"), "Ipv6CidrBlock", "Ipv6CidrBlockState"
        ),
        dns_hostnames=dns_hostnames,
        dns_resolution=dns_support,
    )


def _has_ipv4(subnet: dict[str, Any], ctx: NormalizeContext) -> bool:
    if subnet.get("CidrBlock"):
        return True
    ctx.unmodelled(
        f"subnet {subnet['SubnetId']} has no IPv4 CIDR; IPv6-only subnets are not modelled",
        subject=subnet["SubnetId"],
    )
    return False


def _default_route(table: dict[str, Any] | None) -> dict[str, Any] | None:
    for route in (table or {}).get("Routes") or []:
        if (
            route.get("DestinationCidrBlock") == "0.0.0.0/0"
            and route.get("State", "active") == "active"
        ):
            return route
    return None


def _internet(table: dict[str, Any] | None) -> str:
    route = _default_route(table)
    if route is None:
        return "isolated"
    if (route.get("GatewayId") or "").startswith("igw-"):
        return "public"
    if route.get("NatGatewayId"):
        return "private_nat"
    return "isolated"


def _subnet(
    subnet: dict[str, Any],
    ir_id: str,
    network_id: str,
    table: dict[str, Any] | None,
    ctx: NormalizeContext,
) -> Subnet:
    subnet_id = subnet["SubnetId"]
    ipv6 = av.associated_cidrs(
        subnet.get("Ipv6CidrBlockAssociationSet"), "Ipv6CidrBlock", "Ipv6CidrBlockState"
    )
    return Subnet(
        id=ir_id,
        name=av.name_tag(subnet) or subnet_id,
        labels=clean_labels(av.tags(subnet)),
        source=_source(ctx, "subnet", subnet_id),
        network=network_id,
        cidr=av.cidr(subnet["CidrBlock"]),
        ipv6_cidr=ipv6[0] if ipv6 else None,
        zone=subnet["AvailabilityZone"],
        internet=_internet(table),
        map_public_ip=bool(subnet.get("MapPublicIpOnLaunch")),
    )


def _nats(
    snapshot: Snapshot,
    network_ids: dict[str, str],
    subnet_ids: dict[str, str],
    subnets_raw: list[dict[str, Any]],
    tables: _RouteTables,
    ctx: NormalizeContext,
) -> list[Nat]:
    live = [
        n
        for n in snapshot.get("network.nat_gateways") or []
        if n.get("State") in _LIVE_NAT_STATES and n["VpcId"] in network_ids
    ]
    nat_ids = allocate_ids("nat", [(av.name_tag(n), n["NatGatewayId"]) for n in live])
    served: dict[str, list[str]] = {}
    for subnet in subnets_raw:
        route = _default_route(tables.of(subnet))
        if route and route.get("NatGatewayId"):
            served.setdefault(route["NatGatewayId"], []).append(subnet_ids[subnet["SubnetId"]])
    return [
        Nat(
            id=nat_ids[n["NatGatewayId"]],
            name=av.name_tag(n) or n["NatGatewayId"],
            labels=clean_labels(av.tags(n)),
            source=_source(ctx, "nat_gateway", n["NatGatewayId"]),
            network=network_ids[n["VpcId"]],
            serves_subnets=sorted(served.get(n["NatGatewayId"], [])),
        )
        for n in live
    ]


def _is_internet_default(route: dict[str, Any]) -> bool:
    gateway = route.get("GatewayId") or ""
    if route.get("DestinationCidrBlock") == "0.0.0.0/0":
        return gateway.startswith("igw-") or bool(route.get("NatGatewayId"))
    if route.get("DestinationIpv6CidrBlock") == "::/0":
        return gateway.startswith("igw-") or bool(route.get("EgressOnlyInternetGatewayId"))
    return False


def _destination(route: dict[str, Any]) -> str | None:
    if route.get("DestinationCidrBlock"):
        return av.cidr(route["DestinationCidrBlock"])
    if route.get("DestinationIpv6CidrBlock"):
        return av.cidr(route["DestinationIpv6CidrBlock"])
    return route.get("DestinationPrefixListId")


def _target(route: dict[str, Any]) -> tuple[str, str]:
    for key, target_type in _TYPED_TARGETS:
        if route.get(key):
            return target_type, route[key]
    gateway = route.get("GatewayId") or ""
    if gateway.startswith("vgw-"):
        return "vpn", gateway
    for key in _OTHER_TARGETS:
        if route.get(key):
            return "other", route[key]
    return "other", "unknown"


def _custom_routes(
    route_tables: list[dict[str, Any]],
    network_ids: dict[str, str],
    subnet_ids: dict[str, str],
    subnets_raw: list[dict[str, Any]],
    tables: _RouteTables,
    ctx: NormalizeContext,
) -> list[CustomRoute]:
    candidates: list[tuple[str, str, dict[str, Any], dict[str, Any], str]] = []
    for table in route_tables:
        if table["VpcId"] not in network_ids:
            continue
        table_name = av.name_tag(table) or table["RouteTableId"]
        for route in table.get("Routes") or []:
            if route.get("GatewayId") == "local" or _is_internet_default(route):
                continue
            destination = _destination(route)
            if destination is None:
                ctx.unmodelled(
                    f"route in {table['RouteTableId']} has no destination",
                    subject=table["RouteTableId"],
                )
                continue
            native_id = f"{table['RouteTableId']}:{destination}"
            candidates.append((native_id, f"{table_name} {destination}", table, route, destination))

    route_ids = allocate_ids("custom_route", [(name, native) for native, name, *_ in candidates])
    routes: list[CustomRoute] = []
    for native_id, name, table, route, destination in candidates:
        target_type, target_id = _target(route)
        using = sorted(subnet_ids[s["SubnetId"]] for s in subnets_raw if tables.of(s) is table)
        routes.append(
            CustomRoute(
                id=route_ids[native_id],
                name=name,
                source=_source(ctx, "route", native_id),
                network=network_ids[table["VpcId"]],
                destination=destination,
                target_type=target_type,
                target_id=target_id,
                subnets=using,
            )
        )
    return routes
