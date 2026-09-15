from easymigrate.core.plugin import NormalizeContext
from easymigrate.ir import IRDocument
from easymigrate.ir.network import (
    CidrPeer,
    ExternalPolicyPeer,
    PolicyPeer,
    PrefixListPeer,
    SelfPeer,
)
from easymigrate.plugins.network.normalize_security import build_security
from easymigrate.plugins.network.normalize_topology import build_topology
from tests.plugins.network import aws_builders as aws


def run(snap):
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1", strict=True)
    topology = build_topology(snap, ctx)
    policies, acls = build_security(snap, ctx, topology)
    objects = [*topology.networks, *topology.subnets, *policies, *acls]
    IRDocument(source={"cloud": "aws"}, objects=objects)  # validates references
    return policies, acls, ctx


def base(**files):
    files.setdefault("vpcs", [aws.vpc("vpc-1", name="main")])
    files.setdefault("route_tables", [aws.route_table("rtb-main", "vpc-1", main=True)])
    return aws.snapshot(**files)


def test_cidr_rules_ports_descriptions_and_sorting():
    web = aws.security_group(
        "sg-web",
        "vpc-1",
        "web",
        ingress=[
            aws.permission("tcp", 443, 443, cidrs=("0.0.0.0/0",), description="https"),
            aws.permission("6", 8000, 8080, ipv6=("2600:1F18::/32",)),
            aws.permission("tcp", 0, 65535, cidrs=("10.0.0.0/16",)),
        ],
    )

    (policy,), acls, _ = run(base(security_groups=[web]))

    assert (policy.id, policy.name, policy.network) == (
        "firewall_policy:web",
        "web",
        "network:main",
    )
    assert policy.source.id == "sg-web" and policy.source.type == "security_group"
    assert policy.applies_to == []
    summary = [(r.direction, r.protocol, r.ports, r.peer, r.description) for r in policy.rules]
    assert summary == [
        ("egress", "all", [], CidrPeer(cidr="0.0.0.0/0"), None),
        ("ingress", "tcp", [], CidrPeer(cidr="10.0.0.0/16"), None),
        ("ingress", "tcp", ["443"], CidrPeer(cidr="0.0.0.0/0"), "https"),
        ("ingress", "tcp", ["8000-8080"], CidrPeer(cidr="2600:1f18::/32"), None),
    ]
    assert acls == []


def test_group_references_self_same_vpc_other_account_and_other_vpc():
    db = aws.security_group(
        "sg-db",
        "vpc-1",
        "db",
        ingress=[
            aws.permission("tcp", 5432, 5432, groups=({"GroupId": "sg-web", "UserId": aws.OWNER},)),
            aws.permission("-1", groups=({"GroupId": "sg-db"},)),
            aws.permission(
                "tcp", 22, 22, groups=({"GroupId": "sg-999", "UserId": "210987654321"},)
            ),
            aws.permission("tcp", 9000, 9000, groups=({"GroupId": "sg-other-vpc"},)),
        ],
        egress=[],
    )
    web = aws.security_group("sg-web", "vpc-1", "web", egress=[])
    other = aws.security_group("sg-other-vpc", "vpc-2", "other", egress=[])
    snap = base(
        vpcs=[aws.vpc("vpc-1", name="main"), aws.vpc("vpc-2", "10.1.0.0/16", name="second")],
        route_tables=[],
        security_groups=[db, web, other],
    )

    policies, _, _ = run(snap)

    rules = {
        r.ports[0] if r.ports else r.protocol: r.peer
        for r in next(p for p in policies if p.name == "db").rules
    }
    assert rules["5432"] == PolicyPeer(policy="firewall_policy:web")
    assert rules["all"] == SelfPeer()
    assert rules["22"] == ExternalPolicyPeer(group_id="sg-999", owner_id="210987654321")
    assert rules["9000"] == ExternalPolicyPeer(group_id="sg-other-vpc", vpc_id="vpc-2")


