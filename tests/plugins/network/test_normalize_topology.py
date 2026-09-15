import pytest

from easymigrate.core.plugin import NormalizeContext, UnmodelledData
from easymigrate.ir import IRDocument
from easymigrate.plugins.network.normalize_topology import build_topology
from tests.plugins.network import aws_builders as aws


def ctx(strict: bool = True) -> NormalizeContext:
    return NormalizeContext(account_id="123456789012", region="ap-south-1", strict=strict)


def as_document(topology) -> IRDocument:
    objects = [*topology.networks, *topology.subnets, *topology.nats, *topology.routes]
    return IRDocument(source={"cloud": "aws"}, objects=objects)


def test_public_subnet_via_explicit_route_table():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main", tags={"Cost Center": "R&D"})],
        subnets=[aws.subnet("subnet-1", "vpc-1", name="web", map_public_ip=True)],
        route_tables=[
            aws.route_table(
                "rtb-1", "vpc-1", [aws.route("0.0.0.0/0", GatewayId="igw-1")], subnets=("subnet-1",)
            ),
            aws.route_table("rtb-main", "vpc-1", main=True),
        ],
    )
    c = ctx()

    topo = build_topology(snap, c)

    (network,) = topo.networks
    assert network.id == "network:main"
    assert network.cidr_blocks == ["10.0.0.0/16"]
    assert (network.dns_resolution, network.dns_hostnames) == (True, True)
    assert network.labels == {"cost_center": "r_d", "name": "main"}
    assert network.source.id == "vpc-1" and network.source.type == "vpc"
    (sub,) = topo.subnets
    assert (sub.id, sub.network, sub.cidr, sub.zone) == (
        "subnet:web",
        "network:main",
        "10.0.1.0/24",
        "ap-south-1a",
    )
    assert sub.internet == "public" and sub.map_public_ip is True
    assert topo.routes == [] and topo.nats == [] and c.findings == []
    assert topo.network_ids == {"vpc-1": "network:main"}
    assert topo.subnet_ids == {"subnet-1": "subnet:web"}
    as_document(topo)


def test_main_route_table_fallback_and_nat():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main")],
        subnets=[
            aws.subnet("subnet-pub", "vpc-1", "10.0.1.0/24", name="public"),
            aws.subnet("subnet-app", "vpc-1", "10.0.2.0/24", name="app"),
        ],
        route_tables=[
            aws.route_table(
                "rtb-pub",
                "vpc-1",
                [aws.route("0.0.0.0/0", GatewayId="igw-1")],
                subnets=("subnet-pub",),
            ),
            aws.route_table(
                "rtb-main", "vpc-1", [aws.route("0.0.0.0/0", NatGatewayId="nat-1")], main=True
            ),
        ],
        nat_gateways=[
            aws.nat_gateway("nat-1", "vpc-1", "subnet-pub", name="egress"),
            aws.nat_gateway("nat-old", "vpc-1", "subnet-pub", state="deleted"),
        ],
    )

    topo = build_topology(snap, ctx())

    internet = {s.id: s.internet for s in topo.subnets}
    assert internet == {"subnet:public": "public", "subnet:app": "private_nat"}
    (nat,) = topo.nats
    assert (nat.id, nat.network, nat.serves_subnets) == (
        "nat:egress",
        "network:main",
        ["subnet:app"],
    )
    assert topo.routes == []
    as_document(topo)


def test_isolated_when_no_default_route():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[aws.subnet("subnet-1", "vpc-1")],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    (sub,) = build_topology(snap, ctx()).subnets

    assert sub.internet == "isolated"
    assert sub.name == "subnet-1" and sub.id == "subnet:subnet-1"


def test_custom_routes_by_target_type():
    routes = [
        aws.route("172.16.0.0/16", VpcPeeringConnectionId="pcx-1"),
        aws.route("192.168.0.0/16", GatewayId="vgw-1"),
        aws.route("10.50.0.0/16", TransitGatewayId="tgw-1"),
        aws.route("10.60.0.0/16", NetworkInterfaceId="eni-1", InstanceId="i-1"),
        aws.route("pl-1234", VpcEndpointId="vpce-1"),
        aws.route("10.70.0.5/16", NatGatewayId="nat-1"),
        aws.route("::/0", GatewayId="igw-1"),
        aws.route("::/0", EgressOnlyInternetGatewayId="eigw-1"),
    ]
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main")],
        subnets=[aws.subnet("subnet-1", "vpc-1", name="app")],
        route_tables=[
            aws.route_table("rtb-1", "vpc-1", routes, subnets=("subnet-1",), name="app-routes"),
            aws.route_table("rtb-main", "vpc-1", main=True),
        ],
    )

    topo = build_topology(snap, ctx())

    found = {(r.destination, r.target_type, r.target_id) for r in topo.routes}
    assert found == {
        ("172.16.0.0/16", "peering", "pcx-1"),
        ("192.168.0.0/16", "vpn", "vgw-1"),
        ("10.50.0.0/16", "tgw", "tgw-1"),
        ("10.60.0.0/16", "eni", "eni-1"),
        ("pl-1234", "other", "vpce-1"),
        ("10.70.0.0/16", "other", "nat-1"),
    }
    peering = next(r for r in topo.routes if r.target_type == "peering")
    assert peering.id == "custom_route:app-routes-172-16-0-0-16"
    assert peering.source.id == "rtb-1:172.16.0.0/16"
    assert peering.subnets == ["subnet:app"]
    assert all(r.network == "network:main" for r in topo.routes)
    as_document(topo)


