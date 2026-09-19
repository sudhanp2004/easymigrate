"""Policy membership derived from forward references (spec 4 §2.1).

This replaces `FirewallPolicy.applies_to`. The point of deriving it is that it cannot drift from
the objects it describes, and no future family has to remember to maintain a back-reference.
"""

from __future__ import annotations

from easymigrate.differences.membership import policy_members
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.compute import ComputeInstance, InstanceTemplate
from easymigrate.ir.database import DatabaseInstance, DbCompute, DbNetwork, DbStorage
from easymigrate.ir.network import FirewallPolicy, Network, Subnet


def source(type_: str, native: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native, region="ap-south-1")


def network() -> Network:
    return Network(
        id="network:vpc",
        name="vpc",
        source=source("vpc", "vpc-1"),
        cidr_blocks=["10.0.0.0/16"],
        dns_hostnames=True,
        dns_resolution=True,
    )


def subnet() -> Subnet:
    return Subnet(
        id="subnet:a",
        name="a",
        source=source("subnet", "subnet-1"),
        network="network:vpc",
        cidr="10.0.1.0/24",
        zone="ap-south-1a",
        internet="public",
        map_public_ip=False,
    )


def policy(name: str) -> FirewallPolicy:
    return FirewallPolicy(
        id=f"firewall_policy:{name}",
        name=name,
        source=source("security-group", f"sg-{name}"),
        network="network:vpc",
    )


def compute(name: str, policies: list[str]) -> ComputeInstance:
    return ComputeInstance(
        id=f"compute_instance:{name}",
        name=name,
        source=source("instance", f"i-{name}"),
        instance_type="t3.micro",
        architecture="x86_64",
        state="running",
        zone="ap-south-1a",
        subnet="subnet:a",
        image="ami-1",
        platform="linux",
        firewall_policies=policies,
    )


def database(name: str, policies: list[str]) -> DatabaseInstance:
    return DatabaseInstance(
        id=f"database_instance:{name}",
        name=name,
        source=source("db", name),
        engine="postgres",
        engine_version="15.5",
        compute=DbCompute(class_name="db.t3.micro"),
        storage=DbStorage(size_gib=20, type="gp3"),
        network=DbNetwork(port=5432, firewall_policies=policies),
    )


def template(name: str, policies: list[str]) -> InstanceTemplate:
    return InstanceTemplate(
        id=f"instance_template:{name}",
        name=name,
        source=source("launch-template", f"lt-{name}"),
        instance_type="t3.micro",
        architecture="x86_64",
        image="ami-1",
        platform="linux",
        version="1",
        firewall_policies=policies,
    )


def document(*objects) -> IRDocument:
    return IRDocument(
        source={"cloud": "aws", "account_id": "1", "region": "ap-south-1"},
        objects=[network(), subnet(), *objects],
    )


def test_database_instance_is_a_member():
    doc = document(policy("db"), database("orders", ["firewall_policy:db"]))
    assert policy_members(doc)["firewall_policy:db"] == {"database_instance:orders"}


def test_shared_policy_reports_both_members():
    """The case that `applies_to` got wrong: only the database plugin wrote it."""
    doc = document(
        policy("shared"),
        database("orders", ["firewall_policy:shared"]),
        compute("web", ["firewall_policy:shared"]),
    )
    assert policy_members(doc)["firewall_policy:shared"] == {
        "database_instance:orders",
        "compute_instance:web",
    }


def test_policy_guarding_nothing_is_present_with_an_empty_set():
    doc = document(policy("orphan"))
    members = policy_members(doc)
    assert "firewall_policy:orphan" in members
    assert members["firewall_policy:orphan"] == set()


def test_non_policy_references_are_ignored():
    doc = document(policy("web"), compute("web", []))
    # The instance references subnet:a, which is not a policy.
    assert policy_members(doc)["firewall_policy:web"] == set()
    assert "subnet:a" not in policy_members(doc)


def test_instance_template_is_a_member():
    doc = document(policy("web"), template("web-lt", ["firewall_policy:web"]))
    assert policy_members(doc)["firewall_policy:web"] == {"instance_template:web-lt"}


def test_every_policy_appears_even_with_no_objects_around():
    doc = document(policy("a"), policy("b"))
    assert set(policy_members(doc)) == {"firewall_policy:a", "firewall_policy:b"}


def test_a_peer_policy_is_not_a_member():
    """`FirewallPolicy.references()` includes peer policies, but a rule pointing *at* a group
    is not a resource attached *to* it. Counting peers as members would make DB-SG-FIREWALL
    think an RDS-only policy had a non-database member and stop dropping it."""
    from easymigrate.ir.network import FirewallRule, PolicyPeer

    app = policy("app")
    app = app.model_copy(
        update={
            "rules": [
                FirewallRule(
                    direction="egress",
                    protocol="tcp",
                    ports=["5432"],
                    peer=PolicyPeer(policy="firewall_policy:db"),
                )
            ]
        }
    )
    doc = document(policy("db"), app, database("orders", ["firewall_policy:db"]))

    assert policy_members(doc)["firewall_policy:db"] == {"database_instance:orders"}
