"""Generator unit tests, one per mapping row (spec §5.3, plan 4 A5)."""

from __future__ import annotations

from easymigrate.core.plugin import GenerateContext
from easymigrate.ir.base import Finding, IRDocument, ResolutionRef, SourceRef
from easymigrate.ir.network import (
    CidrPeer,
    FirewallPolicy,
    FirewallRule,
    Nat,
    Network,
    PolicyPeer,
    SelfPeer,
    Subnet,
)
from easymigrate.plugins.network.generate_gcp import generate


def _src(type_: str, id_: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def _network(id_: str, name: str, **kw) -> Network:
    return Network(
        id=f"network:{id_}",
        name=name,
        source=_src("vpc", f"vpc-{id_}"),
        cidr_blocks=kw.pop("cidr_blocks", ["10.0.0.0/16"]),
        dns_hostnames=kw.pop("dns_hostnames", True),
        dns_resolution=kw.pop("dns_resolution", True),
        **kw,
    )


def _subnet(id_: str, network: str, **kw) -> Subnet:
    return Subnet(
        id=f"subnet:{id_}",
        name=id_,
        source=_src("subnet", f"subnet-{id_}"),
        network=f"network:{network}",
        cidr=kw.pop("cidr", "10.0.1.0/24"),
        zone=kw.pop("zone", "ap-south-1a"),
        internet=kw.pop("internet", "public"),
        map_public_ip=kw.pop("map_public_ip", False),
        **kw,
    )


def _ctx(**kw) -> GenerateContext:
    return GenerateContext(project="p", region="asia-south1", version="1.2.3", **kw)


def test_network_plus_public_subnet_creates_route():
    net = _network("my_vpc", "my-vpc")
    subnet = _subnet("sub", "my_vpc", internet="public")
    doc = IRDocument(source={}, objects=[net, subnet])

    out = generate(doc, _ctx()).render()

    assert (
        'resource "google_compute_network" "my_vpc" {\n'
        '  name                            = "my-vpc"\n'
        "  auto_create_subnetworks         = false\n"
        "  delete_default_routes_on_create = true\n"
        '  routing_mode                    = "REGIONAL"\n'
        "}"
    ) in out
    assert (
        'resource "google_compute_route" "my_vpc_default_internet" {\n'
        '  name             = "my-vpc-default-internet"\n'
        "  network          = google_compute_network.my_vpc.id\n"
        '  dest_range       = "0.0.0.0/0"\n'
        '  next_hop_gateway = "default-internet-gateway"\n'
        "  priority         = 1000\n"
        "}"
    ) in out


def test_isolated_only_network_has_no_route():
    net = _network("my_vpc", "my-vpc")
    subnet = _subnet("sub", "my_vpc", internet="isolated")
    doc = IRDocument(source={}, objects=[net, subnet])

    out = generate(doc, _ctx()).render()

    assert "google_compute_route" not in out


def test_private_nat_generates_router_and_router_nat():
    net = _network("my_vpc", "my-vpc")
    subnet = _subnet("sub", "my_vpc", internet="private_nat")
    nat = Nat(
        id="nat:egress",
        name="egress",
        source=_src("nat_gateway", "nat-1"),
        network="network:my_vpc",
        serves_subnets=["subnet:sub"],
    )
    doc = IRDocument(source={}, objects=[net, subnet, nat])

    out = generate(doc, _ctx()).render()

    assert (
        'resource "google_compute_router" "egress" {\n'
        '  name    = "egress-router"\n'
        "  network = google_compute_network.my_vpc.id\n"
        "  region  = var.region\n"
        "}"
    ) in out
    assert (
        'resource "google_compute_router_nat" "egress_nat" {\n'
        '  name                               = "egress"\n'
        "  router                             = google_compute_router.egress.name\n"
        "  region                             = var.region\n"
        '  nat_ip_allocate_option             = "AUTO_ONLY"\n'
        '  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"\n'
        "\n"
        "  subnetwork {\n"
        "    name                    = google_compute_subnetwork.sub.id\n"
        '    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]\n'
        "  }\n"
        "}"
    ) in out


def test_nat_serving_no_subnets_is_skipped_with_info_finding():
    net = _network("my_vpc", "my-vpc")
    nat = Nat(
        id="nat:unused",
        name="unused",
        source=_src("nat_gateway", "nat-2"),
        network="network:my_vpc",
        serves_subnets=[],
    )
    doc = IRDocument(source={}, objects=[net, nat])
    ctx = _ctx()

    out = generate(doc, ctx).render()

    assert "google_compute_router" not in out
    assert [f.code for f in ctx.findings] == ["GEN-NAT-UNUSED"]
    assert ctx.findings[0].subject == "nat:unused"


def test_sg_grouping_by_direction_and_peer_kind():
    net = _network("my_vpc", "my-vpc")
    policy = FirewallPolicy(
        id="firewall_policy:web",
        name="web",
        source=_src("security_group", "sg-1"),
        network="network:my_vpc",
        rules=[
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["80"], peer=CidrPeer(cidr="0.0.0.0/0")
            ),
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["443"],
                peer=CidrPeer(cidr="0.0.0.0/0"),
            ),
        ],
    )
    doc = IRDocument(source={}, objects=[net, policy])

    out = generate(doc, _ctx()).render()

    assert (
        'resource "google_compute_firewall" "web_in_cidr" {\n'
        '  name          = "web-in-cidr"\n'
        "  network       = google_compute_network.my_vpc.id\n"
        '  direction     = "INGRESS"\n'
        "  priority      = 1000\n"
        '  target_tags   = ["fw-web"]\n'
        '  source_ranges = ["0.0.0.0/0"]\n'
        "\n"
        "  allow {\n"
        '    protocol = "tcp"\n'
        '    ports    = ["443"]\n'
        "  }\n"
        "  allow {\n"
        '    protocol = "tcp"\n'
        '    ports    = ["80"]\n'
        "  }\n"
        "}"
    ) in out