def test_prefix_lists_icmp_and_other_protocols():
    group = aws.security_group(
        "sg-1",
        "vpc-1",
        "mixed",
        ingress=[
            aws.permission("icmp", 8, -1, cidrs=("10.0.0.0/8",)),
            aws.permission("icmp", -1, -1, cidrs=("10.0.0.0/8",)),
            aws.permission("58", 128, 0, ipv6=("::/0",)),
            aws.permission("50", cidrs=("10.9.0.0/16",)),
        ],
        egress=[aws.permission("tcp", 443, 443, prefix_lists=("pl-123",))],
    )

    (policy,), _, _ = run(base(security_groups=[group]))

    by_protocol = {
        (r.protocol, r.icmp_type, r.icmp_code) for r in policy.rules if r.direction == "ingress"
    }
    assert by_protocol == {
        ("icmp", 8, None),
        ("icmp", None, None),
        ("icmpv6", 128, 0),
        ("50", None, None),
    }
    (egress,) = [r for r in policy.rules if r.direction == "egress"]
    assert egress.peer == PrefixListPeer(prefix_list_id="pl-123")


def test_default_groups_in_two_vpcs_get_distinct_ids_and_unknown_vpcs_are_skipped():
    snap = base(
        vpcs=[aws.vpc("vpc-1"), aws.vpc("vpc-2", "10.1.0.0/16")],
        route_tables=[],
        security_groups=[
            aws.security_group("sg-a", "vpc-1", "default"),
            aws.security_group("sg-b", "vpc-2", "default"),
            aws.security_group("sg-c", "vpc-missing", "stray"),
        ],
    )

    policies, _, _ = run(snap)

    assert sorted(p.id for p in policies) == [
        "firewall_policy:default-sg-a",
        "firewall_policy:default-sg-b",
    ]


def test_allow_all_acls_are_omitted_but_restrictive_ones_kept():
    custom = aws.network_acl(
        "acl-db",
        "vpc-1",
        [
            aws.acl_entry(200, protocol="6", egress=False, cidr="10.0.0.0/16", ports=(5432, 5432)),
            aws.acl_entry(100, protocol="1", egress=False, cidr="10.0.0.0/16", icmp=(8, -1)),
            aws.acl_entry(100, protocol="-1", action="deny", egress=True, cidr="::/0"),
            aws.acl_entry(32767, action="deny", egress=False),
            aws.acl_entry(32767, action="deny", egress=True),
        ],
        subnets=("subnet-db",),
        name="db-acl",
    )
    snap = base(
        subnets=[
            aws.subnet("subnet-db", "vpc-1", name="db"),
            aws.subnet("subnet-web", "vpc-1", "10.0.2.0/24", name="web"),
        ],
        network_acls=[
            aws.network_acl(
                "acl-default",
                "vpc-1",
                aws.DEFAULT_ACL_ENTRIES,
                is_default=True,
                subnets=("subnet-web",),
            ),
            aws.network_acl("acl-open", "vpc-1", aws.DEFAULT_ACL_ENTRIES),
            aws.network_acl("acl-deny", "vpc-1", [aws.acl_entry(32767, action="deny")]),
            custom,
        ],
    )

    _, acls, _ = run(snap)

    assert sorted(a.id for a in acls) == ["network_acl:acl-deny", "network_acl:db-acl"]
    db = next(a for a in acls if a.id == "network_acl:db-acl")
    assert db.subnets == ["subnet:db"]
    assert [
        (r.direction, r.number, r.protocol, r.ports, r.icmp_type, r.cidr, r.action)
        for r in db.rules
    ] == [
        ("egress", 100, "all", [], None, "::/0", "deny"),
        ("ingress", 100, "icmp", [], 8, "10.0.0.0/16", "allow"),
        ("ingress", 200, "tcp", ["5432"], None, "10.0.0.0/16", "allow"),
    ]
    deny = next(a for a in acls if a.id == "network_acl:acl-deny")
    assert deny.rules == []
