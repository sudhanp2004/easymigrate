"""Migration scope (core/scope.py): keep what the user means, plus what it depends on."""

from __future__ import annotations

from easymigrate.core.scope import Scope, apply_scope
from easymigrate.ir.base import SourceRef
from easymigrate.ir.compute import ComputeInstance, Disk
from easymigrate.ir.iam import IamRole
from easymigrate.ir.kubernetes import KubernetesCluster, NodePool
from easymigrate.ir.network import FirewallPolicy, Nat, Network, Subnet
from easymigrate.ir.storage import StorageBucket


def _src(t, i):
    return SourceRef(cloud="aws", type=t, id=i, region="ap-south-1")


def _world():
    objects = []
    for vpc, cidr in (("app", "10.0"), ("default", "172.31")):
        objects += [
            Network(
                id=f"network:{vpc}",
                name=vpc,
                source=_src("vpc", f"vpc-{vpc}"),
                cidr_blocks=[f"{cidr}.0.0/16"],
                dns_hostnames=True,
                dns_resolution=True,
            ),
            Subnet(
                id=f"subnet:{vpc}",
                name=vpc,
                source=_src("subnet", f"subnet-{vpc}"),
                network=f"network:{vpc}",
                cidr=f"{cidr}.1.0/24",
                zone="ap-south-1a",
                internet="public",
                map_public_ip=False,
            ),
            FirewallPolicy(
                id=f"firewall_policy:{vpc}-sg",
                name=f"{vpc}-sg",
                source=_src("sg", f"sg-{vpc}"),
                network=f"network:{vpc}",
            ),
        ]
    objects.append(
        Nat(
            id="nat:app",
            name="app",
            source=_src("nat", "nat-1"),
            network="network:app",
            serves_subnets=["subnet:app"],
        )
    )
    for name, vpc, labels in (("web", "app", {"app": "shop"}), ("trineural", "default", {})):
        objects += [
            Disk(
                id=f"disk:{name}",
                name=name,
                source=_src("volume", f"vol-{name}"),
                size_gib=8,
                type="gp3",
                zone="ap-south-1a",
                boot=True,
            ),
            ComputeInstance(
                id=f"compute_instance:{name}",
                name=name,
                labels=labels,
                source=_src("instance", f"i-{name}"),
                instance_type="t3.micro",
                architecture="x86_64",
                state="running",
                zone="ap-south-1a",
                subnet=f"subnet:{vpc}",
                firewall_policies=[f"firewall_policy:{vpc}-sg"],
                disks=[f"disk:{name}"],
                image="ami-1",
                platform="linux",
            ),
        ]
    objects += [
        KubernetesCluster(
            id="kubernetes_cluster:prod",
            name="prod",
            source=_src("eks", "prod"),
            version="1.31",
            subnets=["subnet:app"],
        ),
        NodePool(
            id="node_pool:workers",
            name="workers",
            source=_src("ng", "w"),
            cluster="kubernetes_cluster:prod",
            origin="managed",
            min_size=1,
            max_size=1,
            desired_size=1,
            ami_type="AL2_x86_64",
            subnets=["subnet:app"],
        ),
        StorageBucket(
            id="storage_bucket:assets",
            name="assets",
            labels={"app": "shop"},
            source=_src("s3", "assets"),
            bucket_name="assets",
            region="ap-south-1",
        ),
        StorageBucket(
            id="storage_bucket:other",
            name="other",
            source=_src("s3", "other"),
            bucket_name="other",
            region="ap-south-1",
        ),
        IamRole(id="iam_role:r", name="r", source=_src("iam", "r"), arn="arn:aws:iam::1:role/r"),
    ]
    return objects


def _ids(result):
    return {o.id for o in result.objects}


def test_no_scope_keeps_everything():
    world = _world()
    assert len(apply_scope(world, None).objects) == len(world)
    assert len(apply_scope(world, Scope()).objects) == len(world)


def test_vpc_scope_keeps_the_vpc_its_resources_and_their_dependencies():
    result = apply_scope(_world(), Scope(vpcs=["vpc-app"]))
    ids = _ids(result)
    assert {
        "compute_instance:web",
        "disk:web",
        "kubernetes_cluster:prod",
        "node_pool:workers",
        "nat:app",
    } <= ids
    assert {"network:app", "subnet:app", "firewall_policy:app-sg"} <= ids
    # The default VPC and everything in it is out.
    assert not ids & {
        "compute_instance:trineural",
        "disk:trineural",
        "network:default",
        "subnet:default",
    }
    # Buckets belong to no VPC: kept, and the report says so.
    assert {"storage_bucket:assets", "storage_bucket:other", "iam_role:r"} <= ids
    codes = [f.code for f in result.findings]
    assert "SCOPE-OUT-OF-SCOPE" in codes and "SCOPE-NOT-IN-A-VPC" in codes


def test_vpc_can_be_named_by_ir_id_or_name():
    assert "compute_instance:web" in _ids(apply_scope(_world(), Scope(vpcs=["app"])))
    assert "compute_instance:web" in _ids(apply_scope(_world(), Scope(vpcs=["network:app"])))


def test_tag_scope_keeps_matching_resources_and_their_vpc_only():
    ids = _ids(apply_scope(_world(), Scope(tags=["app=shop"])))
    assert {"compute_instance:web", "storage_bucket:assets", "network:app", "nat:app"} <= ids
    assert not ids & {"storage_bucket:other", "compute_instance:trineural", "network:default"}
    # The cluster is untagged, so it is out; its node pool goes with it.
    assert not ids & {"kubernetes_cluster:prod", "node_pool:workers"}


def test_tag_key_alone_matches_any_value():
    assert "storage_bucket:assets" in _ids(apply_scope(_world(), Scope(tags=["app"])))


def test_exclude_alone_drops_one_resource_and_its_private_dependencies():
    """The case every live run hit: an unrelated instance in the account."""
    ids = _ids(apply_scope(_world(), Scope(exclude=["trineural"])))
    assert "compute_instance:trineural" not in ids and "disk:trineural" not in ids
    # With only an exclusion, networks are not narrowed.
    assert {"network:default", "compute_instance:web", "storage_bucket:other"} <= ids


def test_excluding_a_vpc_drops_everything_in_it():
    ids = _ids(apply_scope(_world(), Scope(exclude=["vpc-default"])))
    assert not ids & {"network:default", "subnet:default", "compute_instance:trineural"}
    assert "compute_instance:web" in ids


def test_excluding_something_a_kept_resource_needs_is_refused_and_said():
    result = apply_scope(_world(), Scope(exclude=["subnet-app"]))
    assert "subnet:app" in _ids(result)
    assert "SCOPE-EXCLUDE-IGNORED" in [f.code for f in result.findings]


def test_unknown_vpc_is_a_scan_gap():
    result = apply_scope(_world(), Scope(vpcs=["vpc-typo"]))
    assert "SCOPE-VPC-NOT-FOUND" in [f.code for f in result.findings]


def test_scoped_document_keeps_referential_integrity():
    from easymigrate.ir.base import IRDocument

    for scope in (Scope(vpcs=["vpc-app"]), Scope(tags=["app=shop"]), Scope(exclude=["trineural"])):
        IRDocument(source={"cloud": "aws"}, objects=apply_scope(_world(), scope).objects)
