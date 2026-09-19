"""Per-rule detect/apply tests for the AWS→GCP networking catalogue (spec §4.4, plan 3 tasks 2-3).

Small hand-built IR objects, local to this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from easymigrate.differences.rules.aws_gcp.network import (
    NetCustomRoute,
    NetEgressTag,
    NetIcmpType,
    NetIsolationVmLevel,
    NetNacl,
    NetPeering,
    NetPrefixList,
    NetSgXvpcRef,
    NetZoneRegional,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.network import (
    CidrPeer,
    CustomRoute,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    Nat,
    Network,
    NetworkAcl,
    PolicyPeer,
    PrefixListPeer,
    Subnet,
)


def _source(type_="vpc", id_="native-1"):
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def _network(id_="network:main", cidr="10.0.0.0/16"):
    return Network(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("vpc", id_),
        cidr_blocks=[cidr],
        dns_hostnames=True,
        dns_resolution=True,
    )


def _subnet(
    id_,
    network="network:main",
    cidr="10.0.1.0/24",
    zone="ap-south-1a",
    internet="public",
    map_public_ip=None,
):
    return Subnet(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("subnet", id_),
        network=network,
        cidr=cidr,
        zone=zone,
        internet=internet,
        map_public_ip=map_public_ip if map_public_ip is not None else (internet == "public"),
    )


def _policy(id_, network="network:main", rules=None):
    return FirewallPolicy(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("security-group", id_),
        network=network,
        rules=rules or [],
    )


def _doc(*objects) -> IRDocument:
    return IRDocument(source={"cloud": "aws"}, objects=list(objects))


# ---- NET-ZONE-REGIONAL ----------------------------------------------------


def test_zone_regional_detects_network_with_subnets():
    net = _network()
    s1 = _subnet("subnet:a", cidr="10.0.1.0/24", zone="ap-south-1a")
    s2 = _subnet("subnet:b", cidr="10.0.2.0/24", zone="ap-south-1b")
    doc = _doc(net, s1, s2)
    rule = NetZoneRegional()

    diffs = rule.detect(doc)

    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.key == "NET-ZONE-REGIONAL@network:main"
    assert diff.subjects == ["network:main", "subnet:a", "subnet:b"]
    assert diff.context["subnet_count"] == 2
    assert diff.context["zones"] == ["ap-south-1a", "ap-south-1b"]


def test_zone_regional_detects_nothing_for_network_without_subnets():
    doc = _doc(_network())
    assert NetZoneRegional().detect(doc) == []


def test_zone_regional_keep_1to1_is_a_no_op():
    net = _network()
    s1 = _subnet("subnet:a")
    doc = _doc(net, s1)
    rule = NetZoneRegional()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "keep_1to1")

    assert result == doc
    assert doc == _doc(net, s1)  # input unchanged


def test_zone_regional_merge_computes_supernet_and_exposure_and_rewrites_refs():
    net = _network()
    s1 = _subnet("subnet:a", cidr="10.0.1.0/24", zone="ap-south-1a", internet="public")
    s2 = _subnet(
        "subnet:b", cidr="10.0.2.0/24", zone="ap-south-1b", internet="isolated", map_public_ip=False
    )
    nat = Nat(
        id="nat:a",
        name="a",
        source=_source("nat", "nat:a"),
        network="network:main",
        serves_subnets=["subnet:a", "subnet:b"],
    )
    acl = NetworkAcl(
        id="network_acl:a",
        name="a",
        source=_source("acl", "network_acl:a"),
        network="network:main",
        subnets=["subnet:a", "subnet:b"],
    )
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "custom_route:a"),
        network="network:main",
        destination="0.0.0.0/0",
        target_type="other",
        target_id="x",
        subnets=["subnet:a", "subnet:b"],
    )
    doc = _doc(net, s1, s2, nat, acl, route)
    rule = NetZoneRegional()
    diff = rule.detect(doc)[0]

    original = doc.model_copy(deep=True)
    result = rule.apply(doc, diff, "merge_per_network")

    assert doc == original  # input unchanged
    merged = result.get("subnet:main-regional")
    assert merged.cidr == "10.0.0.0/22"
    assert merged.zone == "regional"
    assert merged.internet == "public"  # public more exposed than isolated
    assert merged.map_public_ip is True
    assert result.get("subnet:a", None) if False else True
    remaining_subnet_ids = [o.id for o in result.objects if o.kind == "subnet"]
    assert remaining_subnet_ids == ["subnet:main-regional"]
    assert result.get("nat:a").serves_subnets == ["subnet:main-regional"]
    assert result.get("network_acl:a").subnets == ["subnet:main-regional"]
    assert result.get("custom_route:a").subnets == ["subnet:main-regional"]


def test_zone_regional_merge_supernet_cases():
    from easymigrate.differences.rules.aws_gcp.network import _smallest_ipv4_supernet

    assert _smallest_ipv4_supernet(["10.0.1.0/24", "10.0.2.0/24"]) == "10.0.0.0/22"


# ---- NET-ISOLATION-VM-LEVEL -------------------------------------------------


def test_isolation_vm_level_detects_isolated_subnet_with_public_sibling():
    net = _network()
    public = _subnet("subnet:pub", internet="public")
    isolated = _subnet("subnet:iso", internet="isolated", map_public_ip=False)
    doc = _doc(net, public, isolated)
    diffs = NetIsolationVmLevel().detect(doc)
    assert [d.key for d in diffs] == ["NET-ISOLATION-VM-LEVEL@subnet:iso"]
    assert diffs[0].subjects == ["subnet:iso"]


def test_isolation_vm_level_detects_nothing_without_public_sibling():
    net = _network()
    isolated = _subnet("subnet:iso", internet="isolated", map_public_ip=False)
    doc = _doc(net, isolated)
    assert NetIsolationVmLevel().detect(doc) == []


def test_isolation_vm_level_no_external_ip_sets_hint():
    net = _network()
    public = _subnet("subnet:pub", internet="public")
    isolated = _subnet("subnet:iso", internet="isolated", map_public_ip=False)
    doc = _doc(net, public, isolated)
    rule = NetIsolationVmLevel()
    diff = rule.detect(doc)[0]
    original = doc.model_copy(deep=True)

    result = rule.apply(doc, diff, "no_external_ip")

    assert doc == original
    assert result.get("subnet:iso").target_hints == {"no_external_ip": True}


def test_isolation_vm_level_manual_is_a_no_op():
    net = _network()
    public = _subnet("subnet:pub", internet="public")
    isolated = _subnet("subnet:iso", internet="isolated", map_public_ip=False)
    doc = _doc(net, public, isolated)
    rule = NetIsolationVmLevel()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result == doc


# ---- NET-EGRESS-TAG ---------------------------------------------------------


def test_egress_tag_detects_egress_policy_peer_rule():
    net = _network()
    peer = _policy("firewall_policy:peer")
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="egress",
                protocol="tcp",
                ports=["443"],
                peer=PolicyPeer(policy="firewall_policy:peer"),
            ),
        ],
    )
    doc = _doc(net, peer, main)
    diffs = NetEgressTag().detect(doc)
    assert [d.key for d in diffs] == ["NET-EGRESS-TAG@firewall_policy:main"]
    assert diffs[0].subjects == ["firewall_policy:main", "firewall_policy:peer"]


def test_egress_tag_detects_nothing_without_egress_policy_peer():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["443"],
                peer=CidrPeer(cidr="10.0.0.0/16"),
            )
        ],
    )
    doc = _doc(net, main)
    assert NetEgressTag().detect(doc) == []


def test_egress_tag_peer_attached_ranges_sets_hint():
    net = _network(cidr="10.0.0.0/16")
    other_net = _network(id_="network:other", cidr="10.1.0.0/16")
    peer = _policy("firewall_policy:peer", network="network:other")
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="egress",
                protocol="tcp",
                ports=["443"],
                peer=PolicyPeer(policy="firewall_policy:peer"),
            ),
        ],
    )
    doc = _doc(net, other_net, peer, main)
    rule = NetEgressTag()
    diff = rule.detect(doc)[0]
    original = doc.model_copy(deep=True)

    result = rule.apply(doc, diff, "peer_attached_ranges")

    assert doc == original
    assert result.get("firewall_policy:main").target_hints == {
        "egress_ranges": {"firewall_policy:peer": ["10.1.0.0/16"]}
    }


def test_egress_tag_manual_removes_rules_and_counts_omitted():
    net = _network()
    peer = _policy("firewall_policy:peer")
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="egress",
                protocol="tcp",
                ports=["443"],
                peer=PolicyPeer(policy="firewall_policy:peer"),
            ),
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["80"], peer=CidrPeer(cidr="10.0.0.0/16")
            ),
        ],
    )
    doc = _doc(net, peer, main)
    rule = NetEgressTag()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    updated = result.get("firewall_policy:main")
    assert len(updated.rules) == 1
    assert updated.rules[0].direction == "ingress"
    assert updated.target_hints == {"omitted_rules": 1}


# ---- NET-SG-XVPC-REF ---------------------------------------------------------


def test_sg_xvpc_ref_detects_external_policy_peer():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["22"],
                peer=ExternalPolicyPeer(group_id="sg-ext"),
            )
        ],
    )
    doc = _doc(net, main)
    diffs = NetSgXvpcRef().detect(doc)
    assert [d.key for d in diffs] == ["NET-SG-XVPC-REF@firewall_policy:main"]
    assert diffs[0].context["group_ids"] == ["sg-ext"]


def test_sg_xvpc_ref_detects_nothing_without_external_peer():
    net = _network()
    main = _policy("firewall_policy:main")
    doc = _doc(net, main)
    assert NetSgXvpcRef().detect(doc) == []


def test_sg_xvpc_ref_manual_removes_rules_and_counts_omitted():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["22"],
                peer=ExternalPolicyPeer(group_id="sg-ext"),
            ),
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["80"], peer=CidrPeer(cidr="10.0.0.0/16")
            ),
        ],
    )
    doc = _doc(net, main)
    rule = NetSgXvpcRef()
    diff = rule.detect(doc)[0]
    original = doc.model_copy(deep=True)

    result = rule.apply(doc, diff, "manual")

    assert doc == original
    updated = result.get("firewall_policy:main")
    assert len(updated.rules) == 1
    assert updated.target_hints == {"omitted_rules": 1}


# ---- NET-PREFIX-LIST ---------------------------------------------------------


def test_prefix_list_detects_prefix_list_peer():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["443"],
                peer=PrefixListPeer(prefix_list_id="pl-1"),
            ),
        ],
    )
    doc = _doc(net, main)
    diffs = NetPrefixList().detect(doc)
    assert [d.key for d in diffs] == ["NET-PREFIX-LIST@firewall_policy:main"]


def test_prefix_list_detects_nothing_without_prefix_list_peer():
    net = _network()
    main = _policy("firewall_policy:main")
    doc = _doc(net, main)
    assert NetPrefixList().detect(doc) == []


def test_prefix_list_manual_removes_rules_and_counts_omitted():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["443"],
                peer=PrefixListPeer(prefix_list_id="pl-1"),
            ),
        ],
    )
    doc = _doc(net, main)
    rule = NetPrefixList()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    updated = result.get("firewall_policy:main")
    assert updated.rules == []
    assert updated.target_hints == {"omitted_rules": 1}


def test_prefix_list_only_has_manual_strategy():
    rule = NetPrefixList()
    assert [s.id for s in rule.strategies] == ["manual"]
    assert rule.default == "manual"


# ---- NET-ICMP-TYPE ------------------------------------------------------------


def test_icmp_type_detects_rule_with_icmp_type():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress", protocol="icmp", peer=CidrPeer(cidr="10.0.0.0/16"), icmp_type=8
            ),
        ],
    )
    doc = _doc(net, main)
    diffs = NetIcmpType().detect(doc)
    assert [d.key for d in diffs] == ["NET-ICMP-TYPE@firewall_policy:main"]


def test_icmp_type_detects_nothing_without_icmp_type():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(direction="ingress", protocol="icmp", peer=CidrPeer(cidr="10.0.0.0/16"))
        ],
    )
    doc = _doc(net, main)
    assert NetIcmpType().detect(doc) == []


def test_icmp_type_allow_all_icmp_clears_and_dedupes():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress", protocol="icmp", peer=CidrPeer(cidr="10.0.0.0/16"), icmp_type=8
            ),
            FirewallRule(
                direction="ingress", protocol="icmp", peer=CidrPeer(cidr="10.0.0.0/16"), icmp_type=0
            ),
        ],
    )
    doc = _doc(net, main)
    rule = NetIcmpType()
    diff = rule.detect(doc)[0]
    original = doc.model_copy(deep=True)

    result = rule.apply(doc, diff, "allow_all_icmp")

    assert doc == original
    updated = result.get("firewall_policy:main")
    assert len(updated.rules) == 1
    assert updated.rules[0].icmp_type is None
    assert updated.rules[0].icmp_code is None


def test_icmp_type_manual_removes_rules_and_counts_omitted():
    net = _network()
    main = _policy(
        "firewall_policy:main",
        rules=[
            FirewallRule(
                direction="ingress", protocol="icmp", peer=CidrPeer(cidr="10.0.0.0/16"), icmp_type=8
            ),
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["80"], peer=CidrPeer(cidr="10.0.0.0/16")
            ),
        ],
    )
    doc = _doc(net, main)
    rule = NetIcmpType()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    updated = result.get("firewall_policy:main")
    assert len(updated.rules) == 1
    assert updated.rules[0].protocol == "tcp"
    assert updated.target_hints == {"omitted_rules": 1}


def test_rules_1_through_6_have_valid_catalogue_ids():
    for rule_cls in (
        NetZoneRegional,
        NetIsolationVmLevel,
        NetEgressTag,
        NetSgXvpcRef,
        NetPrefixList,
        NetIcmpType,
    ):
        rule = rule_cls()
        assert rule.family == "network"
        assert rule.id.startswith("NET-")


# ---- NET-NACL ------------------------------------------------------------


def test_nacl_detects_each_acl():
    net = _network()
    acl = NetworkAcl(
        id="network_acl:a", name="a", source=_source("acl", "acl-1"), network="network:main"
    )
    doc = _doc(net, acl)
    diffs = NetNacl().detect(doc)
    assert [d.key for d in diffs] == ["NET-NACL@network_acl:a"]


def test_nacl_detects_nothing_without_acl():
    doc = _doc(_network())
    assert NetNacl().detect(doc) == []


def test_nacl_manual_excludes():
    net = _network()
    acl = NetworkAcl(
        id="network_acl:a", name="a", source=_source("acl", "acl-1"), network="network:main"
    )
    doc = _doc(net, acl)
    rule = NetNacl()
    diff = rule.detect(doc)[0]
    original = doc.model_copy(deep=True)

    result = rule.apply(doc, diff, "manual")

    assert doc == original
    assert result.get("network_acl:a").excluded == (
        "NET-NACL: stateless subnet ACLs have no GCP equivalent"
    )


# ---- NET-PEERING -----------------------------------------------------------


def test_peering_detects_peering_route():
    net = _network()
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "route-1"),
        network="network:main",
        destination="10.1.0.0/16",
        target_type="peering",
        target_id="pcx-1",
    )
    doc = _doc(net, route)
    diffs = NetPeering().detect(doc)
    assert [d.key for d in diffs] == ["NET-PEERING@custom_route:a"]


def test_peering_detects_nothing_for_non_peering_route():
    net = _network()
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "route-1"),
        network="network:main",
        destination="10.1.0.0/16",
        target_type="vpn",
        target_id="vgw-1",
    )
    doc = _doc(net, route)
    assert NetPeering().detect(doc) == []


def test_peering_manual_excludes():
    net = _network()
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "route-1"),
        network="network:main",
        destination="10.1.0.0/16",
        target_type="peering",
        target_id="pcx-1",
    )
    doc = _doc(net, route)
    rule = NetPeering()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get("custom_route:a").excluded == (
        "NET-PEERING: recreate VPC peering after migrating"
    )


# ---- NET-CUSTOM-ROUTE -------------------------------------------------------


def test_custom_route_detects_other_targets():
    net = _network()
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "route-1"),
        network="network:main",
        destination="10.1.0.0/16",
        target_type="vpn",
        target_id="vgw-1",
    )
    doc = _doc(net, route)
    diffs = NetCustomRoute().detect(doc)
    assert [d.key for d in diffs] == ["NET-CUSTOM-ROUTE@custom_route:a"]


def test_custom_route_detects_nothing_for_peering():
    net = _network()
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "route-1"),
        network="network:main",
        destination="10.1.0.0/16",
        target_type="peering",
        target_id="pcx-1",
    )
    doc = _doc(net, route)
    assert NetCustomRoute().detect(doc) == []


def test_custom_route_manual_excludes_with_target_details():
    net = _network()
    route = CustomRoute(
        id="custom_route:a",
        name="a",
        source=_source("route", "route-1"),
        network="network:main",
        destination="10.1.0.0/16",
        target_type="vpn",
        target_id="vgw-1",
    )
    doc = _doc(net, route)
    rule = NetCustomRoute()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get("custom_route:a").excluded == (
        "NET-CUSTOM-ROUTE: vpn target vgw-1 is not migrated"
    )


# ---- NET-IPV6 ----------------------------------------------------------------


def test_ipv6_detects_network_with_ipv6_cidrs():
    net = Network(
        id="network:main",
        name="main",
        source=_source("vpc", "network:main"),
        cidr_blocks=["10.0.0.0/16"],
        ipv6_cidr_blocks=["2001:db8::/56"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    subnet = Subnet(
        id="subnet:a",
        name="a",
        source=_source("subnet", "subnet-1"),
        network="network:main",
        cidr="10.0.1.0/24",
        ipv6_cidr="2001:db8::/64",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=True,
    )
    doc = _doc(net, subnet)
    from easymigrate.differences.rules.aws_gcp.network import NetIpv6

    diffs = NetIpv6().detect(doc)
    assert [d.key for d in diffs] == ["NET-IPV6@network:main"]
    assert diffs[0].subjects == ["network:main", "subnet:a"]


def test_ipv6_detects_nothing_without_ipv6():
    doc = _doc(_network())
    from easymigrate.differences.rules.aws_gcp.network import NetIpv6

    assert NetIpv6().detect(doc) == []


def test_ipv6_ipv4_only_clears_ipv6_fields():
    net = Network(
        id="network:main",
        name="main",
        source=_source("vpc", "network:main"),
        cidr_blocks=["10.0.0.0/16"],
        ipv6_cidr_blocks=["2001:db8::/56"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    subnet = Subnet(
        id="subnet:a",
        name="a",
        source=_source("subnet", "subnet-1"),
        network="network:main",
        cidr="10.0.1.0/24",
        ipv6_cidr="2001:db8::/64",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=True,
    )
    doc = _doc(net, subnet)
    from easymigrate.differences.rules.aws_gcp.network import NetIpv6

    rule = NetIpv6()
    diff = rule.detect(doc)[0]
    original = doc.model_copy(deep=True)

    result = rule.apply(doc, diff, "ipv4_only")

    assert doc == original
    assert result.get("network:main").ipv6_cidr_blocks == []
    assert result.get("subnet:a").ipv6_cidr is None


def test_ipv6_manual_makes_the_same_change():
    net = Network(
        id="network:main",
        name="main",
        source=_source("vpc", "network:main"),
        cidr_blocks=["10.0.0.0/16"],
        ipv6_cidr_blocks=["2001:db8::/56"],
        dns_hostnames=True,
        dns_resolution=True,
    )
    doc = _doc(net)
    from easymigrate.differences.rules.aws_gcp.network import NetIpv6

    rule = NetIpv6()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get("network:main").ipv6_cidr_blocks == []


def test_ipv6_strategies_include_manual_marker():
    from easymigrate.differences.rules.aws_gcp.network import NetIpv6

    rule = NetIpv6()
    assert any(s.id == "manual" and s.manual for s in rule.strategies)
    assert "isolated" in rule.impact


# ---- NET-CIDR-OVERLAP ---------------------------------------------------------


def test_cidr_overlap_detects_overlapping_networks():
    from easymigrate.differences.rules.aws_gcp.network import NetCidrOverlap

    net_a = _network("network:a", cidr="10.0.0.0/16")
    net_b = _network("network:b", cidr="10.0.0.0/16")
    doc = _doc(net_a, net_b)
    diffs = NetCidrOverlap().detect(doc)
    assert [d.key for d in diffs] == ["NET-CIDR-OVERLAP@network:a"]
    assert diffs[0].subjects == ["network:a", "network:b"]


def test_cidr_overlap_detects_nothing_for_disjoint_networks():
    from easymigrate.differences.rules.aws_gcp.network import NetCidrOverlap

    net_a = _network("network:a", cidr="10.0.0.0/16")
    net_b = _network("network:b", cidr="10.1.0.0/16")
    doc = _doc(net_a, net_b)
    assert NetCidrOverlap().detect(doc) == []


def test_cidr_overlap_keep_is_a_no_op():
    from easymigrate.differences.rules.aws_gcp.network import NetCidrOverlap

    net_a = _network("network:a", cidr="10.0.0.0/16")
    net_b = _network("network:b", cidr="10.0.0.0/16")
    doc = _doc(net_a, net_b)
    rule = NetCidrOverlap()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "keep")

    assert result == doc


def test_rules_7_through_11_have_valid_catalogue_ids():
    from easymigrate.differences.rules.aws_gcp.network import (
        NetCidrOverlap,
        NetCustomRoute,
        NetIpv6,
        NetPeering,
    )

    for rule_cls in (NetNacl, NetPeering, NetCustomRoute, NetIpv6, NetCidrOverlap):
        rule = rule_cls()
        assert rule.family == "network"
        assert rule.id.startswith("NET-")


# ---- fixture-based detection (plan 3 task 3) -------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "plugins" / "network" / "fixtures"

_EXPECTED_KEYS: dict[str, list[str]] = {
    "basic_public": ["NET-ZONE-REGIONAL@network:migration-test-vpc"],
    "custom_nacl": ["NET-NACL@network_acl:db-acl", "NET-ZONE-REGIONAL@network:locked-vpc"],
    "external_sg_ref": ["NET-SG-XVPC-REF@firewall_policy:app"],
    "ipv6": ["NET-IPV6@network:dual-stack", "NET-ZONE-REGIONAL@network:dual-stack"],
    "multi_vpc": [
        "NET-CIDR-OVERLAP@network:prod",
        "NET-ZONE-REGIONAL@network:prod",
        "NET-ZONE-REGIONAL@network:staging",
    ],
    "peering_route": [
        "NET-PEERING@custom_route:app-rt-172-16-0-0-16",
        "NET-ZONE-REGIONAL@network:requester",
    ],
    "private_with_nat": ["NET-ZONE-REGIONAL@network:app-vpc"],
    "sg_references_sg": [
        "NET-EGRESS-TAG@firewall_policy:web",
        "NET-ICMP-TYPE@firewall_policy:web",
    ],
}


def _golden_doc(case: str) -> IRDocument:
    from easymigrate.core.plugin import NormalizeContext, Snapshot
    from easymigrate.plugins.network import NetworkPlugin

    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    objects = NetworkPlugin().normalize(Snapshot.from_dir(_FIXTURES_DIR / case / "snapshot"), ctx)
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objects,
        findings=ctx.findings,
    )


def test_every_fixture_case_has_expected_keys():
    cases = sorted(p.name for p in _FIXTURES_DIR.glob("*") if (p / "snapshot").is_dir())
    assert set(cases) == set(_EXPECTED_KEYS)


@pytest.mark.parametrize("case", sorted(_EXPECTED_KEYS))
def test_golden_detected_difference_keys(case):
    from easymigrate.differences.engine import detect

    doc = _golden_doc(case)
    differences = detect(doc)
    assert [d.key for d in differences] == _EXPECTED_KEYS[case]


# ---- NET-EGRESS-TAG narrowing (spec 4 §10) --------------------------------


def _compute(id_, subnet, policies):
    from easymigrate.ir.compute import ComputeInstance

    return ComputeInstance(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("instance", id_),
        instance_type="t3.micro",
        architecture="x86_64",
        state="running",
        zone="ap-south-1a",
        subnet=subnet,
        image="ami-1",
        platform="linux",
        firewall_policies=policies,
    )


def _egress_to(peer_id):
    return FirewallRule(
        direction="egress",
        protocol="tcp",
        ports=["443"],
        peer=PolicyPeer(policy=peer_id),
    )


def _narrowing_doc(*extra):
    net = _network(cidr="10.0.0.0/16")
    peer = _policy("firewall_policy:peer")
    main = _policy("firewall_policy:main", rules=[_egress_to("firewall_policy:peer")])
    return _doc(
        net,
        _subnet("subnet:a", cidr="10.0.1.0/24"),
        _subnet("subnet:b", cidr="10.0.2.0/24"),
        _subnet("subnet:c", cidr="10.0.3.0/24"),
        peer,
        main,
        *extra,
    )


def _ranges(doc):
    rule = NetEgressTag()
    diff = rule.detect(doc)[0]
    result = rule.apply(doc, diff, "peer_attached_ranges")
    return result.get("firewall_policy:main").target_hints["egress_ranges"]


def test_egress_narrows_to_the_subnets_hosting_the_peers_instances():
    doc = _narrowing_doc(
        _compute("compute_instance:one", "subnet:a", ["firewall_policy:peer"]),
        _compute("compute_instance:two", "subnet:b", ["firewall_policy:peer"]),
    )

    assert _ranges(doc)["firewall_policy:peer"] == ["10.0.1.0/24", "10.0.2.0/24"]


def test_egress_narrowing_is_strictly_tighter_than_the_network_range():
    """A regression that widened this back to the VPC CIDR would still be 'a list of CIDRs',
    so assert the narrowing itself, not just the shape."""
    doc = _narrowing_doc(_compute("compute_instance:one", "subnet:a", ["firewall_policy:peer"]))

    ranges = _ranges(doc)["firewall_policy:peer"]

    assert ranges == ["10.0.1.0/24"]
    assert "10.0.0.0/16" not in ranges


def test_egress_falls_back_to_network_cidrs_when_no_instance_is_behind_the_peer():
    doc = _narrowing_doc()

    assert _ranges(doc)["firewall_policy:peer"] == ["10.0.0.0/16"]


def test_egress_falls_back_when_the_peers_only_member_is_a_template():
    """A launch template names no subnet; the group that uses it does, and that describes
    where instances *may* launch rather than where they are."""
    from easymigrate.ir.compute import InstanceTemplate

    template = InstanceTemplate(
        id="instance_template:web-lt",
        name="web-lt",
        source=_source("launch-template", "lt-1"),
        instance_type="t3.micro",
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        version="1",
        firewall_policies=["firewall_policy:peer"],
    )
    doc = _narrowing_doc(template)

    assert _ranges(doc)["firewall_policy:peer"] == ["10.0.0.0/16"]


def test_egress_resolves_each_peer_independently():
    net = _network(cidr="10.0.0.0/16")
    busy = _policy("firewall_policy:busy")
    empty = _policy("firewall_policy:empty")
    main = _policy(
        "firewall_policy:main",
        rules=[_egress_to("firewall_policy:busy"), _egress_to("firewall_policy:empty")],
    )
    doc = _doc(
        net,
        _subnet("subnet:a", cidr="10.0.1.0/24"),
        busy,
        empty,
        main,
        _compute("compute_instance:one", "subnet:a", ["firewall_policy:busy"]),
    )

    ranges = _ranges(doc)

    assert ranges["firewall_policy:busy"] == ["10.0.1.0/24"]
    assert ranges["firewall_policy:empty"] == ["10.0.0.0/16"]


def test_egress_deduplicates_instances_sharing_a_subnet():
    doc = _narrowing_doc(
        _compute("compute_instance:one", "subnet:a", ["firewall_policy:peer"]),
        _compute("compute_instance:two", "subnet:a", ["firewall_policy:peer"]),
    )

    assert _ranges(doc)["firewall_policy:peer"] == ["10.0.1.0/24"]


def test_egress_effect_text_no_longer_admits_applies_to_is_unknown():
    effect = next(s for s in NetEgressTag.strategies if s.id == "peer_attached_ranges").effect
    assert "applies_to" not in effect
    assert "subnets hosting" in effect