def test_self_and_policy_peers():
    net = _network("my_vpc", "my-vpc")
    web = FirewallPolicy(
        id="firewall_policy:web",
        name="web",
        source=_src("security_group", "sg-1"),
        network="network:my_vpc",
        rules=[FirewallRule(direction="ingress", protocol="tcp", ports=["22"], peer=SelfPeer())],
    )
    db = FirewallPolicy(
        id="firewall_policy:db",
        name="db",
        source=_src("security_group", "sg-2"),
        network="network:my_vpc",
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["5432"],
                peer=PolicyPeer(policy="firewall_policy:web"),
            )
        ],
    )
    doc = IRDocument(source={}, objects=[net, web, db])

    out = generate(doc, _ctx()).render()

    assert 'source_tags = ["fw-web"]' in out  # self peer -> own tag
    assert 'resource "google_compute_firewall" "db_in_tag"' in out
    assert 'target_tags = ["fw-db"]' in out
    assert 'source_tags = ["fw-web"]' in out  # policy peer -> peer's tag


def test_egress_ranges_hint_used_for_policy_peer():
    net = _network("my_vpc", "my-vpc")
    other_net = _network("other", "other-vpc", cidr_blocks=["10.20.0.0/16"])
    peer = FirewallPolicy(
        id="firewall_policy:peer",
        name="peer",
        source=_src("security_group", "sg-2"),
        network="network:other",
    )
    policy = FirewallPolicy(
        id="firewall_policy:web",
        name="web",
        source=_src("security_group", "sg-1"),
        network="network:my_vpc",
        rules=[
            FirewallRule(
                direction="egress",
                protocol="tcp",
                ports=["5432"],
                peer=PolicyPeer(policy="firewall_policy:peer"),
            )
        ],
        target_hints={"egress_ranges": {"firewall_policy:peer": ["10.20.0.0/16"]}},
    )
    doc = IRDocument(source={}, objects=[net, other_net, peer, policy])

    out = generate(doc, _ctx()).render()

    assert 'destination_ranges = ["10.20.0.0/16"]' in out


def test_default_allow_all_egress_is_skipped_exactly_once():
    net = _network("my_vpc", "my-vpc")
    policy = FirewallPolicy(
        id="firewall_policy:web",
        name="web",
        source=_src("security_group", "sg-1"),
        network="network:my_vpc",
        rules=[
            FirewallRule(
                direction="egress", protocol="all", ports=[], peer=CidrPeer(cidr="0.0.0.0/0")
            ),
        ],
    )
    doc = IRDocument(source={}, objects=[net, policy])

    out = generate(doc, _ctx()).render()

    assert "google_compute_firewall" not in out


def test_default_allow_all_ipv6_egress_is_skipped_too():
    # AWS's IPv6 all-egress default rule (::/0) must be skipped just like the IPv4
    # default (0.0.0.0/0): GCP already allows all egress by default, so leaving this
    # in produces dead, contradictory Terraform once ipv4_only has stripped IPv6
    # addressing elsewhere.
    net = _network("my_vpc", "my-vpc")
    policy = FirewallPolicy(
        id="firewall_policy:web",
        name="web",
        source=_src("security_group", "sg-1"),
        network="network:my_vpc",
        rules=[
            FirewallRule(direction="egress", protocol="all", ports=[], peer=CidrPeer(cidr="::/0")),
        ],
    )
    doc = IRDocument(source={}, objects=[net, policy])

    out = generate(doc, _ctx()).render()

    assert "google_compute_firewall" not in out