def test_main_table_custom_route_lists_implicit_subnets():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[
            aws.subnet("subnet-a", "vpc-1", "10.0.1.0/24", name="a"),
            aws.subnet("subnet-b", "vpc-1", "10.0.2.0/24", name="b"),
        ],
        route_tables=[
            aws.route_table("rtb-b", "vpc-1", subnets=("subnet-b",)),
            aws.route_table(
                "rtb-main",
                "vpc-1",
                [aws.route("172.16.0.0/16", VpcPeeringConnectionId="pcx-1")],
                main=True,
            ),
        ],
    )

    (custom,) = build_topology(snap, ctx()).routes

    assert custom.subnets == ["subnet:a"]


def test_ipv6_cidrs_are_captured_and_canonical():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", ipv6=("2600:1F18:ABCD:1200::/56",))],
        subnets=[aws.subnet("subnet-1", "vpc-1", ipv6="2600:1f18:abcd:1201::/64")],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    topo = build_topology(snap, ctx())

    assert topo.networks[0].ipv6_cidr_blocks == ["2600:1f18:abcd:1200::/56"]
    assert topo.subnets[0].ipv6_cidr == "2600:1f18:abcd:1201::/64"


def test_same_named_subnets_get_native_id_suffixes():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[
            aws.subnet("subnet-2", "vpc-1", "10.0.2.0/24", name="public"),
            aws.subnet("subnet-1", "vpc-1", "10.0.1.0/24", name="public"),
        ],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    ids = sorted(s.id for s in build_topology(snap, ctx()).subnets)

    assert ids == ["subnet:public-subnet-1", "subnet:public-subnet-2"]


def test_ipv6_only_subnet_is_unmodelled():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[aws.subnet("subnet-v6", "vpc-1", cidr=None, ipv6="2600:1f18::/64")],
        route_tables=[aws.route_table("rtb-main", "vpc-1", main=True)],
    )

    with pytest.raises(UnmodelledData, match="subnet-v6"):
        build_topology(snap, ctx(strict=True))

    lenient = ctx(strict=False)
    topo = build_topology(snap, lenient)
    assert topo.subnets == []
    assert [f.code for f in lenient.findings] == ["INTERNAL-UNMODELLED"]


def test_missing_vpc_attributes_use_aws_defaults():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1", name="main", is_default=True)],
        route_tables=[],
        omit=("network.vpc_attributes",),
    )
    c = ctx()

    (network,) = build_topology(snap, c).networks

    assert (network.dns_resolution, network.dns_hostnames) == (True, False)
    assert sorted(f.code for f in c.findings) == ["NET-DEFAULT-VPC", "NET-DNS-ATTRIBUTES-UNKNOWN"]
    assert all(f.subject == "network:main" for f in c.findings)


def test_missing_route_tables_are_reported_once():
    snap = aws.snapshot(
        vpcs=[aws.vpc("vpc-1")],
        subnets=[
            aws.subnet("subnet-1", "vpc-1", "10.0.1.0/24"),
            aws.subnet("subnet-2", "vpc-1", "10.0.2.0/24"),
        ],
        route_tables=None,
    )
    c = ctx()

    topo = build_topology(snap, c)

    assert {s.internet for s in topo.subnets} == {"isolated"}
    assert [f.code for f in c.findings] == ["NET-ROUTES-UNKNOWN"]


def test_subnets_of_unknown_vpcs_are_skipped():
    snap = aws.snapshot(
        vpcs=[],
        subnets=[aws.subnet("subnet-1", "vpc-missing")],
        route_tables=[],
    )

    topo = build_topology(snap, ctx())

    assert topo.subnets == [] and topo.networks == []


def test_secondary_vpc_cidrs():
    snap = aws.snapshot(vpcs=[aws.vpc("vpc-1", extra_cidrs=("100.64.0.0/16",))], route_tables=[])

    assert build_topology(snap, ctx()).networks[0].cidr_blocks == ["10.0.0.0/16", "100.64.0.0/16"]
