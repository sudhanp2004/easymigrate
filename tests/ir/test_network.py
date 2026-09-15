import pytest
from pydantic import ValidationError

from easymigrate.ir import IRDocument, SourceRef, intent_class
from easymigrate.ir.network import (
    AclRule,
    CidrPeer,
    CustomRoute,
    ExternalPolicyPeer,
    FirewallPolicy,
    FirewallRule,
    Nat,
    Network,
    NetworkAcl,
    PolicyPeer,
    SelfPeer,
    Subnet,
)


def src(type_: str, id_: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def network(name: str = "main", **overrides) -> Network:
    fields = dict(cidr_blocks=["10.0.0.0/16"], dns_hostnames=True, dns_resolution=True)
    fields.update(overrides)
    return Network(id=f"network:{name}", name=name, source=src("vpc", "vpc-1"), **fields)


def subnet(name: str = "s1", **overrides) -> Subnet:
    fields = dict(
        network="network:main",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=True,
    )
    fields.update(overrides)
    return Subnet(id=f"subnet:{name}", name=name, source=src("subnet", f"subnet-{name}"), **fields)


def policy(name: str, rules: list[FirewallRule]) -> FirewallPolicy:
    return FirewallPolicy(
        id=f"firewall_policy:{name}",
        name=name,
        source=src("security_group", f"sg-{name}"),
        network="network:main",
        rules=rules,
    )


def document(*objects) -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=list(objects),
    )


def test_networking_kinds_are_registered():
    assert intent_class("network") is Network
    assert intent_class("subnet") is Subnet
    assert intent_class("nat") is Nat
    assert intent_class("firewall_policy") is FirewallPolicy
    assert intent_class("network_acl") is NetworkAcl
    assert intent_class("custom_route") is CustomRoute


def test_full_networking_document_round_trips():
    web = policy(
        "web",
        [
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["443"], peer=CidrPeer(cidr="0.0.0.0/0")
            ),
            FirewallRule(direction="ingress", protocol="all", peer=SelfPeer()),
            FirewallRule(
                direction="egress",
                protocol="tcp",
                ports=["5432"],
                peer=PolicyPeer(policy="firewall_policy:db"),
            ),
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["22"],
                peer=ExternalPolicyPeer(group_id="sg-999", owner_id="210987654321"),
            ),
        ],
    )
    db = policy("db", [])
    original = document(
        network(ipv6_cidr_blocks=["2600:1f18:abcd:1200::/56"]),
        subnet("s1"),
        subnet("s2", cidr="10.0.2.0/24", internet="private_nat", map_public_ip=False),
        Nat(
            id="nat:nat-1",
            name="nat-1",
            source=src("nat_gateway", "nat-1"),
            network="network:main",
            serves_subnets=["subnet:s2"],
        ),
        web,
        db,
        NetworkAcl(
            id="network_acl:locked",
            name="locked",
            source=src("network_acl", "acl-1"),
            network="network:main",
            subnets=["subnet:s2"],
            rules=[
                AclRule(
                    number=100,
                    direction="ingress",
                    protocol="tcp",
                    ports=["443"],
                    cidr="10.0.0.0/16",
                    action="allow",
                )
            ],
        ),
        CustomRoute(
            id="custom_route:pcx-1",
            name="pcx-1",
            source=src("route", "rtb-1:172.16.0.0/16"),
            network="network:main",
            destination="172.16.0.0/16",
            target_type="peering",
            target_id="pcx-1",
            subnets=["subnet:s1"],
        ),
    )

    restored = IRDocument.model_validate_json(original.model_dump_json())

    assert restored.model_dump() == original.model_dump()
    assert isinstance(restored.get("firewall_policy:web").rules[2].peer, PolicyPeer)
    assert isinstance(restored.get("firewall_policy:web").rules[3].peer, ExternalPolicyPeer)


def test_subnet_must_point_at_existing_network():
    with pytest.raises(ValidationError, match="subnet:s1 references missing object network:main"):
        document(subnet("s1"))


def test_reference_fields_check_the_kind():
    with pytest.raises(ValidationError, match="expected a network reference"):
        subnet(network="subnet:oops")


def test_policy_peer_must_point_at_existing_policy():
    rule = FirewallRule(
        direction="ingress",
        protocol="tcp",
        ports=["80"],
        peer=PolicyPeer(policy="firewall_policy:gone"),
    )
    with pytest.raises(ValidationError, match="references missing object firewall_policy:gone"):
        document(network(), policy("web", [rule]))


@pytest.mark.parametrize(
    "cidrs, message",
    [
        (["10.0.0.0/33"], "not a valid CIDR"),
        (["10.0.0.0"], "not a valid CIDR"),
        (["10.0.1.5/24"], "not a valid CIDR"),
        (["2600:1f18::/56"], "not an IPv4 CIDR"),
        ([], "at least 1 item"),
    ],
)
def test_network_ipv4_cidrs_are_validated(cidrs, message):
    with pytest.raises(ValidationError, match=message):
        network(cidr_blocks=cidrs)


def test_network_ipv6_cidrs_must_be_ipv6():
    with pytest.raises(ValidationError, match="not an IPv6 CIDR"):
        network(ipv6_cidr_blocks=["10.0.0.0/16"])


@pytest.mark.parametrize(
    "protocol, ports",
    [("tcp", ["443"]), ("udp", ["8000-8080"]), ("tcp", ["0-65535"]), ("all", []), ("58", [])],
)
def test_valid_protocols_and_ports(protocol, ports):
    FirewallRule(direction="ingress", protocol=protocol, ports=ports, peer=SelfPeer())


@pytest.mark.parametrize(
    "protocol, ports, message",
    [
        ("tcp", ["70000"], "invalid port range"),
        ("tcp", ["90-80"], "invalid port range"),
        ("tcp", ["http"], "invalid port"),
        ("icmp", ["1"], "ports are only allowed for tcp/udp"),
        ("banana", [], "unsupported protocol"),
        ("300", [], "unsupported protocol"),
    ],
)
def test_invalid_protocols_and_ports(protocol, ports, message):
    with pytest.raises(ValidationError, match=message):
        FirewallRule(direction="ingress", protocol=protocol, ports=ports, peer=SelfPeer())


def test_acl_rule_number_range():
    with pytest.raises(ValidationError):
        AclRule(number=32767, direction="ingress", protocol="all", cidr="0.0.0.0/0", action="deny")