def test_colliding_policy_names_get_distinct_tags_via_allocator():
    # Two security groups whose slugs collide after gcp_name() cleaning must not get the
    # same network tag: that would let one group's firewall rules match the other
    # group's instances once a later plugin attaches tags. The allocator must assign a
    # numeric suffix on collision, and the second (renamed) one must report GEN-RENAMED.
    net = _network("my_vpc", "my-vpc")
    web1 = FirewallPolicy(
        id="firewall_policy:Web!!",
        name="Web!!",
        source=_src("security_group", "sg-1"),
        network="network:my_vpc",
        rules=[
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["80"], peer=CidrPeer(cidr="0.0.0.0/0")
            )
        ],
    )
    web2 = FirewallPolicy(
        id="firewall_policy:Web##",
        name="Web##",
        source=_src("security_group", "sg-2"),
        network="network:my_vpc",
        rules=[
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["443"], peer=CidrPeer(cidr="0.0.0.0/0")
            )
        ],
    )
    doc = IRDocument(source={}, objects=[net, web1, web2])
    ctx = _ctx()

    out = generate(doc, ctx).render()

    assert 'target_tags   = ["fw-web"]' in out
    assert 'target_tags   = ["fw-web-2"]' in out
    assert out.count('source_ranges = ["0.0.0.0/0"]') == 2
    assert any(
        f.code == "GEN-RENAMED" and f.subject == "firewall_policy:Web##" for f in ctx.findings
    )


def test_excluded_objects_are_skipped():
    net = _network("my_vpc", "my-vpc", excluded="test")
    doc = IRDocument(source={}, objects=[net])

    out = generate(doc, _ctx()).render()

    assert "google_compute_network" not in out


def test_rename_finding_when_gcp_name_differs_from_ir_name():
    net = _network("My_VPC!!", "My_VPC!!")
    doc = IRDocument(source={}, objects=[net])
    ctx = _ctx()

    generate(doc, ctx)

    assert any(f.code == "GEN-RENAMED" and f.subject == "network:My_VPC!!" for f in ctx.findings)


def test_no_rename_finding_when_slug_differs_from_name_but_name_matches_gcp_name():
    # The GCP name is derived from the IR id's slug (my_vpc -> my-vpc), which differs
    # from the id itself but not from the object's `name` field. GEN-RENAMED must
    # compare against `name`, not the id-derived slug, so this must NOT fire.
    net = _network("my_vpc", "my-vpc")
    doc = IRDocument(source={}, objects=[net])
    ctx = _ctx()

    generate(doc, ctx)

    assert ctx.findings == []


def test_rename_finding_when_name_has_spaces():
    # A genuine rename: the display name itself (not just the id-derived slug) doesn't
    # satisfy GCP naming rules, so the generated name must differ from `name`.
    net = _network("my_vpc", "My VPC Name")
    doc = IRDocument(source={}, objects=[net])
    ctx = _ctx()

    generate(doc, ctx)

    assert any(f.code == "GEN-RENAMED" and f.subject == "network:my_vpc" for f in ctx.findings)


def test_rename_finding_when_name_over_63_chars():
    long_name = "a" * 70
    net = _network("my_vpc", long_name)
    doc = IRDocument(source={}, objects=[net])
    ctx = _ctx()

    generate(doc, ctx)

    assert any(f.code == "GEN-RENAMED" and f.subject == "network:my_vpc" for f in ctx.findings)


def test_header_lines_include_source_decisions_and_findings():
    net = _network(
        "vpc",
        "my-vpc",
        resolution=[ResolutionRef(key="NET-ZONE-REGIONAL@network:vpc", strategy="keep_1to1")],
    )
    doc = IRDocument(
        source={},
        objects=[net],
        findings=[
            Finding(
                level="scan_gap",
                code="SCAN-ACCESS-DENIED",
                message="could not read tags",
                subject="network:vpc",
            )
        ],
    )
    ctx = _ctx(decided_by={"NET-ZONE-REGIONAL@network:vpc": "user"})

    out = generate(doc, ctx).render()

    assert "# from AWS vpc vpc-vpc (my-vpc)" in out
    assert "# decision NET-ZONE-REGIONAL@network:vpc → keep_1to1 (user)" in out
    assert "# ⚠ SCAN-ACCESS-DENIED: could not read tags" in out
